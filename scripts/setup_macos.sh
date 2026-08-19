#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3.12}"
INSTALL_CODEX_CLI="${TOUCH_INSTALL_CODEX_CLI:-0}"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script is for macOS." >&2
  exit 1
fi
if ! xcode-select -p >/dev/null 2>&1; then
  echo "Install Xcode Command Line Tools first: xcode-select --install" >&2
  exit 1
fi
if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is required: https://brew.sh" >&2
  exit 1
fi
brew list python@3.12 >/dev/null 2>&1 || brew install python@3.12
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN="$(brew --prefix python@3.12)/bin/python3.12"
fi
"$PYTHON_BIN" -m venv "$ROOT_DIR/.venv"
"$ROOT_DIR/.venv/bin/python" -m pip install --upgrade pip
"$ROOT_DIR/.venv/bin/pip" install -r "$ROOT_DIR/requirements-dev.txt"
if [[ "$INSTALL_CODEX_CLI" == "1" ]]; then
  brew list node >/dev/null 2>&1 || brew install node
  if ! command -v codex >/dev/null 2>&1; then
    npm install --global @openai/codex
  fi
fi
echo
echo "Dependencies are ready. Next steps:"
echo "  1. Start Codex++ with CDP enabled."
echo "  2. ./scripts/provision_board.sh"
echo "  3. ./scripts/install_server_macos.sh"
echo "  4. ./scripts/build_flash.sh --port /dev/cu.usbmodemXXXX"
if [[ "$INSTALL_CODEX_CLI" != "1" ]]; then
  echo "Optional CLI compatibility: TOUCH_INSTALL_CODEX_CLI=1 ./scripts/setup_macos.sh"
fi
