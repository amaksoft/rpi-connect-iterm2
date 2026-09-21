# Raspberry Pi Connect → iTerm2

Open a [Raspberry Pi Connect](https://connect.raspberrypi.com) remote shell
directly in [iTerm2](https://iterm2.com) — a real local tty with tmux integration,
instead of the browser's embedded terminal.

Shell-only (no screen sharing). The browser extension is just a 1-click launcher:
all WebRTC work happens in a local Python client, so iTerm2 gets raw PTY bytes.

## How it works

Reverse-engineered from the Connect web app (no official API is used):

- **Auth:** session cookie + `X-CSRF-Token`, same-origin. No tokens leave your machine.
- **Signaling:** `POST /devices/{id}/connections` with the WebRTC offer →
  `201 + Location:` → `GET` polling until the Pi posts its answer.
- **Media:** WebRTC DataChannel `shell` (raw PTY bytes both ways) + a peer-opened
  `resize` channel carrying `{"cols","rows"}`. No video transceivers.

## Prerequisites

- A Raspberry Pi enrolled in Raspberry Pi Connect with **Remote shell** enabled
- A Mac with macOS, Chrome (or Brave/Arc/Edge/Chromium), iTerm2, Python 3.12+
- `tmux` on the Pi if you want iTerm2's tmux integration (`tmux -CC`)

## Install

```bash
git clone https://github.com/<you>/rpi-connect-iterm2.git
cd rpi-connect-iterm2/native
./setup_venv.sh          # creates .venv, installs aiortc + friends
source .venv/bin/activate
```

Load the extension:

1. Open `chrome://extensions`, enable **Developer mode**, **Load unpacked** →
   select this repo's `extension/` folder.
2. Copy the extension's 32-letter ID, then register the native host:
   ```bash
   cd native
   ./install_host.sh <EXTENSION_ID>
   ```
3. Fully quit the browser (`Cmd+Q`) so it picks up the native host, reopen it.

## Use

1. Open your Pi's `remote-shell-session` page on `connect.raspberrypi.com`
   (or the device list — each row gets its own button).
2. Click **Open in iTerm2**.
3. A new iTerm2 window opens with a live shell. Attach tmux however you like:
   ```
   tmux -CC new -A
   ```

Prefer a bare shell with no tmux autostart? That's the default — nothing is
started for you. The client can autostart it if you want:
`.venv/bin/python rpi_shell.py ... --tmux [--tmux-session NAME]`.

Manual CLI (no button):

```bash
.venv/bin/python rpi_shell.py --device <DEVICE_ID> --cookies 'a=b' --csrf <TOKEN> \
  --ice-json '{"iceServers":[...]}'
```

## Tests

```bash
cd native && .venv/bin/python -m unittest discover -s tests -v   # 25 tests, offline
```

## Troubleshooting

- Crash window stays open with a traceback; full log: `~/Library/Logs/rpi-shell.log`
- `401/403` from signaling = cookies/CSRF expired → reload the Connect page, re-click.
- `venv missing` in `~/Library/Logs/rpi-shell-nm.log` → rerun `native/setup_venv.sh`.
- Button dead after extension reload → unpacked extension IDs change; rerun
  `./install_host.sh <NEW_ID>` and fully quit the browser.
- No button on the shell tab → hard-reload the tab after reloading the extension.
- Second click right after closing → the client closes its session on exit, but if
  the Pi still holds it, wait a few seconds and re-click.

## Security notes

- Session cookies live only in memory plus a `0600` payload file under
  `~/.cache/rpi-iterm-bridge/` that is deleted right after launch.
- The extension talks only to `connect.raspberrypi.com` and only to the
  registered native host (`allowed_origins`).
- This is a personal prototype against an undocumented API — Raspberry Pi can
  change the signaling format at any time.

## Layout

- `extension/` — content script (button) + background worker (cookies → native host)
- `native/` — `rpi_shell.py` (native host + aiortc client), `run.sh`, `setup_venv.sh`,
  `install_host.sh`, `requirements.txt`, `tests/`
