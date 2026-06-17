# Arista ZTP Dashboard

A self-contained web dashboard for managing Arista switch Zero Touch Provisioning (ZTP). It acts as the file server, status tracker, and control plane for your ZTP deployment — no separate web server or database required.

Distributes as a single executable (`.exe` on Windows, binary on Linux) built from pure Python stdlib. No external dependencies.

---

## How ZTP Works

1. A switch boots with no config and gets an IP via DHCP.
2. DHCP **option 67** (Boot File Name) points the switch to a URL — the startup script served by this dashboard.
3. EOS fetches and runs the startup script, which reports progress back to the dashboard at each stage.
4. The startup script checks the current EOS version, upgrades if needed (switch reloads), then fetches and applies the per-device config by serial number.
5. The dashboard shows real-time provisioning state for every device.

---

## Quick Start — Running from Source

Requires Python 3.11 or newer.

```bash
git clone git@github.com:miketbanks615-ctrl/ztp-dashboard.git
cd ztp-dashboard
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .
python -m ztp_dashboard
```

The app opens a browser automatically. On first run it shows a setup page to choose your working folder.

### CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `--port N` | `8090` | Starting port; auto-increments if in use |
| `--host H` | `0.0.0.0` | Bind address |
| `--no-open-browser` | — | Skip auto-opening the browser |

---

## First-Run Setup

On first launch the app shows a **Setup** page. Enter an absolute path to your working folder (it will be created if it doesn't exist) and click **Initialize**.

The app creates this structure under your chosen folder:

```
/your-working-folder/
├── configs/                 # Per-device config files  (<serial>.cfg)
├── images/                  # EOS image files          (*.swi)
├── arista-ztp-startup.py    # Startup script (auto-updated with your IP/port)
└── .data/                   # App state DB (SQLite) — excluded from git
```

The working folder path is saved to a tiny bootstrap file so the app remembers it on next launch:

- **Linux:** `~/.arista-ztp`
- **Windows:** `%LOCALAPPDATA%\AristaZTP\wdir.txt`

---

## Dashboard Features

### IP / Port Detection
The app detects all local IP addresses. If there is only one, it auto-updates `FILE_SERVER` and `DASHBOARD_URL` in the startup script and shows a green **Startup Script Updated** indicator. If multiple IPs are found, an amber selector panel appears — pick the one reachable by the switches.

### DHCP Configuration
Once an IP is selected, the dashboard shows the DHCP option 67 value your switches need, with ready-to-paste examples for both ISC DHCP and Windows DHCP Server, plus a copy button.

**ISC DHCP** (`/etc/dhcp/dhcpd.conf`):
```
option bootfile-name "http://<ip>:<port>/arista-ztp-startup.py";
```

**Windows DHCP Server:**
Set scope option 067 (Boot File Name) to:
```
http://<ip>:<port>/arista-ztp-startup.py
```

### Status Bar
The top-right status bar shows:
- **Ready** badge (green) — all checks pass: script updated, configs present, at least one EOS image
- **Configs** — count of `.cfg` files found in `configs/`; non-`.cfg` files shown dimmed
- **EOS Images** — count of `.swi` files found in `images/`
- **Startup Script** — updated / needs selection / not found

### Device Table
Shows every device that has checked in, updated every 3 seconds:

| Column | Description |
|--------|-------------|
| Serial | Switch serial number |
| Model | Hardware model from `show version` |
| Status | Current provisioning stage |
| EOS | Current → target version |
| Config | Config file applied |
| Message | Detail from the startup script |
| Last Seen | Time of last status update |

---

## Device Status Stages

| Status | Meaning |
|--------|---------|
| `starting` | Startup script has begun |
| `eos_checking` | Comparing current EOS to target |
| `eos_upgrading` | Downloading image; device will reload |
| `eos_ok` | EOS version matches target |
| `config_applying` | Fetching and applying `<serial>.cfg` |
| `complete` | Provisioning finished successfully |
| `error` | An error occurred (message column has details) |

---

## Config Files

Create one file per switch named `<serial>.cfg` in the `configs/` folder. The serial number must match exactly what `show version` reports on the switch (e.g. `JPE12345678.cfg`).

The startup script fetches the config via HTTP — `http://<ip>:<port>/configs/<serial>.cfg` — so the file just needs to be present before the switch reaches the `config_applying` stage.

---

## EOS Images

Place `.swi` image files in the `images/` folder. The dashboard serves them at `http://<ip>:<port>/images/<filename>`.

The startup script's `TARGET_VERSION` must match the filename exactly. For per-model image selection, populate `EOS_BY_MODEL` in `arista-ztp-startup.py`:

```python
EOS_BY_MODEL = {
    '7260CX3': 'EOS64-4.28.3M.swi',
    '720XPM':  'EOS-4.28.3M.swi',
}
```

The model key is matched as a substring of the model string from `show version` (e.g. `Arista DCS-7260CX3-64` matches key `7260CX3`). If no key matches, `TARGET_VERSION` is used.

---

## Building the Binary

### Linux

Requires Python 3.11+ and a shell. Produces `dist/AristaZTPDashboard-linux`.

```bash
chmod +x build-linux-binary.sh
./build-linux-binary.sh
```

Copy the binary to any Linux machine — no Python required.

### Windows

Requires Python 3.11+ and PowerShell. Produces `dist\AristaZTPDashboard.exe` and also copies it to `%USERPROFILE%\Downloads\`.

```powershell
.\build-windows-exe.ps1
```

The Windows build uses `--windowed` so no console window appears when double-clicking the `.exe`.

---

## Startup Script Reference

`arista-ztp-startup.py` runs on the switch via EOS ZTP. Key configuration variables at the top of the file:

```python
FILE_SERVER   = "http://192.168.1.50"     # Base URL — updated automatically by the dashboard
DASHBOARD_URL = "http://192.168.1.50:8090" # Dashboard URL — updated automatically
TARGET_VERSION = "EOS-4.28.3M.swi"        # Global fallback EOS image
IMAGE_PATH    = "/images"                  # Path for EOS images under FILE_SERVER
CONFIG_PATH   = "/configs"                 # Path for config files under FILE_SERVER
```

`FILE_SERVER` and `DASHBOARD_URL` are kept in sync by the dashboard whenever the working folder is initialized or a new IP is selected. The file is served directly by the dashboard at `/arista-ztp-startup.py`.

Status is POSTed to `DASHBOARD_URL/api/device-status` as JSON. All POSTs are best-effort — a network failure never aborts provisioning.

---

## File Serving

The dashboard IS the file server. No Apache, nginx, or IIS required. It serves:

| URL | Content |
|-----|---------|
| `/arista-ztp-startup.py` | Startup script (from working folder) |
| `/startup-script` | Same file, alternate URL |
| `/configs/<file>` | Any file in the `configs/` directory |
| `/images/<file>` | Any file in the `images/` directory (chunked for large `.swi` files) |

Path traversal is blocked — requests cannot escape the working folder.
