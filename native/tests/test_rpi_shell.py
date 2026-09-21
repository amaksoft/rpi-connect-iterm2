"""Unit tests for rpi_shell pure helpers + framing. No network, no iTerm, no venv side effects.

Run:  cd native && .venv/bin/python -m unittest discover -s tests -v
"""
import io
import json
import struct
import sys
import os
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rpi_shell as m


class TestValidation(unittest.TestCase):
    def test_device_ok(self):
        self.assertEqual(m.validate_device_id("3daaefa0-5b3d-47e2-a9cd-c8f21ca039ec"),
                         "3daaefa0-5b3d-47e2-a9cd-c8f21ca039ec")

    def test_device_rejects_traversal(self):
        for bad in ["", "../x", "a/b", "a?b", "x" * 65, "a$b", "a\nb"]:
            with self.assertRaises(ValueError, msg=bad):
                m.validate_device_id(bad)

    def test_tmux_session(self):
        self.assertEqual(m.build_tmux_cmd(None), "command -v tmux >/dev/null && exec tmux -CC new -A\n")
        self.assertIn("-s work", m.build_tmux_cmd("work"))
        for bad in ["x; rm -rf /", "a b", "a$b", "x" * 33, "a\nb"]:
            with self.assertRaises(ValueError, msg=bad):
                m.build_tmux_cmd(bad)

    def test_script_path(self):
        self.assertEqual(m.validate_script_path("/a/b/c.py"), "/a/b/c.py")
        for bad in ["a$b", 'a"b', "a\nb", "", "a$(x)", "a`x`", "a;b", "a|b", "a&b"]:
            with self.assertRaises(ValueError, msg=bad):
                m.validate_script_path(bad)


class TestPollUrl(unittest.TestCase):
    def test_relative(self):
        self.assertEqual(m.resolve_poll_url("/devices/d/connections/1"),
                         m.BASE + "/devices/d/connections/1")

    def test_same_origin_absolute(self):
        u = m.BASE + "/devices/d/connections/1"
        self.assertEqual(m.resolve_poll_url(u), u)

    def test_rejects_plain_http_to_same_host(self):
        with self.assertRaises(RuntimeError):
            m.resolve_poll_url("http://connect.raspberrypi.com/devices/d")

    def test_rejects_traversal_and_query(self):
        for bad in ["/devices/../admin", "/devices/x?next=//evil", "/devices/", "/devices/x/connections/", "/other/path"]:
            with self.assertRaises(RuntimeError, msg=bad):
                m.resolve_poll_url(bad)

    def test_rejects_cross_origin(self):
        for bad in ["https://evil.com/devices/d", "http://x/", "//evil/y", "javascript:1", ""]:
            with self.assertRaises(RuntimeError, msg=bad):
                m.resolve_poll_url(bad)


class TestAppleScript(unittest.TestCase):
    def test_real_shlex_quoted_shape_passes_through(self):
        # open_iterm2 builds cmd from shlex.quote parts: single quotes must survive
        import shlex
        cmd = (f"{shlex.quote('/p/python')} {shlex.quote('/p/rpi_shell.py')} "
               f"--from-file {shlex.quote('/c/rpi-conn-1.json')}")
        osa = m.applescript_for_command(cmd)
        self.assertIn(f'command "{cmd}"', osa)
        self.assertNotIn("'\"'", osa)

    def test_rejects_newlines_and_quotes(self):
        for bad in ['a"b', "a\nb", "a\rb"]:
            with self.assertRaises(ValueError, msg=bad):
                m.applescript_for_command(bad)

    def test_backslash_escaped(self):
        osa = m.applescript_for_command("echo a\\b")
        self.assertIn("a\\\\b", osa)


class FakeStdin:
    def __init__(self, raw: bytes, max_chunk=None):
        self.buffer = io.BytesIO(raw)
        if max_chunk:
            orig = self.buffer.read
            def small(n=-1):
                return orig(min(n, max_chunk) if n is not None and n >= 0 else n)
            self.buffer.read = small

class FakeStdout:
    def __init__(self):
        self.buffer = io.BytesIO()

class TestNMFraming(unittest.TestCase):
    def test_roundtrip(self):
        obj = {"deviceId": "abc", "cookies": "a=b"}
        data = json.dumps(obj).encode()
        with mock.patch.object(m.sys, "stdin", FakeStdin(struct.pack("<I", len(data)) + data)):
            self.assertEqual(m.read_nm(), obj)

    def test_chunked_reads(self):
        # pipe delivers 1 byte at a time; framing loop must reassemble
        obj = {"deviceId": "abc"}
        data = json.dumps(obj).encode()
        raw = struct.pack("<I", len(data)) + data
        with mock.patch.object(m.sys, "stdin", FakeStdin(raw, max_chunk=1)):
            self.assertEqual(m.read_nm(), obj)

    def test_short_header_returns_none(self):
        with mock.patch.object(m.sys, "stdin", FakeStdin(b"\x01\x02")):
            self.assertIsNone(m.read_nm())

    def test_eof_mid_body_raises(self):
        obj = {"deviceId": "abc"}
        data = json.dumps(obj).encode()
        raw = struct.pack("<I", len(data)) + data[:2]  # truncated body, then EOF
        with mock.patch.object(m.sys, "stdin", FakeStdin(raw)):
            with self.assertRaises(EOFError):
                m.read_nm()

    def test_oversize_rejected(self):
        with mock.patch.object(m.sys, "stdin", FakeStdin(struct.pack("<I", 2 * 1024 * 1024))):
            with self.assertRaises(ValueError):
                m.read_nm()

    def test_write_nm_framing(self):
        fake = FakeStdout()
        with mock.patch.object(m.sys, "stdout", fake):
            m.write_nm({"ok": True})
        raw = fake.buffer.getvalue()
        (ln,) = struct.unpack("<I", raw[:4])
        self.assertEqual(json.loads(raw[4:4 + ln]), {"ok": True})


class TestReqErrors(unittest.TestCase):
    def test_http_error_mapping(self):
        import urllib.error
        err = urllib.error.HTTPError("http://x", 401, "Unauthorized", {}, io.BytesIO(b"nope"))
        with mock.patch.object(m.urllib.request, "urlopen", side_effect=err):
            with self.assertRaisesRegex(RuntimeError, "session expired"):
                m._req("GET", "https://h/", "a=b")

    def test_network_error_mapping(self):
        import urllib.error
        with mock.patch.object(m.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("boom")):
            with self.assertRaisesRegex(RuntimeError, "network error"):
                m._req("GET", "https://h/", "a=b")

    def test_crlf_in_creds_rejected(self):
        with mock.patch.object(m.urllib.request, "urlopen") as uo:
            with self.assertRaisesRegex(RuntimeError, "CR/LF"):
                m._req("GET", "https://h/", "a=b\r\nEvil: 1")
            with self.assertRaisesRegex(RuntimeError, "CR/LF"):
                m._req("GET", "https://h/", "a=b", csrf="t\nok")
            uo.assert_not_called()


class TestOpenIterM2Validation(unittest.TestCase):
    def test_bad_inputs_fail_before_subprocess(self):
        with mock.patch.object(m.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                m.open_iterm2("../evil", "c", "t", {})
            with self.assertRaises(ValueError):
                m.open_iterm2("dev", "c", "t", {}, tmux_session="a;b")
            run.assert_not_called()

    def test_forwards_tmux_opt_in(self):
        import tempfile
        seen = {}
        class FakeTF:
            name = "/priv/rpi-conn-x.json"
            def close(self): pass
        def fake_ntf(*a, **k):
            seen["dir"] = k.get("dir")
            return FakeTF()
        dumped = {}
        ok = mock.Mock(returncode=0, stderr="")
        with mock.patch.object(m.os, "makedirs"), \
             mock.patch.object(m.os, "chmod"), \
             mock.patch.object(m.os, "listdir", return_value=[]), \
             mock.patch.object(tempfile, "NamedTemporaryFile", side_effect=fake_ntf), \
             mock.patch.object(m.json, "dump", side_effect=lambda p, f: dumped.update(p)), \
             mock.patch.object(m.subprocess, "run", return_value=ok), \
             mock.patch.object(m.os, "unlink"):
            m.open_iterm2("dev1", "c", "t", {"iceServers": []}, tmux=True, tmux_session="work")
        self.assertEqual(dumped.get("tmux"), True)
        self.assertEqual(dumped.get("tmuxSession"), "work")
        self.assertIn(".cache", seen["dir"])


class TestIceWait(unittest.TestCase):
    def test_already_complete_returns_fast(self):
        import asyncio
        pc = mock.Mock(iceGatheringState="complete")
        asyncio.run(m._wait_ice_gathered(pc, timeout=5))
        pc.on.assert_not_called()

    def test_never_complete_times_out(self):
        import asyncio
        pc = mock.Mock(iceGatheringState="gathering")
        pc.on = mock.Mock(side_effect=TypeError("nope"))
        pc.add_listener = mock.Mock()
        pc.remove_listener = mock.Mock()
        asyncio.run(m._wait_ice_gathered(pc, timeout=0.05))
        pc.add_listener.assert_called_once()
        pc.remove_listener.assert_called_once()


if __name__ == "__main__":
    unittest.main()
