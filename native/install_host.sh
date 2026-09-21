#!/bin/bash
# Installs the NativeMessaging host manifest for Chromium browsers.
# Usage: ./install_host.sh [extension-id]  (or set EXT_ID env)
set -e
umask 077
HOST_NAME="com.rpi.shell"
EXT_ID="${1:-${EXT_ID:-}}"
if ! [[ "$EXT_ID" =~ ^[a-z]{32}$ ]]; then
  echo "usage: $0 <extension-id>  (copy it from chrome://extensions, or set EXT_ID env)"
  exit 1
fi
BIN="$(cd "$(dirname "$0")" && pwd)/run.sh"
chmod +x "$BIN"
if [ ! -f "$(dirname "$0")/setup_venv.sh" ]; then echo "setup_venv.sh missing next to $0"; exit 1; fi
"$(dirname "$0")/setup_venv.sh"
OK=0
for D in \
  "$HOME/Library/Application Support/Google Chrome/NativeMessagingHosts" \
  "$HOME/Library/Application Support/Chromium/NativeMessagingHosts" \
  "$HOME/Library/Application Support/BraveSoftware/Brave-Browser/NativeMessagingHosts" \
  "$HOME/Library/Application Support/Arc/NativeMessagingHosts" \
  "$HOME/Library/Application Support/Microsoft Edge/NativeMessagingHosts" \
  "$HOME/Library/Containers/com.google.Chrome/Data/Library/Application Support/Google/Chrome/NativeMessagingHosts"; do
  mkdir -p "$D" 2>/dev/null || { echo "skip (unwritable): $D"; continue; }
  # JSON-escaped via python so exotic repo paths can't corrupt the manifest
  if BIN="$BIN" HOST_NAME="$HOST_NAME" EXT_ID="$EXT_ID" DEST="$D/$HOST_NAME.json" python3 -c '
import json, os
json.dump({"name": os.environ["HOST_NAME"], "description": "rpi-connect -> iTerm2 opener",
  "path": os.environ["BIN"], "type": "stdio",
  "allowed_origins": ["chrome-extension://" + os.environ["EXT_ID"] + "/"]},
  open(os.environ["DEST"], "w"), indent=2)
'; then echo "wrote $D/$HOST_NAME.json"; OK=$((OK+1)); else echo "FAILED: $D"; fi
done
if [ "$OK" -eq 0 ]; then echo "ERROR: installed nowhere"; exit 1; fi
echo "done ($OK browsers). Fully quit the browser (Cmd+Q) so it rescans manifests."
