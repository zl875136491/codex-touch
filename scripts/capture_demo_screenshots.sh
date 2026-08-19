#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="$ROOT_DIR/demo/status-bar-demo.html"
OUTPUT_DIR="$ROOT_DIR/docs/demo"
TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/codex-touch-demo.XXXXXX")"
CHROME="${CHROME_BIN:-}"
chrome_pid=""

stop_chrome() {
  local attempt
  if [[ -z "$chrome_pid" ]]; then
    return
  fi
  pkill -TERM -P "$chrome_pid" 2>/dev/null || true
  kill -TERM "$chrome_pid" 2>/dev/null || true
  for attempt in $(seq 1 20); do
    if ! kill -0 "$chrome_pid" 2>/dev/null; then
      chrome_pid=""
      return
    fi
    sleep 0.1
  done
  pkill -KILL -P "$chrome_pid" 2>/dev/null || true
  kill -KILL "$chrome_pid" 2>/dev/null || true
  wait "$chrome_pid" 2>/dev/null || true
  chrome_pid=""
}

cleanup() {
  stop_chrome
  rm -rf "$TEMP_DIR"
}
trap cleanup EXIT

if [[ -n "$CHROME" && ! -x "$CHROME" ]]; then
  CHROME="$(command -v "$CHROME" 2>/dev/null || true)"
fi
if [[ -z "$CHROME" && -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]]; then
  CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
fi
if [[ -z "$CHROME" ]]; then
  for candidate in google-chrome chromium chromium-browser; do
    if command -v "$candidate" >/dev/null 2>&1; then
      CHROME="$(command -v "$candidate")"
      break
    fi
  done
fi
if [[ -z "$CHROME" || ! -x "$CHROME" ]]; then
  echo "Google Chrome or Chromium is required. Set CHROME_BIN to its executable path." >&2
  exit 1
fi
if [[ ! -f "$SOURCE" ]]; then
  echo "Demo source is missing: $SOURCE" >&2
  exit 1
fi

mkdir -p "$OUTPUT_DIR"

capture() {
  local view="$1"
  local output="$OUTPUT_DIR/$view.png"
  local capture_output="$TEMP_DIR/$view.png"
  local profile="$TEMP_DIR/$view-profile"
  local url="file://$SOURCE?view=$view&export=1"
  local attempt

  mkdir -p "$profile"
  : > "$capture_output"
  "$CHROME" \
    --headless=new \
    --disable-gpu \
    --hide-scrollbars \
    --no-first-run \
    --disable-background-networking \
    --disable-component-update \
    --disable-sync \
    --user-data-dir="$profile" \
    --window-size=466,466 \
    --virtual-time-budget=1000 \
    --screenshot="$capture_output" \
    "$url" \
    >/dev/null 2>&1 &
  chrome_pid=$!

  for attempt in $(seq 1 100); do
    if [[ -s "$capture_output" ]]; then
      sleep 0.2
      break
    fi
    sleep 0.1
  done
  if [[ ! -s "$capture_output" ]]; then
    echo "Unable to capture $view from $SOURCE" >&2
    exit 1
  fi
  stop_chrome
  mv "$capture_output" "$output"
}

for view in home tasks approval agents agent-detail; do
  capture "$view"
done

echo "Updated 466x466 demo screenshots in $OUTPUT_DIR"
