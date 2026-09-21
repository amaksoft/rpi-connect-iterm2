#!/bin/bash
set -u
umask 077
LOG="$HOME/Library/Logs/rpi-shell-nm.log"
mkdir -p "$HOME/Library/Logs" 2>/dev/null || true
if [ -f "$LOG" ] && [ ! -L "$LOG" ]; then tail -n 100 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"; fi
echo "$(date) launched" >> "$LOG"
BIN_DIR="$(dirname "$0")"
if [ ! -x "$BIN_DIR/.venv/bin/python" ]; then
  echo "$(date) venv missing, run native/setup_venv.sh" >> "$LOG"
  exit 1
fi
exec "$BIN_DIR/.venv/bin/python" "$BIN_DIR/rpi_shell.py"
