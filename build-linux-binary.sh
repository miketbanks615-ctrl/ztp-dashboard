#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="$SCRIPT_DIR/dist"
BUILD_ROOT="$(mktemp -d -t arista-ztp-build-XXXXXXXXXX)"
BUILD_SOURCE="$BUILD_ROOT/ztp-dashboard"
KEEP_BUILD_DIR=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep-build-dir) KEEP_BUILD_DIR=1; shift ;;
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

cleanup() {
  if [[ "$KEEP_BUILD_DIR" == "1" ]]; then
    echo "Kept build directory: $BUILD_ROOT"
  else
    rm -rf "$BUILD_ROOT"
  fi
}
trap cleanup EXIT

mkdir -p "$BUILD_SOURCE" "$OUTPUT_DIR"
cp -R "$SCRIPT_DIR"/. "$BUILD_SOURCE/"
rm -rf \
  "$BUILD_SOURCE/.venv" \
  "$BUILD_SOURCE/.build-venv" \
  "$BUILD_SOURCE/build" \
  "$BUILD_SOURCE/dist"
find "$BUILD_SOURCE" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$BUILD_SOURCE" -type f -name '*.pyc' -delete

cd "$BUILD_SOURCE"
python3 -m venv .build-venv
.build-venv/bin/python -m ensurepip --upgrade
.build-venv/bin/python -m pip install --upgrade --force-reinstall pip setuptools wheel
.build-venv/bin/python -m pip install -e . pyinstaller

.build-venv/bin/pyinstaller \
  --noconfirm \
  --clean \
  --onefile \
  --name AristaZTPDashboard-linux \
  --collect-all ztp_dashboard \
  --add-data "ztp_dashboard/data:ztp_dashboard/data" \
  ztp_dashboard/launcher.py

cp ./dist/AristaZTPDashboard-linux "$OUTPUT_DIR/AristaZTPDashboard-linux"
chmod +x "$OUTPUT_DIR/AristaZTPDashboard-linux"
echo "Wrote $OUTPUT_DIR/AristaZTPDashboard-linux"
