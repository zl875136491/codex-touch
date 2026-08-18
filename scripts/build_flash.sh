#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIO="$ROOT_DIR/.venv/bin/pio"
PORT=""
BUILD_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --build-only) BUILD_ONLY=1; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ ! -x "$PIO" ]]; then
  echo "PlatformIO is not installed. Run ./scripts/setup_macos.sh first." >&2
  exit 1
fi
if [[ ! -f "$ROOT_DIR/include/TouchConfig.h" ]]; then
  echo "Missing include/TouchConfig.h. Run ./scripts/provision_board.sh first." >&2
  exit 1
fi
"$PIO" run -e esp32s3-touch
if [[ "$BUILD_ONLY" == 1 ]]; then exit 0; fi
if [[ -z "$PORT" ]]; then
  echo "Pass the board serial port, for example: --port /dev/cu.usbmodemXXXX" >&2
  exit 2
fi
"$PIO" run -e esp32s3-touch -t upload --upload-port "$PORT"
echo "Firmware uploaded to $PORT"
