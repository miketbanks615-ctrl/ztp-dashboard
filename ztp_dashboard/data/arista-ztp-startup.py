#!/usr/bin/env python3
"""
Arista EOS Zero Touch Provisioning Startup Script
- Per-model EOS image selection with global fallback
- Serial-number based config apply
- Real-time status reporting to ZTP Dashboard
"""

import json
import re
import sys
import subprocess
import urllib.request

# ============================================================
# GLOBAL SETTINGS — apply to all switches unless overridden
# ============================================================
FILE_SERVER     = "http://192.168.1.50"   # Base URL for images and configs
TARGET_VERSION  = "EOS-4.28.3M.swi"      # Global fallback image
IMAGE_PATH      = "/images"              # Path for EOS images
CONFIG_PATH     = "/configs"             # Path for configuration files

# ZTP Dashboard URL — set to "" to disable status reporting
DASHBOARD_URL   = "http://192.168.1.50:8090"

# ============================================================
# PER-MODEL OVERRIDES — model string matched against 'show version'
# Leave empty dict to use global for everything: {}
# Model key is matched as a substring of the Arista model line
# e.g. "Arista DCS-7260CX3-64" → key "7260CX3"
# ============================================================
EOS_BY_MODEL = {
#    '7260CX3': 'EOS64-4.28.3M.swi',    # 64-bit image for core switches
#    '720XPM':  'EOS-4.28.3M.swi',
#    '720DP':   'EOS-4.28.3M.swi',
#    '710XP':   'EOS-4.28.3M.swi',
}
# ============================================================


def cli(cmd):
    """Run an EOS CLI command, return output as string."""
    proc = subprocess.run(
        ['FastCli', '-c', cmd],
        capture_output=True, text=True
    )
    return proc.stdout


def get_current_version():
    out = cli('show version | grep "Software image version"')
    m = re.search(r'Software image version:\s+(\S+)', out)
    return m.group(1) if m else None


def get_serial():
    out = cli('show version | grep "Serial number"')
    m = re.search(r'Serial number:\s+(\S+)', out)
    return m.group(1) if m else None


def get_model():
    """Returns model portion from 'show version', e.g. 'DCS-7260CX3-64'."""
    out = cli('show version | grep "^Arista"')
    parts = out.strip().split()
    return parts[1] if len(parts) >= 2 else None


def resolve_target_version(model):
    """
    Return the correct EOS image for this model.
    Matches model string as substring against EOS_BY_MODEL keys.
    Falls back to global TARGET_VERSION if no match found.
    """
    if model and EOS_BY_MODEL:
        for key, image in EOS_BY_MODEL.items():
            if key in model:
                print(f"Model match '{key}' → using image: {image}")
                return image
    print(f"No model-specific override → using global fallback: {TARGET_VERSION}")
    return TARGET_VERSION


def post_status(serial, status, **fields):
    """Report ZTP progress to the dashboard. Silently ignores all errors."""
    if not DASHBOARD_URL or not serial:
        return
    try:
        payload = json.dumps({"serial": serial, "status": status, **fields}).encode("utf-8")
        req = urllib.request.Request(
            f"{DASHBOARD_URL}/api/device-status",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def upgrade_eos(target, serial, model):
    image_url = f"{FILE_SERVER}{IMAGE_PATH}/{target}"
    print(f"Downloading {target} ...")
    post_status(serial, "eos_upgrading", model=model,
                message=f"Downloading {target} — device will reload")
    result = cli(f'copy {image_url} flash:{target}')
    print(result)
    print("Setting boot image...")
    cli(f'boot system flash:{target}')
    cli('write memory')
    print("Reloading to apply new EOS...")
    post_status(serial, "eos_upgrading", model=model,
                message=f"Reloading to apply {target}")
    cli('reload now')
    sys.exit(0)  # Won't reach here after reload


def check_url(url):
    try:
        urllib.request.urlopen(url, timeout=10)
        return True
    except Exception as e:
        print(f"ERROR: Cannot reach {url} — {e}")
        return False


def apply_config(serial):
    config_url = f"{FILE_SERVER}{CONFIG_PATH}/{serial}.cfg"
    print(f"Looking for config: {config_url}")
    if not check_url(config_url):
        raise RuntimeError(f"Config file not found on server: {config_url}")
    print("Applying config...")
    cli(f'copy {config_url} running-config')
    cli('write memory')


def main():
    print("=" * 50)
    print("Arista ZTP Startup Script")
    print("=" * 50)

    # Step 1 — Identify device
    model = get_model()
    serial = get_serial()
    print(f"Serial      : {serial or 'Unknown'}")
    print(f"Model       : {model or 'Unknown'}")

    if not serial:
        print("ERROR: Could not retrieve serial number. Aborting.")
        sys.exit(1)

    def report(status, **kw):
        print(f"[ZTP] {status}" + (f" — {kw.get('message', '')}" if kw.get("message") else ""))
        post_status(serial, status, model=model, **kw)

    try:
        report("starting")

        # Step 2 — Resolve correct EOS image
        target = resolve_target_version(model)

        # Step 3 — Version check / upgrade
        current = get_current_version()
        print(f"Current EOS : {current}")
        print(f"Target EOS  : {target}")
        report("eos_checking", eos_current=current, eos_target=target)

        if current != target:
            print("Version mismatch — upgrading. Device will reload.")
            upgrade_eos(target, serial=serial, model=model)  # Does not return

        print("EOS version OK.")
        report("eos_ok", eos_current=current, eos_target=target)

        # Step 4 — Apply per-device config
        report("config_applying", eos_current=current, eos_target=target,
               message=f"Fetching {serial}.cfg")
        apply_config(serial)

        # Step 5 — Confirm
        hostname_out = cli('show running-config | grep "^hostname"').strip()
        hostname = hostname_out.split()[-1] if hostname_out else "Unknown"
        print(f"Configuration applied for: {hostname}")
        print("ZTP complete.")
        report("complete", hostname=hostname, eos_current=current, eos_target=target,
               message=f"ZTP complete for {hostname}")

    except SystemExit:
        raise
    except Exception as exc:
        print(f"ZTP ERROR: {exc}")
        post_status(serial, "error", model=model, message=str(exc))
        sys.exit(1)


if __name__ == '__main__':
    main()
