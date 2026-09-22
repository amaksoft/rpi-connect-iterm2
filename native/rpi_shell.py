#!/usr/bin/env python3
"""rpi-shell: Connect shell DataChannel -> local tty. Also Chrome NativeMessaging host.

REAL protocol (from connect.raspberrypi.com.har + application-*.js):
- Auth: session cookie + X-CSRF-Token (meta[name=csrf-token]), same-origin. No Bearer JWT, no WSS.
- Signaling (HTTPS polling):
    POST /devices/{id}/connections  body=JSON localDescription {type:offer,sdp}
      headers: X-CSRF-Token, Content-Type: application/octet-stream, Accept: application/json
      -> 201 + Location: /devices/{id}/connections/{connId}
    GET {Location} every 1s until body contains {"answer": {type:answer,sdp}}
    -> pc.setRemoteDescription(answer)
- ICE: from page data-shell-ice-configuration-value (stun + turn1 credentials).
- WebRTC: DataChannel("shell") raw bytes both ways.
          Remote opens DataChannel(label="resize"); on resize send JSON {cols,rows}.

CLI:
  .venv/bin/python rpi_shell.py --device ID --cookies 'session=...' --csrf TOKEN \
      --ice-json '{"iceServers":[...]}' [--tmux] [--tmux-session NAME]
Native host msg:
  {"deviceId","cookies","csrfToken","iceConfiguration","tmux"?,"tmuxSession"?}
"""
import argparse, asyncio, json, os, re, signal, struct, subprocess, sys, termios, tty
import urllib.request, urllib.error

os.umask(0o077)
BASE = "https://connect.raspberrypi.com"
DEVICE_RE = re.compile(r"^[\w-]{1,64}$")
TMUX_RE = re.compile(r"^[\w-]{1,32}$")
SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9/_.@+ -]+$")

# --- pure helpers (unit-tested, no I/O) ---
def validate_device_id(v):
    if not DEVICE_RE.match(v or ""): raise ValueError("bad deviceId")
    return v

def validate_tmux_session(v):
    if v and not TMUX_RE.match(v): raise ValueError("bad tmux session name (want [\\w-]{1,32})")
    return v

def validate_script_path(p):
    if not SAFE_PATH_RE.match(p or ""): raise ValueError("unsafe script path")
    if '"' in p: raise ValueError("unsafe quoting in launch path")
    return p

def build_tmux_cmd(session=None):
    validate_tmux_session(session)
    cmd = "command -v tmux >/dev/null && exec tmux -CC new -A"
    if session: cmd += f" -s {session}"
    return cmd + "\n"

def applescript_for_command(cmd):
    """Wrap a /bin/sh command for iTerm2's `create window ... command "..."."""
    if "\n" in cmd or "\r" in cmd: raise ValueError("bad command")
    # Strict: words from shlex.quote never contain raw double quotes; reject them
    # outright instead of escaping (no legitimate launch cmd needs them).
    if '"' in cmd: raise ValueError("unsafe quoting in launch command")
    esc = cmd.replace("\\", "\\\\")
    return ("tell application \"iTerm2\" to create window with default profile "
            f"command \"{esc}\"")

_POLL_RE = re.compile(r"^/devices/[\w-]+/connections/[\w-]+/?$")
def friendly_error(e):
    """Map an exception to (title, hint) for the crash panel. Traceback goes to the log only."""
    msg = str(e)
    if "session expired" in msg or "401" in msg or "403" in msg:
        return ("login expired", "reload the Connect page in Chrome and click Open in iTerm2 again.")
    if "DataChannel never opened" in msg or "TURN" in msg:
        return ("Pi unreachable through TURN relay",
                "reload the Connect page (fresh TURN credentials) and re-click. "
                "If it persists, the Pi may be offline or behind a strict firewall.")
    if "timed out waiting for answer" in msg:
        return ("Pi didn't answer", "is the Pi online with rpi-connect running? Re-click to retry.")
    if "stdin is not a TTY" in msg:
        return ("no terminal attached", "launch this from iTerm2 via the extension button.")
    if isinstance(e, ValueError):
        return ("bad input", msg)
    return (f"unexpected error ({type(e).__name__})", f"{msg} - please report with the log.")

def resolve_poll_url(loc):
    if loc.startswith("http"):
        if not loc.startswith(BASE + "/devices/"):
            raise RuntimeError("refusing cross-origin signaling Location")
        from urllib.parse import urlparse as _up
        loc = _up(loc).path or ""
    if _POLL_RE.match(loc or ""): return BASE + loc
    raise RuntimeError("refusing unexpected signaling Location")

def set_raw(fd):
    import termios as t
    old = t.tcgetattr(fd); tty.setraw(fd); return old

def get_winsize():
    import fcntl, array
    try:
        buf = array.array('H', [0, 0, 0, 0])
        fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, buf)
        return (buf[1] or 80, buf[0] or 24)
    except Exception:
        return 80, 24

def _read_exact(n):
    buf = b""
    while len(buf) < n:
        chunk = sys.stdin.buffer.read(n - len(buf))
        if not chunk: break
        buf += chunk
    return buf

def read_nm():
    raw = _read_exact(4)
    if len(raw) < 4: return None
    (ln,) = struct.unpack('<I', raw)
    if ln > 1024 * 1024: raise ValueError(f"NM message too large: {ln}")
    buf = _read_exact(ln)
    if len(buf) < ln: raise EOFError("NM pipe closed mid-message")
    return json.loads(buf.decode())

def write_nm(obj):
    d = json.dumps(obj).encode()
    sys.stdout.buffer.write(struct.pack('<I', len(d)) + d); sys.stdout.buffer.flush()

def open_iterm2(device, cookies, csrf, ice, tmux=False, tmux_session=None):
    import shlex, tempfile
    validate_device_id(device)
    validate_tmux_session(tmux_session)
    if not isinstance(cookies, str) or not isinstance(csrf, str):
        raise ValueError("bad cookies/csrf (want strings)")
    if not isinstance(ice, dict): raise ValueError("bad iceConfiguration (want object)")
    payload = {"deviceId": device, "cookies": cookies, "csrfToken": csrf,
               "iceConfiguration": ice, "tmux": bool(tmux), "tmuxSession": tmux_session}
    priv = os.path.expanduser("~/.cache/rpi-iterm-bridge")
    try: os.makedirs(priv, mode=0o700, exist_ok=True)
    except Exception: pass
    try: os.chmod(priv, 0o700)
    except Exception: pass
    # sweeper: crash/SIGKILL before main() leaves 0600 secret files; drop stale ones
    import time as _t
    try:
        now = _t.time()
        for _f in os.listdir(priv):
            if not _f.startswith("rpi-conn-") or not _f.endswith(".json"): continue
            _p = os.path.join(priv, _f)
            try:  # lstat only: never follow symlinks, unlink removes the link itself
                if now - os.lstat(_p).st_mtime > 600:
                    os.unlink(_p)
            except Exception: pass
    except Exception: pass
    tf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, prefix="rpi-conn-", dir=priv)
    try:
        os.chmod(tf.name, 0o600)
        json.dump(payload, tf); tf.close()
        me = validate_script_path(os.path.abspath(__file__))
        venv_py = os.path.join(os.path.dirname(me), ".venv", "bin", "python")
        if '"' in venv_py or '"' in tf.name:
            raise ValueError("unsafe quoting in launch path")
        cmd = f"{shlex.quote(venv_py)} {shlex.quote(me)} --from-file {shlex.quote(tf.name)}"
        r1 = subprocess.run(["osascript", "-e", 'tell application "iTerm2" to activate'],
                            capture_output=True, text=True)
        if r1.returncode != 0: raise RuntimeError(f"iTerm2 not available: {r1.stderr.strip()[:200]}")
        # cmd's words are already shlex-quoted for /bin/sh; applescript_for_command
        # escapes for the AppleScript "..." layer (never shlex.quote the whole cmd -
        # that would make iTerm exec one binary with spaces in its name).
        osa = applescript_for_command(cmd)
        r2 = subprocess.run(["osascript", "-e", osa], capture_output=True, text=True)
        if r2.returncode != 0: raise RuntimeError(f"iTerm2 open failed: {r2.stderr.strip()[:300]}")
    except Exception:
        try: os.unlink(tf.name)
        except Exception: pass
        raise

def _ssl_context():
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()

def _req(method, url, cookies, csrf=None, data=None, timeout=10):
    for _v in (cookies, csrf or ""):
        if "\r" in _v or "\n" in _v:
            raise RuntimeError(f"{method} {url} -> refusing header with CR/LF (re-login and retry)")
    r = urllib.request.Request(url, method=method,
        data=json.dumps(data).encode() if data is not None else None)
    r.add_header("Accept", "application/json")
    r.add_header("Cookie", cookies)
    if csrf: r.add_header("X-CSRF-Token", csrf)
    if data is not None: r.add_header("Content-Type", "application/octet-stream")
    try:
        with urllib.request.urlopen(r, context=_ssl_context(), timeout=timeout) as resp:
            return resp.status, resp.headers.get("Location"), resp.read().decode()
    except urllib.error.HTTPError as e:
        try: body = e.read().decode(errors="replace")[:300]
        except Exception: body = "<unreadable error body>"
        if e.code in (401, 403):
            raise RuntimeError(f"{method} {url} -> {e.code}: session expired, reload Connect page and re-click. {body}")
        raise RuntimeError(f"{method} {url} -> {e.code}: {body}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"{method} {url} -> network error: {e}")

async def _wait_ice_gathered(pc, timeout=5):
    if pc.iceGatheringState == "complete": return
    ev = asyncio.Event()
    def _check():
        if pc.iceGatheringState == "complete" and not ev.is_set(): ev.set()
    try:
        try: pc.on("icegatheringstatechange")(_check)  # aiortc: on() is a decorator factory
        except TypeError: pc.add_listener("icegatheringstatechange", _check)
        try: await asyncio.wait_for(ev.wait(), timeout)
        except asyncio.TimeoutError: pass
    finally:
        try: pc.remove_listener("icegatheringstatechange", _check)
        except Exception: pass

async def run_shell(device_id, cookies, csrf, ice_cfg, tmux=False, tmux_session=None):
    validate_device_id(device_id)
    validate_tmux_session(tmux_session)
    from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer
    loop = asyncio.get_event_loop()
    # Downgrade aioice TURN retry storms (e.g. ChannelBind 400 on stale creds)
    # from scary tracebacks to one-line warnings; real failures still surface
    # via the DataChannel-open timeout below.
    _turn_noise = [0]
    _NOISY = ("ChannelBind", "TransactionFailed", "send_data", "socket.send", "stun", "turn")
    def _exc_handler(lp, ctx):
        _msg = f"{ctx.get('message', '')} {ctx.get('exception', '')}"
        if any(_k.lower() in _msg.lower() for _k in _NOISY):
            _turn_noise[0] += 1
            if _turn_noise[0] <= 3:
                print(f"[rpi-shell] network hiccup ({_turn_noise[0]}/3 logged, rest silenced)", file=sys.stderr)
            return
        lp.default_exception_handler(ctx)
    try: loop.set_exception_handler(_exc_handler)
    except Exception: pass
    # Page-embedded ICE creds are time-limited; refresh them right before
    # offering (mirrors the web app's /ice-configuration refresh). Fall back
    # to embedded on any failure so a refresh outage can't break a good page.
    try:
        _st, _, _body = await loop.run_in_executor(None, _req, "GET",
            f"{BASE}/devices/{device_id}/ice-configuration", cookies, csrf)
        _fresh = json.loads(_body or "{}")
        if _fresh.get("iceServers"): ice_cfg = _fresh
    except Exception as e:
        print(f"[rpi-shell] ICE refresh failed ({e}), using page config", file=sys.stderr)
    cfg = RTCConfiguration(iceServers=[RTCIceServer(**s) for s in (ice_cfg or {}).get("iceServers", [])])
    pc = RTCPeerConnection(cfg)
    poll_url = None
    try:
        shell = pc.createDataChannel("shell")
        opened = asyncio.Event()
        resize_chan = {}
        pending_resize = {}
        last_resize = [0.0]

        def _send_now(chan, cols, rows):
            if chan and chan.readyState == "open":
                try: chan.send(json.dumps({"cols": cols, "rows": rows}))
                except Exception: pass
            else:
                pending_resize["v"] = (cols, rows)

        @shell.on("open")
        def _o():
            loop.call_soon_threadsafe(opened.set)

        @shell.on("message")
        def _m(data):
            try:
                if isinstance(data, str): data = data.encode()
                os.write(sys.stdout.fileno(), data)
            except (OSError, ValueError) as e:
                print(f"[rpi-shell] stdout write failed ({e})", file=sys.stderr)

        @pc.on("datachannel")
        def _dc(chan):
            if chan.label == "resize":
                resize_chan["c"] = chan
                @chan.on("open")
                def _ro():
                    cols, rows = get_winsize()
                    v = pending_resize.pop("v", (cols, rows))
                    _send_now(chan, v[0], v[1])

        resize_timer = [None]
        def send_resize():
            import time
            now = time.monotonic()
            if now - last_resize[0] < 0.2:
                # trailing edge: single deferred delivery, cancel prior timer
                try:
                    if resize_timer[0] is not None:
                        try: resize_timer[0].cancel()
                        except Exception: pass
                    resize_timer[0] = loop.call_later(0.25, send_resize)
                except Exception: pass
                return
            last_resize[0] = now
            try:
                if resize_timer[0] is not None:
                    resize_timer[0] = None
            except Exception: pass
            cols, rows = get_winsize()
            _send_now(resize_chan.get("c"), cols, rows)

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        await _wait_ice_gathered(pc)  # ensure srflx/relay candidates in SDP (no trickle endpoint)
        ld = {"type": pc.localDescription.type, "sdp": pc.localDescription.sdp}
        st, loc, _ = await loop.run_in_executor(None, _req, "POST",
            f"{BASE}/devices/{device_id}/connections", cookies, csrf, ld)
        if st not in (200, 201) or not loc:
            raise RuntimeError(f"POST connections -> {st} (no Location)")
        poll_url = resolve_poll_url(loc)
        _blips = 0
        for i in range(120):
            await asyncio.sleep(1)
            try:
                st, _, body = await loop.run_in_executor(None, _req, "GET", poll_url, cookies)
            except RuntimeError as e:
                _blips += 1  # transient network blip: keep waiting, fail after 10 straight
                if _blips >= 10: raise RuntimeError(f"poll failed 10x in a row, aborting: {e}")
                if i % 10 == 9: print(f"[rpi-shell] poll blip ({_blips}x): {e}", file=sys.stderr)
                continue
            _blips = 0
            if st in (401, 403, 404, 410):
                raise RuntimeError(f"poll -> {st}: session/connection expired, re-click Open in iTerm2")
            try: msg = json.loads(body or "{}")
            except Exception: continue
            if msg.get("answer"):
                try:
                    await pc.setRemoteDescription(RTCSessionDescription(
                        sdp=msg["answer"]["sdp"], type=msg["answer"]["type"]))
                except Exception as e:
                    raise RuntimeError(f"bad answer from Pi ({e}), re-click Open in iTerm2")
                break
            if i % 10 == 9: print(f"[rpi-shell] waiting for Pi answer... ({i+1}s)", file=sys.stderr)
        else:
            raise RuntimeError("timed out waiting for answer (120s)")

        stdin_fd = sys.stdin.fileno()
        old = None
        try: old = set_raw(stdin_fd)
        except Exception as e: raise RuntimeError(f"stdin is not a TTY ({e})")
        import threading as _th
        _main_thread = _th.current_thread() is _th.main_thread()
        if not _main_thread:
            print("[rpi-shell] warning: not on main thread, signals unavailable", file=sys.stderr)
        try:
            try:
                if not _main_thread: raise RuntimeError("no signals off-main")
                loop.add_signal_handler(signal.SIGWINCH, send_resize)
            except Exception:
                try: signal.signal(signal.SIGWINCH, lambda *_: loop.call_soon_threadsafe(send_resize))
                except Exception as e:
                    print(f"[rpi-shell] warning: no SIGWINCH handling ({e})", file=sys.stderr)
            try: await asyncio.wait_for(opened.wait(), 60)
            except asyncio.TimeoutError:
                raise RuntimeError("DataChannel never opened (60s) - likely TURN rejected stale "
                    "credentials (see TURN hiccups above). Reload the Connect page and re-click "
                    "for fresh ICE config.")
            send_resize()
            if tmux and shell.readyState == "open":
                shell.send(b"export TERM=xterm-256color\n")
                await asyncio.sleep(0.3)
                if shell.readyState == "open": shell.send(build_tmux_cmd(tmux_session).encode())
            stop = asyncio.Event()
            def _stop(*_):
                if not stop.is_set(): loop.call_soon_threadsafe(stop.set)
            for _sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", None)):
                if _sig is None: continue
                try:
                    if not _main_thread: raise RuntimeError("no signals off-main")
                    loop.add_signal_handler(_sig, _stop)
                except Exception:
                    try: signal.signal(_sig, lambda *_a, **_k: loop.call_soon_threadsafe(stop.set))
                    except Exception as e:
                        print(f"[rpi-shell] warning: no shutdown handling for sig {_sig} ({e})", file=sys.stderr)
            import contextlib as _cl
            stop_task = asyncio.ensure_future(stop.wait())
            try:
                while not stop.is_set():
                    read_task = asyncio.ensure_future(loop.run_in_executor(None, os.read, stdin_fd, 4096))
                    try:
                        done, _ = await asyncio.wait([read_task, stop_task], return_when=asyncio.FIRST_COMPLETED)
                        if stop.is_set():
                            # drain a keystroke that arrived with shutdown before exiting
                            try:
                                if read_task.done() and not read_task.cancelled():
                                    _d = read_task.result()
                                    if _d and shell.readyState == "open":
                                        with _cl.suppress(Exception): shell.send(_d)
                            except Exception: pass
                            print("[rpi-shell] window closed, shutting down cleanly...", file=sys.stderr)
                            break
                        try: data = read_task.result()
                        except Exception: break
                        if not data: break
                        if shell.readyState == "open":
                            try: shell.send(data)
                            except Exception as e:
                                print(f"[rpi-shell] send failed (Pi gone?): {e}", file=sys.stderr); break
                        else:
                            print("[rpi-shell] channel closed by Pi", file=sys.stderr); break
                    finally:
                        try: read_task.cancel()
                        except Exception: pass
                        with _cl.suppress(asyncio.CancelledError, Exception):
                            await read_task
            finally:
                try: stop_task.cancel()
                except Exception: pass
                with _cl.suppress(asyncio.CancelledError, Exception):
                    await stop_task
            # NOTE: run_in_executor(os.read) thread can't be unblocked by cancel();
            # it lingers until keypress/EOF. Daemon threads exit with the process.
        finally:
            if old is not None:
                try: termios.tcsetattr(stdin_fd, termios.TCSANOW, old)
                except Exception: pass
    finally:
        try: await pc.close()  # frees the window; server-side session expires on its own
        except Exception: pass
        if poll_url and loop is not None:  # best-effort: release Pi-side connections/{id}
            try: await loop.run_in_executor(None, _req, "DELETE", poll_url, cookies, csrf)
            except Exception: pass

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device"); ap.add_argument("--cookies"); ap.add_argument("--csrf")
    ap.add_argument("--ice-json"); ap.add_argument("--from-file")
    ap.add_argument("--tmux", action="store_true")
    ap.add_argument("--tmux-session", default=None)
    a = ap.parse_args()
    from_file = a.from_file
    # NM mode only when launched by Chrome: piped stdin AND zero CLI flags
    _any_flag = any([a.device, a.cookies, a.csrf, a.ice_json, from_file, a.tmux, a.tmux_session])
    if not _any_flag and not sys.stdin.isatty():
        try: msg = read_nm()
        except Exception as e:
            try: write_nm({"ok": False, "error": f"bad NM message: {e}"})
            except Exception: pass
            sys.exit(1)
        if msg and "deviceId" in msg:
            try:
                open_iterm2(msg["deviceId"], msg.get("cookies", ""),
                            msg.get("csrfToken", ""), msg.get("iceConfiguration"),
                            tmux=bool(msg.get("tmux", False)),
                            tmux_session=msg.get("tmuxSession"))
                write_nm({"ok": True})
            except Exception as e:
                try: write_nm({"ok": False, "error": str(e)})
                except Exception: pass
            return
        try: write_nm({"ok": False, "error": "missing deviceId"})
        except Exception: pass
        sys.exit(1)
    if from_file:
        _priv = os.path.expanduser("~/.cache/rpi-iterm-bridge")
        try:
            with open(from_file) as _fh:
                p = json.load(_fh)
            device, cookies, csrf, ice = p["deviceId"], p.get("cookies", ""), p.get("csrfToken", ""), p.get("iceConfiguration")
            if not DEVICE_RE.match(device or ""): raise ValueError("bad deviceId in payload")
            if not isinstance(cookies, str) or not isinstance(csrf, str):
                raise ValueError("bad cookies/csrf in payload (want strings)")
            if not isinstance(ice, dict): raise ValueError("bad iceConfiguration in payload (want object)")
        finally:
            # secrets: unlink payloads we created; leave foreign paths alone
            try:
                if os.path.realpath(from_file).startswith(os.path.realpath(_priv) + os.sep):
                    os.unlink(from_file)
                else:
                    print(f"[rpi-shell] warning: keeping foreign --from-file {from_file}", file=sys.stderr)
            except Exception: pass
        tmux = p.get("tmux", False)
        tmux_session = p.get("tmuxSession")
    else:
        if not (a.device and a.cookies is not None and a.csrf is not None and a.ice_json):
            ap.error("--device/--cookies/--csrf/--ice-json required (or --from-file)")
        device, cookies, csrf, ice = a.device, a.cookies, a.csrf, json.loads(a.ice_json)
        tmux = a.tmux
        tmux_session = a.tmux_session
    os.umask(0o077)
    _logdir = os.path.expanduser("~/Library/Logs")
    try: os.makedirs(_logdir, exist_ok=True)
    except Exception: pass
    _log = os.path.join(_logdir, "rpi-shell.log")
    try:
        if os.path.islink(_log): os.unlink(_log)  # refuse to follow symlinks
        if os.path.exists(_log):
            with open(_log) as _f: _lines = _f.readlines()[-200:]
            _fd = os.open(_log, os.O_WRONLY | os.O_TRUNC | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(_fd, "w") as _f: _f.writelines(_lines)
            try: os.chmod(_log, 0o600)
            except Exception: pass
    except Exception: pass
    _lfd = os.open(_log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try: os.fchmod(_lfd, 0o600)
    except Exception: pass
    logf = os.fdopen(_lfd, "a")
    logf.write(f"\n=== launch device={device} ===\n"); logf.flush()
    try:
        asyncio.run(run_shell(device, cookies, csrf, ice, tmux, tmux_session))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n[rpi-shell] interrupted, goodbye.", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        import traceback; traceback.print_exc(file=logf); logf.flush()
        _title, _hint = friendly_error(e)
        print(f"\n[rpi-shell] {_title}\n  -> {_hint}\n  detail: {_log}", file=sys.stderr)
        try:
            if sys.stdin.isatty():
                print("press ENTER to close...", file=sys.stderr)
                input()
        except (EOFError, OSError): pass
        sys.exit(1)

if __name__ == "__main__":
    main()
