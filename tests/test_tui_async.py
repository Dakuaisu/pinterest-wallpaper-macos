from __future__ import annotations

import os
import threading
import unittest
from unittest import mock

from PIL import Image
from test_tui_pty import PtyBackend, PtySession
from test_tui_session import FakeBackend

import wallpaper_tui
from wallpaper_tui import Session
from wallpaper_tui_backend import Draft, Pin
from wallpaper_tui_preview import PreviewWorker
from wallpaper_tui_terminal import Size


class BlockingBackend:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def pins(self):
        return [Pin("one", "", 0, False), Pin("two", "", 0, False)]

    def preview_task(self, pin, draft, original, bounds):
        return lambda: self.preview(pin, draft, original, bounds)

    def preview(self, pin, draft, original, size=None):
        self.started.set()
        self.release.wait(3)
        return Image.new("RGB", (8, 8), "red")


class RecordingTerminal:
    kitty = False

    def draw(self, content, image, box, size):
        self.content = content
        self.image = image


class AsyncPreviewTests(unittest.TestCase):
    def test_image_mode_change_redraws_without_kitty_changing(self):
        class SizedBackend(BlockingBackend):
            def __init__(self):
                super().__init__()
                self.bounds = []

            def preview_task(self, pin, draft, original, bounds):
                self.bounds.append(bounds)
                return lambda: self.preview(pin, draft, original, bounds)

        class ChangingTerminal(RecordingTerminal):
            image_mode = "blocks"

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def size(self):
                return Size(80, 24, 2.0)

            def preview_bounds(self, box, size):
                return (60, 36) if self.image_mode == "blocks" else (480, 288)

            def read_key(self):
                if self.image_mode == "blocks":
                    self.image_mode = "native"
                    return None
                return "q"

        backend = SizedBackend()
        self.addCleanup(backend.release.set)
        terminal = ChangingTerminal()
        with mock.patch.object(wallpaper_tui, "Terminal", return_value=terminal), \
                mock.patch.object(wallpaper_tui.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(wallpaper_tui.sys.stdout, "isatty", return_value=True):
            self.assertEqual(wallpaper_tui.run(backend), 0)
        self.assertFalse(terminal.kitty)
        self.assertEqual(backend.bounds, [(60, 36), (480, 288)])

    def test_capability_change_redraws_at_native_bounds_without_a_key(self):
        class SizedBackend(BlockingBackend):
            def __init__(self):
                super().__init__()
                self.bounds = []

            def preview_task(self, pin, draft, original, bounds):
                self.bounds.append(bounds)
                return lambda: self.preview(pin, draft, original, bounds)

        class ChangingTerminal(RecordingTerminal):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def size(self):
                return Size(80, 24, 2.0)

            def read_key(self):
                if not self.kitty:
                    self.kitty = True
                    return None
                return "q"

        backend = SizedBackend()
        self.addCleanup(backend.release.set)
        terminal = ChangingTerminal()
        with mock.patch.object(wallpaper_tui, "Terminal", return_value=terminal), \
                mock.patch.object(wallpaper_tui.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(wallpaper_tui.sys.stdout, "isatty", return_value=True):
            self.assertEqual(wallpaper_tui.run(backend), 0)
        self.assertEqual(backend.bounds, [(50, 38), (400, 304)])

    def test_preview_bounds_follow_terminal_mode_and_resize(self):
        class SizedBackend(BlockingBackend):
            def __init__(self):
                super().__init__()
                self.bounds = []

            def preview_task(self, pin, draft, original, bounds):
                self.bounds.append(bounds)
                return lambda: Image.new("RGB", (8, 8), "red")

        backend = SizedBackend()
        session = Session(backend, Draft)
        self.addCleanup(session.worker.close)
        terminal = RecordingTerminal()
        session.render(terminal, Size(80, 24, 2.0))
        terminal.kitty = True
        session.render(terminal, Size(80, 24, 2.0))
        session.render(terminal, Size(60, 18, 1.5))
        self.assertEqual(backend.bounds, [(50, 38), (400, 304), (296, 156)])

    def test_render_returns_while_preview_is_blocked(self):
        backend = BlockingBackend()
        session = Session(backend, Draft)
        terminal = RecordingTerminal()
        finished = threading.Event()

        def render():
            session.render(terminal, Size(80, 24, 2.0))
            finished.set()

        thread = threading.Thread(target=render, daemon=True)
        thread.start()
        try:
            self.assertTrue(backend.started.wait(1), "preview never started")
            self.assertTrue(finished.wait(0.5), "render blocked on image decoding")
        finally:
            backend.release.set()
            thread.join(2)
            session.worker.close()

    def test_navigation_and_quit_work_in_pty_while_preview_is_blocked(self):
        def blocked(backend, pin, draft, original):
            os.write(backend.event_fd, b"preview-blocked\n")
            threading.Event().wait(30)
            return Image.new("RGB", (8, 8), "red")

        with mock.patch.object(PtyBackend, "preview", blocked):
            session = PtySession()
            self.addCleanup(session.close)
            session.wait_for(b"preview-blocked", events=True)
            session.send(b"\x1b[B")
            session.wait_for(b" 2 of 2 ")
            session.send(b"q")
            session.wait_for(b"exit:0", events=True)
            session.wait_for(b"restored:True", events=True)
            self.assertNotIn(b'"apply"', session.events)
            self.assertNotIn(b'"save"', session.events)

    def test_latest_pending_request_replaces_intermediate_and_rejects_stale_result(self):
        worker = PreviewWorker()
        self.addCleanup(worker.close)
        started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        calls = []

        def first():
            calls.append("first")
            started.set()
            release.wait(3)
            return Image.new("RGB", (1, 1), "red")

        def task(name, color):
            def run():
                calls.append(name)
                return Image.new("RGB", (1, 1), color)
            return run

        worker.request("first", lambda: first)
        self.assertTrue(started.wait(1))
        worker.request("middle", lambda: task("middle", "green"))
        worker.request("last", lambda: task("last", "blue"))
        release.set()
        self.assertTrue(worker.ready.wait(2))
        result = worker.request("last", lambda: task("last", "blue"))
        self.assertEqual(calls, ["first", "last"])
        self.assertEqual(result.image.getpixel((0, 0)), (0, 0, 255))

    def test_cache_reuses_recent_result_and_evicts_least_recent(self):
        worker = PreviewWorker(capacity=2)
        self.addCleanup(worker.close)
        calls = []

        def request(name):
            def render():
                calls.append(name)
                return Image.new("RGB", (1, 1), "red")
            result = worker.request(name, lambda: render)
            if result.pending:
                self.assertTrue(worker.ready.wait(2))
                result = worker.request(name, lambda: render)
            self.assertIsNotNone(result.image)

        for name in ("a", "b", "a", "c", "b"):
            request(name)
        self.assertEqual(calls, ["a", "b", "c", "b"])

    def test_save_invalidates_an_otherwise_identical_cached_preview(self):
        backend = FakeBackend()
        session = Session(backend, Draft)
        self.addCleanup(session.worker.close)
        session.preview()
        self.assertTrue(session.worker.ready.wait(2))
        self.assertIsNotNone(session.preview())
        session.handle("r")
        session.handle("s")
        session.preview()
        self.assertTrue(session.worker.ready.wait(2))
        self.assertIsNotNone(session.preview())
        self.assertEqual([event[0] for event in backend.events], ["preview", "save", "preview"])


if __name__ == "__main__":
    unittest.main()
