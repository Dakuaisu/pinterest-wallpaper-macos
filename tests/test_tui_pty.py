import errno
import fcntl
import importlib
import json
import os
import pty
import re
import select
import signal
import struct
import sys
import termios
import time
import types
import unittest

from test_tui_session import Draft, FakeBackend


class PtyBackend(FakeBackend):
    def __init__(self, event_fd, failure):
        super().__init__()
        self.event_fd = event_fd
        self.failure = failure

    def record(self):
        if self.events:
            os.write(self.event_fd, (json.dumps(self.events[-1]) + "\n").encode())

    def preview(self, pin, draft, original):
        if self.failure == "crash":
            raise RuntimeError("unexpected failure")
        try:
            return super().preview(pin, draft, original)
        finally:
            self.record()

    def save(self, pin, draft):
        try:
            return super().save(pin, draft)
        finally:
            self.record()

    def apply(self, pin, draft):
        try:
            return super().apply(pin, draft)
        finally:
            self.record()

class PtySession:
    def __init__(self, cols=80, rows=24, failure="", kitty=False, force_blocks=False,
                 environment=None, trace_input=False):
        self.master, self.slave = pty.openpty()
        self.resize(cols, rows, notify=False)
        self.before = termios.tcgetattr(self.slave)
        event_read, event_write = os.pipe()
        self.event_read = event_read
        self.readers = {self.master, event_read}
        self.output = b""
        self.events = b""
        self.pid = os.fork()
        if self.pid == 0:
            os.close(event_read)
            os.close(self.master)
            os.setsid()
            fcntl.ioctl(self.slave, termios.TIOCSCTTY, 0)
            for fd in (0, 1, 2):
                os.dup2(self.slave, fd)
            if self.slave > 2:
                os.close(self.slave)
            sys.stdin = os.fdopen(0, "r", encoding="utf-8", closefd=False)
            sys.stdout = os.fdopen(1, "w", encoding="utf-8", buffering=1, closefd=False)
            sys.stderr = os.fdopen(2, "w", encoding="utf-8", buffering=1, closefd=False)
            if environment is not None:
                for name in ("TERM", "TERM_PROGRAM", "COLORTERM", "KITTY_WINDOW_ID",
                             "PINTEREST_WALLPAPER_IMAGE", "TMUX", "STY"):
                    os.environ.pop(name, None)
                os.environ.update(environment)
            elif kitty:
                os.environ.update({"TERM": "xterm-kitty", "KITTY_WINDOW_ID": "7231"})
                for name in ("PINTEREST_WALLPAPER_IMAGE", "TERM_PROGRAM", "TMUX", "STY"):
                    os.environ.pop(name, None)
                if force_blocks:
                    os.environ["PINTEREST_WALLPAPER_IMAGE"] = "blocks"
            else:
                os.environ["PINTEREST_WALLPAPER_IMAGE"] = "blocks"
            if trace_input:
                original_read = os.read

                def traced_read(fd, size):
                    data = original_read(fd, size)
                    if fd == 0:
                        os.write(event_write, b"input:" + data.hex().encode("ascii") + b"\n")
                    return data

                os.read = traced_read
            module = types.ModuleType("wallpaper_tui_backend")
            module.Draft = Draft
            sys.modules["wallpaper_tui_backend"] = module
            previous_handler = signal.getsignal(signal.SIGTERM)
            try:
                result = importlib.import_module("wallpaper_tui").run(PtyBackend(event_write, failure))
                os.write(event_write, ("exit:" + str(result) + "\n").encode())
            except BaseException as exc:
                os.write(event_write, ("exception:" + type(exc).__name__ + "\n").encode())
            finally:
                after = termios.tcgetattr(0)
                before = self.before[:]
                transient = getattr(termios, "PENDIN", 0)
                after[3] &= ~transient
                before[3] &= ~transient
                os.write(event_write, ("restored:" + str(after == before) + "\n").encode())
                os.write(event_write, ("handler_restored:" +
                         str(signal.getsignal(signal.SIGTERM) == previous_handler) + "\n").encode())
                os.close(event_write)
                os._exit(0)
        os.close(event_write)

    def resize(self, cols, rows, notify=True):
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, cols * 8, rows * 16))
        if notify:
            os.kill(self.pid, signal.SIGWINCH)

    def wait_for(self, needle, events=False, count=1):
        deadline = time.monotonic() + 5
        while (self.events if events else self.output).count(needle) < count:
            remaining = deadline - time.monotonic()
            target = self.event_read if events else self.master
            if remaining <= 0 or target not in self.readers:
                raise AssertionError(f"PTY ended waiting for {needle!r}; output={self.output[-1000:]!r} "
                                     f"events={self.events!r}")
            ready, _, _ = select.select(list(self.readers), [], [], remaining)
            for fd in ready:
                try:
                    data = os.read(fd, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    data = b""
                if not data:
                    self.readers.remove(fd)
                elif fd == self.master:
                    self.output += data
                else:
                    self.events += data

    def send(self, data):
        os.write(self.master, data)

    def close(self):
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.close(self.master)
        os.waitpid(self.pid, 0)
        os.close(self.event_read)
        os.close(self.slave)


class PtyTests(unittest.TestCase):
    def open_session(self, *args, **kwargs):
        session = PtySession(*args, **kwargs)
        self.addCleanup(session.close)
        return session

    def test_wide_terminal_batched_keys_apply_then_save_and_restore(self):
        session = self.open_session()
        session.wait_for(b"Enter")
        session.wait_for(b"38;2;12;34;56")
        session.send(b"\x1b[BFr\r")
        session.wait_for(b'["apply", "two", 180, false]', events=True)
        session.wait_for(b'["save", "two", 180, false]', events=True)
        session.wait_for(b"Wallpaper set")
        session.send(b"q")
        session.wait_for(b"exit:0", events=True)
        session.wait_for(b"\x1b[?1049l")
        session.wait_for(b"restored:True", events=True)
        session.wait_for(b"handler_restored:True", events=True)
        self.assertIn(b"38;2;12;34;56", session.output)

    def test_resize_from_too_small_to_two_panes(self):
        session = self.open_session(cols=25, rows=8)
        session.wait_for(b"Resize")
        session.resize(60, 18)
        session.wait_for(b"First")
        session.send(b"q")
        session.wait_for(b"exit:0", events=True)
        session.wait_for(b"restored:True", events=True)

    def test_keys_are_ignored_while_the_terminal_is_too_small(self):
        session = self.open_session(cols=25, rows=8)
        session.wait_for(b"Resize")
        session.send(b"\r")
        session.send(b"s")
        session.resize(60, 18)
        session.wait_for(b"First")
        session.send(b"q")
        session.wait_for(b"exit:0", events=True)
        self.assertNotIn(b'"apply"', session.events)
        self.assertNotIn(b'"save"', session.events)

    def test_ctrl_z_does_not_suspend_and_sigquit_restores_terminal(self):
        session = self.open_session()
        session.wait_for(b"Enter")
        session.send(b"\x1a\x1b[B")
        session.wait_for(b" 2 of 2 ")
        os.kill(session.pid, signal.SIGQUIT)
        session.wait_for(b"exit:143", events=True)
        session.wait_for(b"\x1b[?1049l")
        session.wait_for(b"restored:True", events=True)

    def test_minimum_size_shows_both_panes_and_controls(self):
        session = self.open_session(cols=36, rows=10)
        session.wait_for(b"Quit")
        self.assertIn(b" 1 of 2 ", session.output)
        self.assertIn(b" Preview ", session.output)
        self.assertIn(b"Help", session.output)
        self.assertIn(b"38;2;12;34;56", session.output)
        session.send(b"q")
        session.wait_for(b"exit:0", events=True)
        session.wait_for(b"restored:True", events=True)

    def test_save_error_is_visible_and_session_remains_usable(self):
        session = self.open_session(failure="save")
        session.wait_for(b"Enter")
        session.send(b"rs")
        session.wait_for(b"cannot save")
        session.send(b"q")
        session.wait_for(b"exit:0", events=True)
        session.wait_for(b"restored:True", events=True)

    def test_sigterm_restores_terminal(self):
        session = self.open_session()
        session.wait_for(b"Enter")
        os.kill(session.pid, signal.SIGTERM)
        session.wait_for(b"exit:143", events=True)
        session.wait_for(b"\x1b[?1049l")
        session.wait_for(b"restored:True", events=True)
        session.wait_for(b"handler_restored:True", events=True)

    def test_ctrl_c_restores_terminal(self):
        session = self.open_session()
        session.wait_for(b"Enter")
        session.send(b"\x03")
        session.wait_for(b"exit:130", events=True)
        session.wait_for(b"\x1b[?1049l")
        session.wait_for(b"restored:True", events=True)

    def test_unexpected_exception_restores_terminal(self):
        session = self.open_session(failure="crash")
        session.wait_for(b"exception:RuntimeError", events=True)
        session.wait_for(b"\x1b[?1049l")
        session.wait_for(b"restored:True", events=True)

    def test_kitty_resize_and_quit_delete_only_the_session_image(self):
        session = self.open_session(kitty=True)
        session.wait_for(b",C=1,q=2,m=0;")
        match = re.search(rb"\x1b_Ga=T,f=100,t=d,i=(\d+),", session.output)
        self.assertIsNotNone(match)
        image_id = match.group(1)
        delete = b"\x1b_Ga=d,d=I,i=" + image_id + b",q=2;"
        session.resize(60, 18)
        session.wait_for(delete)
        session.wait_for(b",C=1,q=2,m=0;", count=2)
        session.send(b"q")
        session.wait_for(b"exit:0", events=True)
        session.wait_for(delete, count=2)
        session.wait_for(b"restored:True", events=True)
        self.assertNotIn(b"\x1b_Ga=d,d=A", session.output)

    def test_manual_block_fallback_on_kitty_preserves_keyboard_input(self):
        session = self.open_session(kitty=True, force_blocks=True)
        session.wait_for(b"Set wallpaper")
        session.wait_for(b"38;2;12;34;56")
        session.send(b"Fq")
        session.wait_for(b"exit:0", events=True)
        session.wait_for(b"restored:True", events=True)
        self.assertIn(b"38;2;12;34;56", session.output)
        self.assertNotIn(b"\x1b_G", session.output)


if __name__ == "__main__":
    unittest.main()
