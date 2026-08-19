#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT_DIR/.venv/bin/python"
LABEL="com.codex.touch.server"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
TOKEN="${TOUCH_SERVER_TOKEN:-}"
HOST="${TOUCH_SERVER_HOST:-0.0.0.0}"
PORT="${TOUCH_SERVER_PORT:-8765}"
CODEX="${CODEX_BIN:-$(command -v codex || true)}"
CLI_ACTIONS="${TOUCH_CLI_ACTIONS:-0}"
MOCK=0
if [[ "${1:-}" == "--mock" ]]; then MOCK=1; shift; fi
if [[ $# -gt 0 ]]; then echo "Usage: $0 [--mock]" >&2; exit 2; fi

if [[ ! -x "$PYTHON" ]]; then
  echo "Missing .venv. Run ./scripts/setup_macos.sh first." >&2
  exit 1
fi
if [[ -z "$TOKEN" ]]; then read -r -p "Relay token (blank = generate): " TOKEN; fi
TOKEN="${TOKEN:-$(openssl rand -hex 24)}"
if [[ "$MOCK" == 0 && "$CLI_ACTIONS" == 1 && -z "$CODEX" ]]; then
  echo "TOUCH_CLI_ACTIONS=1 requires the Codex CLI, but it was not found." >&2
  exit 1
fi

mkdir -p "$(dirname "$PLIST")"
"$PYTHON" - "$PLIST" "$ROOT_DIR" "$PYTHON" "$CODEX" "$HOST" "$PORT" "$TOKEN" "$LABEL" "$MOCK" "$CLI_ACTIONS" <<'PY'
from pathlib import Path
import plistlib, sys

path, root, python, codex, host, port, token, label, mock, cli_actions = sys.argv[1:]
arguments = [python, '-m', 'server', '--host', host, '--port', port, '--token', token, '--verbose']
if codex:
    arguments.extend(['--codex-path', codex])
if mock == '1':
    arguments.append('--mock')
if cli_actions == '1':
    arguments.append('--cli-actions')
environment = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'}
if codex:
    environment['PATH'] = str(Path(codex).parent) + ':' + environment['PATH']
plist = {
    'Label': label,
    'WorkingDirectory': root,
    'ProgramArguments': arguments,
    'EnvironmentVariables': environment,
    'RunAtLoad': True,
    'KeepAlive': True,
    'StandardOutPath': '/tmp/codex-touch-server.log',
    'StandardErrorPath': '/tmp/codex-touch-server.error.log',
}
with open(path, 'wb') as handle:
    plistlib.dump(plist, handle, sort_keys=False)
PY
chmod 600 "$PLIST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "gui/$(id -u)/$LABEL"
echo "Real Codex relay installed: http://localhost:$PORT"
echo "Board token: $TOKEN"
echo "LAN host: $HOST"
