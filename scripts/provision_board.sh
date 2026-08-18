#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$ROOT_DIR/include/TouchConfig.h"
EXAMPLE="$ROOT_DIR/include/TouchConfig.example.h"
PYTHON="$ROOT_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then PYTHON="$(command -v python3)"; fi

WIFI_SSID="${TOUCH_WIFI_SSID:-}"
WIFI_PASSWORD="${TOUCH_WIFI_PASSWORD:-}"
SERVER_HOST="${TOUCH_SERVER_HOST:-}"
SERVER_PORT="${TOUCH_SERVER_PORT:-}"
SERVER_TOKEN="${TOUCH_SERVER_TOKEN:-}"
if [[ -z "$WIFI_SSID" ]]; then read -r -p "Wi-Fi SSID: " WIFI_SSID; fi
if [[ -z "$WIFI_PASSWORD" ]]; then read -r -s -p "Wi-Fi password: " WIFI_PASSWORD; echo; fi
if [[ -z "$SERVER_HOST" ]]; then read -r -p "Relay server host [192.168.1.10]: " SERVER_HOST; fi
SERVER_HOST="${SERVER_HOST:-192.168.1.10}"
if [[ -z "$SERVER_PORT" ]]; then read -r -p "Relay server port [8765]: " SERVER_PORT; fi
SERVER_PORT="${SERVER_PORT:-8765}"
if [[ -z "$SERVER_TOKEN" ]]; then read -r -p "Relay token (blank = generate): " SERVER_TOKEN; fi
if [[ -z "$SERVER_TOKEN" ]]; then
  SERVER_TOKEN="$(openssl rand -hex 24)"
fi

"$PYTHON" - "$EXAMPLE" "$CONFIG" "$WIFI_SSID" "$WIFI_PASSWORD" "$SERVER_HOST" "$SERVER_PORT" "$SERVER_TOKEN" <<'PY'
from pathlib import Path
import sys

example, output, ssid, password, host, port, token = sys.argv[1:]
text = Path(example).read_text()
text = text.replace('replace-me', ssid, 1)
text = text.replace('replace-me', password, 1)
text = text.replace('192.168.1.10', host, 1)
text = text.replace('8765', port, 1)
text = text.replace('replace-me', token, 1)
Path(output).write_text(text)
PY
chmod 600 "$CONFIG"
echo "Wrote ignored local config: $CONFIG"
