from __future__ import annotations

import argparse
import os
import socket
from pathlib import Path

from ztp_dashboard.ui import run_ui


def _bootstrap_file() -> Path:
    """Tiny pointer file — stores working dir path between launches."""
    if os.name == "nt":
        base = Path(os.getenv("LOCALAPPDATA") or Path.home())
        return base / "AristaZTP" / "wdir.txt"
    return Path.home() / ".arista-ztp"


def resolve_data_dir() -> Path:
    """
    If we already know the working dir, use working_dir/.data/ so all app files
    live together. Otherwise fall back to a sibling directory of the bootstrap file.
    """
    bf = _bootstrap_file()
    if bf.exists():
        try:
            wd = Path(bf.read_text(encoding="utf-8").strip())
            if wd.is_dir():
                data = wd / ".data"
                data.mkdir(parents=True, exist_ok=True)
                return data
        except Exception:
            pass
    # First-run fallback — keep it next to the bootstrap file
    fallback = bf.parent / ".data"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def find_available_port(start: int) -> int:
    for port in range(start, start + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No available port found in range {start}–{start + 99}.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Arista ZTP provisioning dashboard.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8090,
                        help="Starting port — increments until one is free (default: 8090)")
    parser.add_argument("--no-open-browser", action="store_true")
    args = parser.parse_args()

    port = find_available_port(args.port)
    if port != args.port:
        print(f"Port {args.port} in use — using port {port}.")

    run_ui(
        data_dir=resolve_data_dir(),
        host=args.host,
        port=port,
        open_browser=not args.no_open_browser,
        bootstrap_file=_bootstrap_file(),
    )


if __name__ == "__main__":
    main()
