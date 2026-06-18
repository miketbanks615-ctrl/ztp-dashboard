from __future__ import annotations

import html
import json
import re
import shutil
import socket
import sys
import threading
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .state import ZTPState


STATUS_META: dict[str, tuple[str, str, bool]] = {
    "starting":        ("Starting",        "#6366f1", True),
    "eos_checking":    ("Checking EOS",    "#0170C5", False),
    "eos_upgrading":   ("EOS Upgrade",     "#d97706", True),
    "eos_ok":          ("EOS OK",          "#15803d", False),
    "config_applying": ("Applying Config", "#0170C5", True),
    "complete":        ("Complete",        "#15803d", False),
    "error":           ("Error",           "#b91c1c", False),
    "unknown":         ("Unknown",         "#9ca3af", False),
}

CONTENT_TYPES = {
    ".cfg":  "text/plain; charset=utf-8",
    ".swi":  "application/octet-stream",
    ".py":   "text/plain; charset=utf-8",
    ".txt":  "text/plain; charset=utf-8",
}


# ── helpers ───────────────────────────────────────────────────────────────────

def _local_ips() -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for target in ("8.8.8.8", "192.168.0.1", "10.0.0.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(0)
                s.connect((target, 80))
                ip = s.getsockname()[0]
                if not ip.startswith("127.") and ip not in seen:
                    seen.add(ip)
                    result.append(ip)
            break
        except OSError:
            continue
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in seen:
                seen.add(ip)
                result.append(ip)
    except OSError:
        pass
    return result or ["127.0.0.1"]


def _scan_configs(d: Path | None) -> list[str]:
    """Serial numbers from *.cfg files — used for device inventory."""
    if not d or not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.cfg"))


def _scan_all_files(d: Path | None) -> list[str]:
    """All filenames in directory — used for directory status display."""
    if not d or not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_file())


def _scan_eos(d: Path | None) -> list[str]:
    """EOS image filenames (*.swi)."""
    if not d or not d.is_dir():
        return []
    return sorted(p.name for p in d.glob("*.swi"))


def _bundled_script_path() -> Path | None:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", "")) / "ztp_dashboard" / "data"
    else:
        base = Path(__file__).parent / "data"
    p = base / "arista-ztp-startup.py"
    return p if p.exists() else None


def _check_and_update_script(script_path: Path, ip: str, port: int) -> str:
    """Update FILE_SERVER and DASHBOARD_URL in script if needed.
    Returns 'ok', 'updated', or 'not_found'."""
    if not script_path.exists():
        return "not_found"
    expected = f"http://{ip}:{port}"
    content = script_path.read_text(encoding="utf-8")
    server_ok = bool(re.search(
        rf'^FILE_SERVER\s*=\s*"{re.escape(expected)}"', content, re.MULTILINE
    ))
    dashboard_ok = bool(re.search(
        rf'^DASHBOARD_URL\s*=\s*"{re.escape(expected)}"', content, re.MULTILINE
    ))
    if server_ok and dashboard_ok:
        return "ok"
    content = re.sub(
        r'^(FILE_SERVER\s*=\s*)"[^"]*"', rf'\1"{expected}"', content, flags=re.MULTILINE
    )
    content = re.sub(
        r'^(DASHBOARD_URL\s*=\s*)"[^"]*"', rf'\1"{expected}"', content, flags=re.MULTILINE
    )
    script_path.write_text(content, encoding="utf-8")
    return "updated"


def _get_script_status(
    working_dir: Path | None, local_ips: list[str], port: int, selected_ip: str | None
) -> tuple[str, str]:
    """
    Returns (status, message).
    Single IP or selected IP → auto-update script.
    Multiple IPs, none selected → 'needs_select'.
    """
    if not working_dir:
        return "not_found", "Working folder not configured"
    script_path = working_dir / "arista-ztp-startup.py"
    if not script_path.exists():
        return "not_found", "arista-ztp-startup.py not found in working folder"

    if len(local_ips) == 1:
        ip = local_ips[0]
    elif selected_ip and selected_ip in local_ips:
        ip = selected_ip
    else:
        return "needs_select", "Multiple IPs detected — select the ZTP network interface below"

    result = _check_and_update_script(script_path, ip, port)
    url = f"http://{ip}:{port}"
    if result == "updated":
        return "updated", f"Auto-updated FILE_SERVER and DASHBOARD_URL → {url}"
    return "ok", f"FILE_SERVER and DASHBOARD_URL correctly set to {url}"


def _default_working_dir() -> str:
    if sys.platform == "win32":
        return r"C:\ZTP"
    return str(Path.home() / "ztp")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _setup_working_dir(path: Path, effective_ip: str | None, port: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "configs").mkdir(exist_ok=True)
    (path / "images").mkdir(exist_ok=True)
    (path / ".data").mkdir(exist_ok=True)
    script_dest = path / "arista-ztp-startup.py"
    if not script_dest.exists():
        bundled = _bundled_script_path()
        if bundled:
            shutil.copy2(bundled, script_dest)
    if effective_ip and script_dest.exists():
        _check_and_update_script(script_dest, effective_ip, port)


def _read_eos_config(script_path: Path | None) -> tuple[str, dict[str, str]]:
    """Read TARGET_VERSION and uncommented EOS_BY_MODEL entries from startup script."""
    if not script_path or not script_path.exists():
        return "", {}
    content = script_path.read_text(encoding="utf-8")
    m = re.search(r'^TARGET_VERSION\s*=\s*"([^"]*)"', content, re.MULTILINE)
    target = m.group(1) if m else ""
    by_model: dict[str, str] = {}
    m2 = re.search(r'^EOS_BY_MODEL\s*=\s*\{([^}]*)\}', content, re.MULTILINE | re.DOTALL)
    if m2:
        for line in m2.group(1).splitlines():
            stripped = line.strip().rstrip(",")
            if stripped.startswith("#") or not stripped:
                continue
            kv = re.match(r"""['"]([^'"]+)['"]\s*:\s*['"]([^'"]+)['"]""", stripped)
            if kv:
                by_model[kv.group(1)] = kv.group(2)
    return target, by_model


def _write_eos_config(
    script_path: Path, target_version: str, eos_by_model: dict[str, str]
) -> None:
    """Rewrite TARGET_VERSION and EOS_BY_MODEL in startup script."""
    content = script_path.read_text(encoding="utf-8")
    content = re.sub(
        r'^(TARGET_VERSION\s*=\s*)"[^"]*"',
        rf'\1"{target_version}"',
        content, flags=re.MULTILINE,
    )
    if eos_by_model:
        entries = "\n".join(f"    '{k}': '{v}'," for k, v in eos_by_model.items())
        new_block = f"EOS_BY_MODEL = {{\n{entries}\n}}"
    else:
        new_block = "EOS_BY_MODEL = {}"
    content = re.sub(
        r"^EOS_BY_MODEL\s*=\s*\{[^}]*\}",
        new_block,
        content, flags=re.MULTILINE | re.DOTALL,
    )
    script_path.write_text(content, encoding="utf-8")


def _maybe_auto_set_eos(script_path: Path | None, eos_images: list[str]) -> str:
    """If exactly one .swi exists and TARGET_VERSION doesn't match, auto-update it.
    Returns: 'auto_set', 'ok', 'multi', 'none', or 'no_script'."""
    if not script_path or not script_path.exists():
        return "no_script"
    if not eos_images:
        return "none"
    if len(eos_images) > 1:
        return "multi"
    target, by_model = _read_eos_config(script_path)
    if target != eos_images[0]:
        _write_eos_config(script_path, eos_images[0], by_model)
        return "auto_set"
    return "ok"


# ── main entry point ──────────────────────────────────────────────────────────

def run_ui(
    data_dir: Path,
    host: str = "0.0.0.0",
    port: int = 8090,
    open_browser: bool = True,
    bootstrap_file: Path | None = None,
) -> None:
    data_dir = data_dir.expanduser()

    # Mutable refs so setup can migrate state into working_dir/.data/
    state_ref: list[ZTPState] = [ZTPState(data_dir)]
    data_dir_ref: list[Path] = [data_dir]

    def st() -> ZTPState:
        return state_ref[0]

    local_ips = _local_ips()

    def _working_dir() -> Path | None:
        raw = st().get_setting("working_dir")
        return Path(raw) if raw else None

    def _configs_dir() -> Path | None:
        raw = st().get_setting("configs_dir")
        if raw:
            return Path(raw)
        wd = _working_dir()
        return (wd / "configs") if wd else None

    def _eos_dir() -> Path | None:
        raw = st().get_setting("eos_dir")
        if raw:
            return Path(raw)
        wd = _working_dir()
        return (wd / "images") if wd else None

    def _selected_ip() -> str | None:
        val = st().get_setting("selected_ip")
        return val if val and val in local_ips else None

    def _effective_ip() -> str | None:
        """The IP to use for DHCP / script: selected (multi) or only (single)."""
        if len(local_ips) == 1:
            return local_ips[0]
        return _selected_ip()

    def _reinit_state(new_data_dir: Path) -> None:
        """Move state DB into new_data_dir, carrying settings and device history."""
        new_data_dir.mkdir(parents=True, exist_ok=True)
        old = state_ref[0]
        new = ZTPState(new_data_dir)
        for key in ("working_dir", "configs_dir", "eos_dir", "selected_ip"):
            val = old.get_setting(key)
            if val:
                new.set_setting(key, val)
        for d in old.all_devices():
            new.upsert_device(
                serial=d["serial"], status=d["status"],
                model=d.get("model"), hostname=d.get("hostname"),
                eos_current=d.get("eos_current"), eos_target=d.get("eos_target"),
                message=d.get("message"),
            )
        state_ref[0] = new
        data_dir_ref[0] = new_data_dir

    # ── HTTP handler ──────────────────────────────────────────────────────────

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urllib.parse.urlparse(self.path).path
            wd = _working_dir()
            if path in ("/", "") and not wd:
                self._page_setup()
                return
            if path == "/setup":
                self._page_setup()
            elif path in ("/", ""):
                self._page_main()
            elif path == "/api/status":
                self._api_status()
            elif path == "/startup-script":
                self._view_script()
            elif path == "/arista-ztp-startup.py":
                self._serve_script_for_eos()
            elif path.startswith("/configs/"):
                self._serve_static(_configs_dir(), path[9:])
            elif path.startswith("/images/"):
                self._serve_static(_eos_dir(), path[8:])
            else:
                self._not_found()

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            path = urllib.parse.urlparse(self.path).path
            try:
                if path == "/setup":
                    self._handle_setup(body)
                elif path == "/select-ip":
                    self._handle_select_ip(body)
                elif path == "/api/device-status":
                    self._receive_device_status(body)
                elif path == "/save-settings":
                    self._save_settings(body)
                elif path == "/clear-all":
                    st().clear_all_devices()
                    self._redirect("All device history cleared.")
                elif path == "/clear-device":
                    self._clear_device(body)
                elif path == "/save-eos-config":
                    self._save_eos_config(body)
                elif path == "/shutdown":
                    self._shutdown()
                else:
                    self._redirect("Unknown action.")
            except Exception as exc:
                self._redirect(str(exc))

        def log_message(self, format: str, *args: object) -> None:
            return

        # ── response helpers ──────────────────────────────────────────────────

        def _html(self, page: str, status: int = 200) -> None:
            enc = page.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(enc)))
            self.end_headers()
            self.wfile.write(enc)

        def _json(self, data: object, status: int = 200) -> None:
            body = json.dumps(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, msg: str = "") -> None:
            self.send_response(303)
            self.send_header("Location", f"/?msg={urllib.parse.quote(msg)}")
            self.end_headers()

        def _not_found(self) -> None:
            self.send_response(404)
            self.end_headers()

        # ── static file serving ───────────────────────────────────────────────

        def _safe_path(self, base: Path | None, filename: str) -> Path | None:
            if not base or not base.is_dir():
                return None
            if "/" in filename or "\\" in filename or filename.startswith("."):
                return None
            candidate = base / filename
            try:
                candidate.resolve().relative_to(base.resolve())
                return candidate if candidate.is_file() else None
            except ValueError:
                return None

        def _serve_static(self, base: Path | None, filename: str) -> None:
            path = self._safe_path(base, filename)
            if not path:
                self._not_found()
                return
            ct = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
            size = path.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(size))
            if ct == "application/octet-stream":
                self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
            self.end_headers()
            try:
                with open(path, "rb") as f:
                    shutil.copyfileobj(f, self.wfile, 1 << 16)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _view_script(self) -> None:
            wd = _working_dir()
            sp = (wd / "arista-ztp-startup.py") if wd else None
            if not sp or not sp.is_file():
                self._html("<h1>Startup script not found.</h1>", 404)
                return
            enc = sp.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(enc)))
            self.end_headers()
            self.wfile.write(enc)

        def _serve_script_for_eos(self) -> None:
            wd = _working_dir()
            sp = (wd / "arista-ztp-startup.py") if wd else None
            if not sp or not sp.is_file():
                self._not_found()
                return
            enc = sp.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(enc)))
            self.end_headers()
            self.wfile.write(enc)

        # ── setup page ────────────────────────────────────────────────────────

        def _page_setup(self) -> None:
            wd = _working_dir()
            existing = str(wd) if wd else _default_working_dir()
            ip_bullets = "".join(
                f"<li><code>http://{ip}:{port}/arista-ztp-startup.py</code></li>"
                for ip in local_ips
            )
            page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Arista ZTP Dashboard — Setup</title>
  <style>
    *{{box-sizing:border-box}}
    body{{margin:0;font-family:Segoe UI,system-ui,sans-serif;background:#f0f2f5;color:#111;
         display:flex;align-items:center;justify-content:center;min-height:100vh}}
    .card{{background:#fff;border:1px solid #d1d5db;border-radius:12px;padding:36px 40px;
           width:100%;max-width:540px;box-shadow:0 4px 24px rgba(0,0,0,.08)}}
    h1{{margin:0 0 8px;font-size:22px}}
    p{{margin:0 0 18px;color:#6b7280;font-size:14px;line-height:1.55}}
    label{{display:block;font-size:13px;font-weight:700;margin-bottom:6px}}
    input{{width:100%;border:1px solid #d1d5db;border-radius:8px;padding:10px 12px;
           font:inherit;font-size:14px}}
    input:focus{{outline:none;border-color:#0170C5;box-shadow:0 0 0 3px rgba(1,112,197,.15)}}
    .hint{{margin:10px 0 20px;font-size:12px;color:#6b7280;background:#f9fafb;
           border:1px solid #e5e7eb;border-radius:6px;padding:10px 14px;line-height:1.7}}
    .hint ul{{margin:4px 0 0 16px;padding:0}}
    code{{font-family:Consolas,monospace;font-size:12px;background:#f3f4f6;
          border-radius:3px;padding:1px 5px}}
    button{{width:100%;border:0;border-radius:8px;background:#0170C5;color:#fff;
            padding:11px;font:inherit;font-size:14px;font-weight:700;cursor:pointer}}
    button:hover{{background:#0161ad}}
    .ips{{margin:18px 0 0;font-size:12px;color:#6b7280}}
    .ips ul{{margin:4px 0 0 16px;padding:0;line-height:1.8}}
    .change-note{{font-size:12px;color:#9ca3af;margin-top:14px;text-align:center}}
  </style>
</head>
<body>
<div class="card">
  <h1>Arista ZTP Dashboard</h1>
  <p>Choose a working folder. The app will create all required subdirectories and drop the ZTP startup script there.</p>
  <form method="post" action="/setup">
    <label for="wd">Working folder path</label>
    <input id="wd" name="working_dir" type="text"
           value="{html.escape(existing)}" autocomplete="off"/>
    <div class="hint">
      Will create inside this folder:
      <ul>
        <li><code>configs/</code> — device configs named <code>{{serial}}.cfg</code></li>
        <li><code>images/</code> — EOS <code>.swi</code> image files</li>
        <li><code>.data/</code> — dashboard state database</li>
        <li><code>arista-ztp-startup.py</code> — ZTP startup script</li>
      </ul>
    </div>
    <button type="submit">Open Dashboard →</button>
  </form>
  <div class="ips">
    DHCP option 67 will point to:
    <ul>{ip_bullets}</ul>
  </div>
  {"" if not wd else f'<p class="change-note">Currently: <strong>{html.escape(str(wd))}</strong></p>'}
</div>
</body>
</html>"""
            self._html(page)

        def _handle_setup(self, body: bytes) -> None:
            data = urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True)
            raw = (data.get("working_dir", [""])[0] or "").strip()
            if not raw:
                raise ValueError("Working folder path is required.")
            wd = Path(raw).expanduser().resolve()
            eip = _effective_ip()
            _setup_working_dir(wd, eip, port)

            # Write bootstrap file so next launch goes straight to working_dir/.data/
            if bootstrap_file:
                bootstrap_file.parent.mkdir(parents=True, exist_ok=True)
                bootstrap_file.write_text(str(wd), encoding="utf-8")

            # Re-init state into working_dir/.data/ for this session too
            new_data_dir = wd / ".data"
            _reinit_state(new_data_dir)
            st().set_setting("working_dir", str(wd))
            st().set_setting("configs_dir", str(wd / "configs"))
            st().set_setting("eos_dir", str(wd / "images"))
            if eip:
                st().set_setting("selected_ip", eip)

            self._redirect("Working folder ready.")

        def _handle_select_ip(self, body: bytes) -> None:
            data = urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True)
            ip = (data.get("ip", [""])[0] or "").strip()
            if ip not in local_ips:
                raise ValueError(f"Unknown IP: {ip}")
            st().set_setting("selected_ip", ip)
            # Update script immediately with chosen IP
            wd = _working_dir()
            if wd:
                sp = wd / "arista-ztp-startup.py"
                if sp.exists():
                    _check_and_update_script(sp, ip, port)
            self._redirect(f"IP {ip} selected — startup script and DHCP info updated.")

        # ── API ───────────────────────────────────────────────────────────────

        def _api_status(self) -> None:
            wd = _working_dir()
            cd = _configs_dir()
            ed = _eos_dir()
            inventory = _scan_configs(cd)          # .cfg serials only
            cfg_files = _scan_all_files(cd)        # all files in configs dir
            eos_images = _scan_eos(ed)             # .swi only
            eos_files = _scan_all_files(ed)        # all files in images dir

            # EOS version config — auto-set TARGET_VERSION when only one image present
            script_path = (wd / "arista-ztp-startup.py") if wd else None
            eos_auto_status = _maybe_auto_set_eos(script_path, eos_images)
            target_version, eos_by_model = (
                _read_eos_config(script_path)
                if script_path and script_path.exists()
                else ("", {})
            )

            devices = st().all_devices()
            known = {d["serial"] for d in devices}
            for serial in inventory:
                if serial not in known:
                    devices.append({
                        "serial": serial, "model": None, "hostname": None,
                        "eos_current": None, "eos_target": None,
                        "status": None, "message": None, "last_seen": None,
                    })
            for d in devices:
                d["has_config"] = d["serial"] in set(inventory)

            sel_ip = _selected_ip()
            script_status, script_message = _get_script_status(wd, local_ips, port, sel_ip)
            cd_ok = cd is not None and cd.is_dir()
            ed_ok = ed is not None and ed.is_dir()
            ready = (
                wd is not None and wd.exists()
                and cd_ok and ed_ok
                and script_status in ("ok", "updated")
                and (not eos_images or bool(target_version))
            )

            self._json({
                "devices": devices,
                "configs": inventory,
                "configs_files": cfg_files,
                "eos_images": eos_images,
                "eos_files": eos_files,
                "working_dir": str(wd) if wd else "",
                "configs_dir": str(cd) if cd else "",
                "eos_dir": str(ed) if ed else "",
                "configs_dir_exists": cd_ok,
                "eos_dir_exists": ed_ok,
                "script_status": script_status,
                "script_message": script_message,
                "eos_auto_status": eos_auto_status,
                "eos_target_version": target_version,
                "eos_by_model": eos_by_model,
                "ready": ready,
                "server_time": _iso_now(),
            })

        def _receive_device_status(self, body: bytes) -> None:
            ct = self.headers.get("Content-Type", "")
            if "application/json" in ct:
                try:
                    data = json.loads(body.decode("utf-8"))
                except ValueError as exc:
                    self._json({"error": str(exc)}, 400)
                    return
            else:
                parsed = urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True)
                data = {k: v[0] for k, v in parsed.items()}
            serial = (data.get("serial") or "").strip()
            status = (data.get("status") or "unknown").strip()
            if not serial:
                self._json({"error": "serial required"}, 400)
                return
            st().upsert_device(
                serial=serial, status=status,
                model=data.get("model") or None,
                hostname=data.get("hostname") or None,
                eos_current=data.get("eos_current") or None,
                eos_target=data.get("eos_target") or None,
                message=data.get("message") or None,
            )
            self._json({"ok": True})

        def _save_settings(self, body: bytes) -> None:
            data = urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True)
            cd = (data.get("configs_dir", [""])[0] or "").strip()
            ed = (data.get("eos_dir", [""])[0] or "").strip()
            st().set_setting("configs_dir", cd)
            st().set_setting("eos_dir", ed)
            self._redirect("Settings saved.")

        def _save_eos_config(self, body: bytes) -> None:
            wd = _working_dir()
            if not wd:
                raise ValueError("No working folder configured.")
            script_path = wd / "arista-ztp-startup.py"
            if not script_path.exists():
                raise ValueError("Startup script not found in working folder.")
            data = urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True)
            default_image = (data.get("default_image", [""])[0] or "").strip()
            model_keys = [k.strip() for k in data.get("model_key", [])]
            model_images = [v.strip() for v in data.get("model_image", [])]
            eos_by_model = {k: v for k, v in zip(model_keys, model_images) if k and v}
            _write_eos_config(script_path, default_image, eos_by_model)
            self._redirect("EOS version config saved.")

        def _clear_device(self, body: bytes) -> None:
            data = urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True)
            serial = (data.get("serial", [""])[0] or "").strip()
            if serial:
                st().clear_device(serial)
            self._redirect(f"Cleared {serial}.")

        def _shutdown(self) -> None:
            self._html(
                '<!doctype html><html><head><meta charset="utf-8"/><title>ZTP Dashboard</title>'
                "<style>body{font-family:Segoe UI,sans-serif;background:#f5f5f5;display:flex;"
                "align-items:center;justify-content:center;height:100vh;margin:0}"
                ".c{background:#fff;border-radius:10px;padding:40px;text-align:center;"
                "border:1px solid #d1d5db}</style></head><body>"
                '<div class="c"><h2>ZTP Dashboard Stopped</h2><p>Close this window.</p></div>'
                "</body></html>"
            )
            threading.Timer(0.2, self.server.shutdown).start()

        # ── main dashboard page ───────────────────────────────────────────────

        def _page_main(self) -> None:
            query = urllib.parse.urlparse(self.path).query
            message = urllib.parse.parse_qs(query).get("msg", [""])[0]
            wd = _working_dir()
            cd = _configs_dir()
            ed = _eos_dir()
            eip = _effective_ip()
            sel_ip = _selected_ip()

            msg_html = f'<div class="banner">{html.escape(message)}</div>' if message else ""

            # ── IP selector (only when multiple IPs) ──────────────────────────
            if len(local_ips) > 1:
                options_html = ""
                for ip in local_ips:
                    checked = "checked" if ip == sel_ip else ""
                    cur = ' <span class="ip-cur">(selected)</span>' if ip == sel_ip else ""
                    options_html += (
                        f'<label class="ip-opt">'
                        f'<input type="radio" name="ip" value="{html.escape(ip)}" {checked}>'
                        f' {html.escape(ip)}{cur}</label>'
                    )
                prompt = "" if sel_ip else '<p class="ip-prompt">⚠ Select the IP on your ZTP/management network to configure the startup script and DHCP option.</p>'
                ip_section = f"""
  <section class="ip-section">
    <h2>ZTP Network Interface</h2>
    {prompt}
    <form method="post" action="/select-ip" style="display:flex;align-items:flex-end;gap:14px;flex-wrap:wrap">
      <div class="ip-opts">{options_html}</div>
      <button type="submit">Update Startup Script &amp; DHCP</button>
    </form>
  </section>"""
            else:
                ip_section = ""

            # ── DHCP section ──────────────────────────────────────────────────
            if eip:
                dhcp_url = f"http://{eip}:{port}/arista-ztp-startup.py"
                dhcp_html = f"""
  <section>
    <h2>DHCP Server Configuration</h2>
    <p class="dhcp-intro">Add <strong>Option 67 (Boot File Name)</strong> to your DHCP scope.
    Arista switches fetch this URL on first boot to run the ZTP script:</p>
    <div class="dhcp-block">
      <span class="dhcp-label">Option 67</span>
      <span class="dhcp-url" id="dhcp-url">{html.escape(dhcp_url)}</span>
      <button class="copy-btn" type="button" onclick="copyUrl()">Copy</button>
    </div>
    <div class="dhcp-examples">
      <div><strong>ISC / Kea DHCP</strong>
        <pre>option bootfile-name "{html.escape(dhcp_url)}";</pre></div>
      <div><strong>Windows DHCP Server</strong>
        <pre>Option 067 (Boot File Name):
{html.escape(dhcp_url)}</pre></div>
    </div>
  </section>"""
            else:
                dhcp_html = """
  <section>
    <h2>DHCP Server Configuration</h2>
    <p style="color:#d97706">⚠ Select a ZTP network interface above to see the DHCP option 67 value.</p>
  </section>"""

            status_meta_json = json.dumps(
                {k: {"label": v[0], "color": v[1], "pulse": v[2]} for k, v in STATUS_META.items()}
            )

            # EOS version assignment section
            eos_images_list = _scan_eos(ed)
            script_path_p = (wd / "arista-ztp-startup.py") if wd else None
            eos_target, eos_model_map = (
                _read_eos_config(script_path_p)
                if script_path_p and script_path_p.exists()
                else ("", {})
            )

            def _img_options(images: list[str], selected: str) -> str:
                opts = ['<option value="">— select —</option>']
                all_imgs = list(images)
                if selected and selected not in images:
                    all_imgs.append(selected)
                for img in all_imgs:
                    s = " selected" if img == selected else ""
                    label = (f"{html.escape(img)} ⚠ missing"
                             if img == selected and img not in images
                             else html.escape(img))
                    opts.append(f'<option value="{html.escape(img)}"{s}>{label}</option>')
                return "".join(opts)

            if eos_images_list or eos_model_map:
                _def_row = (
                    f'<tr><td><span class="eos-role eos-role-def">Default</span></td>'
                    f'<td><span class="eos-all-models">All models (global fallback)</span></td>'
                    f'<td><select name="default_image" class="eos-sel">'
                    f'{_img_options(eos_images_list, eos_target)}</select></td>'
                    f'<td></td></tr>'
                )
                _ovr_rows = "".join(
                    f'<tr class="eos-row">'
                    f'<td><span class="eos-role eos-role-model">Model Override</span></td>'
                    f'<td><input type="text" name="model_key" class="eos-inp"'
                    f' value="{html.escape(mk)}" placeholder="e.g. 7260CX3"/></td>'
                    f'<td><select name="model_image" class="eos-sel">'
                    f'{_img_options(eos_images_list, mi)}</select></td>'
                    f'<td><button type="button" class="secondary eos-rm"'
                    f' onclick="this.closest(\'tr\').remove()">✕</button></td></tr>'
                    for mk, mi in eos_model_map.items()
                )
                eos_section_html = f"""
  <section>
    <h2>EOS Version Assignment</h2>
    <p class="eos-intro">The <strong>Default</strong> image sets <code>TARGET_VERSION</code>
    and applies to any model without a specific override. Model patterns are matched as
    substrings against the model string from <code>show version</code>
    (e.g. <code>7260CX3</code> matches <code>Arista DCS-7260CX3-64</code>).</p>
    <form method="post" action="/save-eos-config">
      <div class="tbl-wrap">
        <table class="eos-tbl">
          <thead><tr>
            <th style="width:130px">Role</th>
            <th>Model Pattern</th>
            <th>EOS Image</th>
            <th style="width:38px"></th>
          </tr></thead>
          <tbody id="eos-rows">
            {_def_row}
            {_ovr_rows}
          </tbody>
        </table>
      </div>
      <div style="margin-top:10px;display:flex;gap:8px">
        <button type="button" class="secondary" onclick="addEosRow()">+ Add Model Override</button>
        <button type="submit">Save</button>
      </div>
    </form>
  </section>"""
            else:
                eos_section_html = ""

            eos_images_json = json.dumps(eos_images_list)

            # Current data_dir (updates after setup re-init)
            current_data_dir = data_dir_ref[0]

            page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Arista ZTP Dashboard</title>
  <style>
    *{{box-sizing:border-box}}
    body{{margin:0;font-family:Segoe UI,system-ui,sans-serif;background:#f0f2f5;color:#111}}
    main{{max-width:1100px;margin:0 auto;padding:22px 18px}}
    h1{{margin:0 0 3px;font-size:21px;font-weight:700}}
    h2{{margin:0 0 12px;font-size:11px;font-weight:700;text-transform:uppercase;
        letter-spacing:.06em;color:#374151}}
    section{{background:#fff;border:1px solid #d1d5db;border-radius:10px;
             padding:16px 18px;margin:12px 0}}
    label{{display:block;font-size:13px;font-weight:600;margin-bottom:4px}}
    input[type=text]{{width:100%;border:1px solid #d1d5db;border-radius:6px;
                     padding:8px 10px;font:inherit;font-size:13px}}
    button{{border:0;border-radius:6px;background:#0170C5;color:#fff;padding:8px 14px;
            font:inherit;font-size:13px;font-weight:700;cursor:pointer}}
    button.danger{{background:#b91c1c}}
    button.secondary{{background:#f3f4f6;color:#374151;border:1px solid #d1d5db}}
    button:hover{{opacity:.88}}
    .top{{display:flex;align-items:flex-start;justify-content:space-between;
          gap:12px;flex-wrap:wrap;margin-bottom:4px}}
    .top-actions{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding-top:4px}}
    .ips{{font-size:11px;color:#6b7280;margin-top:3px}}
    .ips code{{background:#f3f4f6;border-radius:3px;padding:1px 5px;
               font-family:Consolas,monospace;font-size:11px}}
    .banner{{background:rgba(1,112,197,.1);border:1px solid rgba(1,112,197,.3);
             color:#0170C5;border-radius:8px;padding:9px 14px;margin:8px 0;
             font-size:13px;font-weight:600}}
    /* status bar */
    .status-bar{{display:flex;flex-wrap:wrap;gap:8px;align-items:center;
                 padding:11px 16px;background:#fff;border:1px solid #d1d5db;
                 border-radius:10px;margin:12px 0;min-height:46px}}
    .ready-badge{{font-size:13px;font-weight:800;padding:4px 12px;border-radius:999px;
                  margin-right:2px;white-space:nowrap}}
    .si{{font-size:12px;font-weight:600;display:inline-flex;align-items:center;
         gap:5px;padding:4px 10px;border-radius:999px;white-space:nowrap}}
    .si a{{color:inherit;font-weight:700;margin-left:4px;text-decoration:none}}
    .ok{{background:#dcfce7;color:#15803d}}
    .warn{{background:#fef3c7;color:#92400e}}
    .bad{{background:#fee2e2;color:#b91c1c}}
    .neutral{{background:#f3f4f6;color:#374151}}
    /* IP selector */
    .ip-section{{border-color:#fbbf24;background:#fffbeb}}
    .ip-prompt{{font-size:13px;color:#92400e;margin:0 0 12px;font-weight:600}}
    .ip-opts{{display:flex;gap:16px;flex-wrap:wrap}}
    .ip-opt{{display:inline-flex;align-items:center;gap:7px;font-size:14px;
             font-weight:600;cursor:pointer;padding:6px 12px;border-radius:7px;
             border:2px solid #e5e7eb;background:#fff}}
    .ip-opt:has(input:checked){{border-color:#0170C5;background:#eff6ff}}
    .ip-cur{{font-size:11px;color:#0170C5;font-weight:700}}
    /* DHCP */
    .dhcp-intro{{font-size:13px;color:#374151;margin:0 0 12px}}
    .dhcp-block{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px}}
    .dhcp-label{{font-size:11px;font-weight:700;text-transform:uppercase;
                 color:#6b7280;white-space:nowrap}}
    .dhcp-url{{font-family:Consolas,monospace;font-size:13px;background:#f3f4f6;
               border:1px solid #e5e7eb;border-radius:6px;padding:7px 12px;
               flex:1;min-width:0;word-break:break-all}}
    .copy-btn{{background:#f3f4f6;color:#374151;border:1px solid #d1d5db;
               font-size:12px;padding:6px 12px;font-weight:600;flex-shrink:0}}
    .dhcp-examples{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
    .dhcp-examples pre{{margin:4px 0 0;background:#1e293b;color:#e2e8f0;
                        border-radius:6px;padding:10px 12px;font-size:11px;
                        font-family:Consolas,monospace;white-space:pre-wrap;line-height:1.5}}
    /* device table */
    .tbl-wrap{{overflow-x:auto}}
    table{{width:100%;border-collapse:collapse;font-size:13px}}
    th{{text-align:left;padding:7px 10px;background:#f9fafb;
        border-bottom:2px solid #e5e7eb;font-size:10px;font-weight:700;
        text-transform:uppercase;letter-spacing:.06em;color:#374151;white-space:nowrap}}
    td{{padding:8px 10px;border-bottom:1px solid #f3f4f6;vertical-align:middle}}
    tr:last-child td{{border-bottom:0}}
    tr:hover td{{background:#fafafa}}
    .badge{{display:inline-flex;align-items:center;gap:5px;padding:3px 9px;
            border-radius:999px;font-size:11px;font-weight:700;white-space:nowrap}}
    .dot{{width:7px;height:7px;border-radius:50%;flex-shrink:0}}
    .dot.pulse{{animation:pulse 1.2s ease-in-out infinite}}
    .eos-cell{{font-size:12px;font-family:Consolas,monospace}}
    .arrow{{color:#9ca3af;margin:0 3px}}
    .cfg-yes{{color:#15803d;font-weight:700}}
    .cfg-no{{color:#d1d5db}}
    .ts{{font-size:11px;color:#9ca3af;white-space:nowrap}}
    .none{{text-align:center;padding:36px;color:#9ca3af;font-size:14px}}
    .rbar{{display:flex;align-items:center;gap:10px;font-size:12px;color:#6b7280;margin-bottom:10px}}
    .rdot{{width:7px;height:7px;border-radius:50%;background:#15803d}}
    .rdot.stale{{background:#d97706;animation:pulse 1s ease-in-out infinite}}
    /* file chips */
    .chips{{display:flex;flex-wrap:wrap;gap:5px;margin-top:6px;min-height:24px}}
    .chip{{background:#f3f4f6;border:1px solid #e5e7eb;border-radius:4px;padding:2px 8px;
           font-size:11px;font-family:Consolas,monospace;color:#374151}}
    .chip-dim{{background:#fafafa;border-color:#f3f4f6;color:#9ca3af}}
    .chip-empty{{font-size:12px;color:#9ca3af;font-style:italic}}
    /* settings */
    .sg{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}}
    .dir-hint{{font-size:11px;margin-top:4px;min-height:14px}}
    .dir-ok{{color:#15803d}}
    .dir-warn{{color:#92400e}}
    .dir-bad{{color:#b91c1c}}
    .data-note{{font-size:12px;color:#9ca3af;margin-top:12px;padding-top:12px;
                border-top:1px solid #f3f4f6}}
    .data-note code{{background:#f3f4f6;border-radius:3px;padding:1px 5px;
                     font-size:11px;font-family:Consolas,monospace}}
    /* EOS version assignment */
    .eos-tbl{{width:100%;border-collapse:collapse;font-size:13px}}
    .eos-tbl th{{text-align:left;padding:7px 10px;background:#f9fafb;
                border-bottom:2px solid #e5e7eb;font-size:10px;font-weight:700;
                text-transform:uppercase;letter-spacing:.06em;color:#374151}}
    .eos-tbl td{{padding:6px 10px;border-bottom:1px solid #f3f4f6;vertical-align:middle}}
    .eos-tbl tr:last-child td{{border-bottom:0}}
    .eos-role{{font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px;white-space:nowrap}}
    .eos-role-def{{background:#eff6ff;color:#1d4ed8}}
    .eos-role-model{{background:#f3f4f6;color:#374151}}
    .eos-sel{{border:1px solid #d1d5db;border-radius:6px;padding:5px 8px;font:inherit;
              font-size:13px;width:100%;background:#fff}}
    .eos-inp{{border:1px solid #d1d5db;border-radius:6px;padding:5px 8px;font:inherit;
              font-size:13px;width:100%}}
    .eos-rm{{padding:3px 8px;font-size:12px}}
    .eos-all-models{{font-size:12px;color:#9ca3af;font-style:italic}}
    .eos-intro{{font-size:13px;color:#374151;margin:0 0 14px;line-height:1.55}}
    @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
    @media(max-width:700px){{.sg,.dhcp-examples{{grid-template-columns:1fr}}main{{padding:12px}}}}
  </style>
</head>
<body>
<main>
  <div class="top">
    <div>
      <h1>Arista ZTP Dashboard</h1>
      <div class="ips">
        {"".join(f'<code>http://{ip}:{port}</code>&nbsp;' for ip in local_ips)}
        &nbsp;·&nbsp;<a href="/setup" style="font-size:11px;color:#6b7280">Change Folder</a>
      </div>
    </div>
    <div class="top-actions">
      <form method="post" action="/clear-all"
            onsubmit="return confirm('Clear all device history?')">
        <button class="secondary" type="submit">Clear History</button>
      </form>
      <form method="post" action="/shutdown">
        <button class="danger" type="submit">Exit</button>
      </form>
    </div>
  </div>

  {msg_html}

  <div class="status-bar" id="status-bar">
    <span style="color:#9ca3af;font-size:13px">Loading…</span>
  </div>

  {ip_section}

  <section>
    <div class="rbar">
      <div class="rdot" id="rdot"></div>
      <span>Updated <span id="rts">—</span></span>
      <span id="dcount" style="margin-left:auto;font-weight:600"></span>
    </div>
    <div class="tbl-wrap">
      <table>
        <thead>
          <tr>
            <th>Serial</th><th title="Config file present">CFG</th>
            <th>Model</th><th>Hostname</th><th>EOS</th>
            <th>Status</th><th>Message</th><th>Last Seen</th><th></th>
          </tr>
        </thead>
        <tbody id="tbody"><tr><td colspan="9" class="none">Loading…</td></tr></tbody>
      </table>
    </div>
  </section>

  <section>
    <h2>File Server
      <span id="fs-note" style="text-transform:none;font-weight:400;color:#6b7280;font-size:12px;margin-left:6px"></span>
    </h2>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
      <div>
        <div style="font-size:12px;font-weight:600;color:#374151">
          Device Configs &nbsp;<span id="cfg-ct" style="font-weight:400;color:#6b7280"></span>
        </div>
        <div id="cfg-chips" class="chips"></div>
        <div class="dir-hint" id="cfg-hint"></div>
      </div>
      <div>
        <div style="font-size:12px;font-weight:600;color:#374151">
          EOS Images &nbsp;<span id="eos-ct" style="font-weight:400;color:#6b7280"></span>
        </div>
        <div id="eos-chips" class="chips"></div>
        <div class="dir-hint" id="eos-hint"></div>
      </div>
    </div>
  </section>

  {eos_section_html}

  {dhcp_html}

  <section>
    <h2>Settings</h2>
    <form method="post" action="/save-settings">
      <div class="sg">
        <div>
          <label>Device configs directory</label>
          <input type="text" name="configs_dir"
                 value="{html.escape(str(cd) if cd else '')}"
                 placeholder="{html.escape(str(wd / 'configs') if wd else '')}"/>
          <div class="dir-hint" id="cfg-setting-hint"></div>
        </div>
        <div>
          <label>EOS images directory</label>
          <input type="text" name="eos_dir"
                 value="{html.escape(str(ed) if ed else '')}"
                 placeholder="{html.escape(str(wd / 'images') if wd else '')}"/>
          <div class="dir-hint" id="eos-setting-hint"></div>
        </div>
      </div>
      <button type="submit">Save</button>
    </form>
    <div class="data-note">
      Working folder: <code>{html.escape(str(wd) if wd else "—")}</code>
      &nbsp;·&nbsp;
      App data: <code>{html.escape(str(current_data_dir))}</code>
    </div>
  </section>
</main>
<script>
const SM = {status_meta_json};

function badge(s) {{
  const m = SM[s] || SM.unknown;
  return `<span class="badge" style="background:${{m.color}}18;color:${{m.color}}">` +
    `<span class="dot${{m.pulse?' pulse':''}}" style="background:${{m.color}}"></span>${{m.label}}</span>`;
}}
function esc(s) {{
  return s==null?'':String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}
function ts(iso) {{
  if(!iso) return '<span class="ts">—</span>';
  const d=new Date(iso);
  return `<span class="ts" title="${{d.toISOString()}}">${{d.toLocaleTimeString([],{{hour:'2-digit',minute:'2-digit',second:'2-digit'}})}}</span>`;
}}
function eosCell(c,t) {{
  if(!c&&!t) return '<span class="ts">—</span>';
  if(!t||c===t) return `<span class="eos-cell">${{esc(c||t)}}</span>`;
  return `<span class="eos-cell">${{esc(c||'?')}}<span class="arrow">→</span>${{esc(t)}}</span>`;
}}
function fileChip(name, validExt) {{
  const ok = name.toLowerCase().endsWith(validExt);
  return ok
    ? `<span class="chip">${{esc(name)}}</span>`
    : `<span class="chip chip-dim" title="Not a valid ${{validExt}} file">${{esc(name)}}</span>`;
}}

function buildStatusBar(d) {{
  const parts = [];
  if(d.ready)
    parts.push('<span class="ready-badge" style="background:#dcfce7;color:#15803d">● Ready</span>');
  else
    parts.push('<span class="ready-badge" style="background:#fee2e2;color:#b91c1c">⚠ Not Ready</span>');

  const ss = d.script_status;
  if(ss==='ok'||ss==='updated') {{
    const lbl = ss==='updated'?'Startup Script Updated':'Startup Script Ready';
    parts.push(`<span class="si ok">✓ ${{lbl}}<a href="/startup-script" target="_blank">↗</a></span>`);
  }} else if(ss==='needs_select') {{
    parts.push('<span class="si warn">⚠ Select IP above</span>');
  }} else {{
    parts.push(`<span class="si bad" title="${{esc(d.script_message)}}">✗ Script Not Found</span>`);
  }}

  const nc = (d.configs||[]).length;
  const nf = (d.configs_files||[]).length;
  if(!d.configs_dir_exists)
    parts.push('<span class="si bad">✗ Configs Folder Missing</span>');
  else if(nc===0&&nf>0)
    parts.push(`<span class="si warn">⚠ No .cfg Configs (${{nf}} other file${{nf!==1?'s':''}})</span>`);
  else if(nc===0)
    parts.push('<span class="si warn">⚠ No Device Configs</span>');
  else
    parts.push(`<span class="si ok">✓ ${{nc}} Config${{nc!==1?'s':''}}</span>`);

  const ne = (d.eos_images||[]).length;
  const ef = (d.eos_files||[]).length;
  if(!d.eos_dir_exists)
    parts.push('<span class="si bad">✗ Images Folder Missing</span>');
  else if(ne===0&&ef>0)
    parts.push(`<span class="si warn">⚠ No .swi Images (${{ef}} other file${{ef!==1?'s':''}})</span>`);
  else if(ne===0)
    parts.push('<span class="si warn">⚠ No EOS Images</span>');
  else
    parts.push(`<span class="si ok">✓ ${{ne}} EOS Image${{ne!==1?'s':''}}</span>`);

  if(d.eos_auto_status==='auto_set')
    parts.push(`<span class="si ok">✓ EOS Target Auto-Set → ${{esc(d.eos_target_version)}}</span>`);
  else if(d.eos_auto_status==='multi'&&!d.eos_target_version)
    parts.push('<span class="si warn">⚠ EOS Default Not Set</span>');
  else if(d.eos_target_version&&ne>0)
    parts.push(`<span class="si ok">✓ EOS Default: ${{esc(d.eos_target_version)}}</span>`);

  return parts.join('');
}}

function buildRows(devs) {{
  if(!devs||!devs.length)
    return '<tr><td colspan="9" class="none">No devices yet — waiting for ZTP activity…</td></tr>';
  return devs.map(d=>`<tr>
    <td style="font-family:Consolas,monospace;font-size:12px">${{esc(d.serial)}}</td>
    <td style="text-align:center">${{d.has_config?'<span class="cfg-yes">✓</span>':'<span class="cfg-no">—</span>'}}</td>
    <td style="font-size:12px">${{esc(d.model)||'<span class="ts">—</span>'}}</td>
    <td>${{esc(d.hostname)||'<span class="ts">—</span>'}}</td>
    <td>${{eosCell(d.eos_current,d.eos_target)}}</td>
    <td>${{d.status?badge(d.status):'<span class="ts">Waiting</span>'}}</td>
    <td style="font-size:12px;color:#6b7280;max-width:190px">${{esc(d.message)||''}}</td>
    <td>${{ts(d.last_seen)}}</td>
    <td><form method="post" action="/clear-device" style="margin:0">
      <input type="hidden" name="serial" value="${{esc(d.serial)}}"/>
      <button class="secondary" type="submit" style="padding:3px 8px;font-size:11px"
        onclick="return confirm('Clear '+JSON.stringify(d.serial)+'?')">Clear</button>
    </form></td>
  </tr>`).join('');
}}

function setHint(id, cls, text) {{
  const el = document.getElementById(id);
  if(el){{ el.className='dir-hint '+cls; el.textContent=text; }}
}}

let stale=0;
async function refresh() {{
  try {{
    const r = await fetch('/api/status');
    const d = await r.json();

    document.getElementById('tbody').innerHTML = buildRows(d.devices);
    document.getElementById('status-bar').innerHTML = buildStatusBar(d);

    const n = (d.devices||[]).length;
    document.getElementById('dcount').textContent = n===1?'1 device':n+' devices';

    // File chips — all files, dim non-valid ones
    const cfgFiles = d.configs_files||[], cfgValid = d.configs||[];
    const eosFiles = d.eos_files||[], eosValid = d.eos_images||[];

    document.getElementById('cfg-ct').textContent = cfgFiles.length?`(${{cfgFiles.length}})`:''
    document.getElementById('eos-ct').textContent = eosFiles.length?`(${{eosFiles.length}})`:''
    document.getElementById('cfg-chips').innerHTML = cfgFiles.length
      ? cfgFiles.map(f=>fileChip(f,'.cfg')).join('')
      : '<span class="chip-empty">No files</span>';
    document.getElementById('eos-chips').innerHTML = eosFiles.length
      ? eosFiles.map(f=>fileChip(f,'.swi')).join('')
      : '<span class="chip-empty">No files</span>';

    // Dir hints
    const nc=cfgValid.length, nf=cfgFiles.length;
    const ne=eosValid.length, ef=eosFiles.length;

    if(!d.configs_dir_exists)
      setHint('cfg-hint','dir-bad','Directory not found');
    else if(nc>0)
      setHint('cfg-hint','dir-ok',`✓ ${{nc}} .cfg device config${{nc!==1?'s':''}}`);
    else if(nf>0)
      setHint('cfg-hint','dir-warn',`Directory found — ${{nf}} file${{nf!==1?'s':''}} but no .cfg device configs`);
    else
      setHint('cfg-hint','dir-warn','Directory exists but is empty');

    if(!d.eos_dir_exists)
      setHint('eos-hint','dir-bad','Directory not found');
    else if(ne>0)
      setHint('eos-hint','dir-ok',`✓ ${{ne}} EOS image${{ne!==1?'s':''}}`);
    else if(ef>0)
      setHint('eos-hint','dir-warn',`Directory found — ${{ef}} file${{ef!==1?'s':''}} but no .swi EOS images`);
    else
      setHint('eos-hint','dir-warn','Directory exists but is empty');

    // Settings hints
    if(!d.configs_dir_exists)
      setHint('cfg-setting-hint','dir-bad','Directory not found');
    else if(nc>0)
      setHint('cfg-setting-hint','dir-ok',`✓ ${{nc}} .cfg file${{nc!==1?'s':''}} found`);
    else if(nf>0)
      setHint('cfg-setting-hint','dir-warn',`${{nf}} file${{nf!==1?'s':''}} found — none are .cfg`);
    else
      setHint('cfg-setting-hint','dir-warn','Empty directory');

    if(!d.eos_dir_exists)
      setHint('eos-setting-hint','dir-bad','Directory not found');
    else if(ne>0)
      setHint('eos-setting-hint','dir-ok',`✓ ${{ne}} .swi image${{ne!==1?'s':''}} found`);
    else if(ef>0)
      setHint('eos-setting-hint','dir-warn',`${{ef}} file${{ef!==1?'s':''}} found — none are .swi`);
    else
      setHint('eos-setting-hint','dir-warn','Empty directory');

    document.getElementById('rts').textContent = new Date().toLocaleTimeString();
    document.getElementById('rdot').className = 'rdot';
    stale = 0;
  }} catch(e) {{
    if(++stale>2) document.getElementById('rdot').className='rdot stale';
  }}
}}

refresh();
setInterval(refresh, 3000);

function copyUrl() {{
  const el = document.getElementById('dhcp-url');
  if(el) navigator.clipboard.writeText(el.textContent).catch(()=>{{}});
}}

const EOS_IMAGES = {eos_images_json};
function addEosRow() {{
  const tbody = document.getElementById('eos-rows');
  if(!tbody) return;
  let opts = '<option value="">— select —</option>';
  for(const img of EOS_IMAGES) opts += `<option value="${{esc(img)}}">${{esc(img)}}</option>`;
  const tr = document.createElement('tr');
  tr.className = 'eos-row';
  tr.innerHTML = `
    <td><span class="eos-role eos-role-model">Model Override</span></td>
    <td><input type="text" name="model_key" class="eos-inp" placeholder="e.g. 7260CX3"/></td>
    <td><select name="model_image" class="eos-sel">${{opts}}</select></td>
    <td><button type="button" class="secondary eos-rm" onclick="this.closest('tr').remove()">✕</button></td>`;
  tbody.appendChild(tr);
}}
</script>
</body>
</html>"""
            self._html(page)

    server = ThreadingHTTPServer((host, port), Handler)
    display = "127.0.0.1" if host == "0.0.0.0" else host
    url = f"http://{display}:{port}"
    print(f"Arista ZTP Dashboard  →  {url}")
    print(f"Local IP(s): {', '.join(local_ips)}")
    print("Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("ZTP Dashboard stopped.")
