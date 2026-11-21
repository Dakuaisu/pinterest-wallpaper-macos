import base64
import io
import os
import re
import struct
import unittest
from unittest import mock

from PIL import Image
from test_tui_pty import PtySession

import wallpaper_tui_terminal as terminal
from wallpaper_tui_graphics import GraphicsReplies

VSCODE = {"TERM": "xterm-256color", "TERM_PROGRAM": "vscode"}
FORCED = dict(VSCODE, PINTEREST_WALLPAPER_IMAGE="iterm")
QUERY = b"\x1b]1337;ReportCellSize\x07"
REPORT = b"\x1b]1337;ReportCellSize=24.000;12.000;2.000\x1b\\"


class ItermGraphicsTests(unittest.TestCase):
    def open_session(self, environment):
        session = PtySession(environment=environment)
        self.addCleanup(session.close)
        return session

    def test_forced_vscode_sends_inline_png_without_a_probe(self):
        session = self.open_session(FORCED)
        session.wait_for(b"\x1b]1337;File=")
        match = re.search(rb"\x1b\]1337;File=([^:]*):([A-Za-z0-9+/=]+)(?:\x07|\x1b\\)", session.output)
        self.assertIsNotNone(match)
        options = dict(field.split(b"=", 1) for field in match.group(1).split(b";"))
        png = base64.b64decode(match.group(2), validate=True)
        self.assertEqual(int(options[b"size"]), len(png))
        self.assertEqual(options[b"inline"], b"1")
        self.assertGreater(int(options[b"width"]), 0)
        self.assertGreater(int(options[b"height"]), 0)
        with Image.open(io.BytesIO(png)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertLessEqual(max(image.size), 1024)
        self.assertNotIn(b"a=q", session.output)
        session.send(b"q")
        session.wait_for(b"exit:0", events=True)
        session.wait_for(b"restored:True", events=True)

    def test_force_iterm_cannot_bypass_host_and_mux_safety(self):
        for environment in (
            {"TERM_PROGRAM": "Apple_Terminal", "PINTEREST_WALLPAPER_IMAGE": "iterm"},
            {"TERM_PROGRAM": "unknown", "PINTEREST_WALLPAPER_IMAGE": "iterm"},
            dict(FORCED, TMUX="1"),
            dict(FORCED, STY="1"),
            dict(FORCED, TERM="tmux-256color"),
            dict(FORCED, TERM="screen-256color"),
        ):
            with self.subTest(environment=environment):
                session = self.open_session(environment)
                session.wait_for("▀".encode())
                self.assertNotIn(b"\x1b]1337;", session.output)
                self.assertNotIn(b"\x1b_G", session.output)
                session.send(b"q")
                session.wait_for(b"exit:0", events=True)

    def test_vscode_uses_verified_iterm_after_kitty_error(self):
        session = self.open_session(VSCODE)
        session.wait_for(QUERY)
        probe_id = re.search(rb"a=q,[^;]*,i=(\d+);", session.output).group(1)
        session.send(b"\x1b[" + REPORT + b"\x1b_Gi=" + probe_id + b";EINVAL\x1b\\BFr\r")
        session.wait_for(b"\x1b]1337;File=")
        session.wait_for(b'["apply", "two", 180, false]', events=True)
        self.assertNotIn(b"a=T,f=100", session.output)
        session.send(b"q")
        session.wait_for(b"exit:0", events=True)

    def test_verified_kitty_wins_over_iterm(self):
        session = self.open_session(VSCODE)
        session.wait_for(QUERY)
        probe_id = re.search(rb"a=q,[^;]*,i=(\d+);", session.output).group(1)
        session.send(REPORT + b"\x1b_Gi=" + probe_id + b";OK\x1b\\")
        session.wait_for(b"a=T,f=100")
        self.assertNotIn(b"\x1b]1337;File=", session.output)
        session.send(b"q")
        session.wait_for(b"exit:0", events=True)

    def test_late_fragmented_report_preserves_arrow_without_switching_mode(self):
        session = self.open_session(VSCODE)
        session.wait_for(QUERY)
        session.wait_for(b"38;2;12;34;56")
        session.send(b"\x1b[" + REPORT[:20])
        session.send(REPORT[20:] + b"BFr\r")
        session.wait_for(b'["apply", "two", 180, false]', events=True)
        self.assertNotIn(b"\x1b]1337;File=", session.output)
        session.send(b"q")
        session.wait_for(b"exit:0", events=True)

    def test_blocks_bounds_match_the_half_block_raster(self):
        with mock.patch.dict(os.environ, {"PINTEREST_WALLPAPER_IMAGE": "blocks"}, clear=True):
            display = terminal.Terminal()
        bounds = display.preview_bounds(terminal.Box(1, 1, 30, 12), terminal.Size(80, 24, 2.0))
        self.assertEqual(bounds, (60, 24))
        with mock.patch.dict(os.environ, {"PINTEREST_WALLPAPER_IMAGE": "half"}, clear=True):
            display = terminal.Terminal()
        bounds = display.preview_bounds(terminal.Box(1, 1, 30, 12), terminal.Size(80, 24, 2.0))
        self.assertEqual(bounds, (30, 24))

    def test_native_bounds_use_reported_cell_pixels_and_fit_cap(self):
        with mock.patch.dict(os.environ, FORCED, clear=True):
            display = terminal.Terminal()
        size = terminal.Size(120, 40, 2.0, 14.0, 28.0)
        self.assertEqual(display.preview_bounds(terminal.Box(1, 1, 30, 12), size), (420, 336))
        self.assertEqual(display.preview_bounds(terminal.Box(1, 1, 200, 100), size), (1024, 1024))

    def test_size_carries_ioctl_pixel_metrics_into_preview_bounds(self):
        with mock.patch.dict(os.environ, FORCED, clear=True):
            display = terminal.Terminal()
        data = struct.pack("HHHH", 40, 120, 1440, 960)
        with mock.patch.object(terminal.fcntl, "ioctl", return_value=data):
            size = display.size()
        self.assertEqual(display.preview_bounds(terminal.Box(1, 1, 25, 10), size), (300, 240))

    def test_native_bounds_have_a_useful_fallback_without_metrics(self):
        with mock.patch.dict(os.environ, FORCED, clear=True):
            display = terminal.Terminal()
        bounds = display.preview_bounds(terminal.Box(1, 1, 20, 10), terminal.Size(80, 24, 2.0))
        self.assertGreaterEqual(bounds[0], 200)
        self.assertGreaterEqual(bounds[1], 200)

    def test_reported_cell_metrics_precede_ioctl_estimates(self):
        replies = GraphicsReplies(123)
        ordinary, answer = replies.feed(b"q" + REPORT + b"r")
        self.assertEqual((ordinary, answer, replies.cell_pixels), (b"qr", None, (24.0, 48.0)))
        with mock.patch.dict(os.environ, FORCED, clear=True):
            display = terminal.Terminal()
        display.reported_cell = replies.cell_pixels
        with mock.patch.object(terminal.fcntl, "ioctl", return_value=struct.pack("HHHH", 40, 120, 960, 640)):
            size = display.size()
        self.assertEqual(display.preview_bounds(terminal.Box(2, 2, 10, 10), size), (240, 480))

    def test_invalid_cell_report_does_not_verify_iterm(self):
        replies = GraphicsReplies(123)
        ordinary, answer = replies.feed(b"\x1b[\x1b]1337;ReportCellSize=0;12;2\x07B")
        self.assertEqual((ordinary, answer, replies.cell_pixels), (b"\x1b[B", None, None))

    def test_draw_clips_the_native_footprint_to_the_terminal(self):
        with mock.patch.dict(os.environ, FORCED, clear=True):
            display = terminal.Terminal()
        frames = []
        display.write = frames.append
        display.draw("", Image.new("RGB", (4, 4)), terminal.Box(9, 37, 20, 10), terminal.Size(40, 10, 2.0))
        self.assertIn("\x1b[9;37H\x1b]1337;File=", frames[-1])
        self.assertIn("width=4;height=2;", frames[-1])

    def test_replacement_resize_and_mode_change_purge_the_previous_image(self):
        with mock.patch.dict(os.environ, FORCED, clear=True):
            display = terminal.Terminal()
        frames = []
        display.write = frames.append
        image = Image.new("RGB", (64, 64), "red")
        display.draw("first", image, terminal.Box(3, 3, 16, 8), terminal.Size(80, 24, 2.0))

        display.draw("resized", image, terminal.Box(2, 2, 4, 2), terminal.Size(40, 12, 2.0))
        self.assertTrue(frames[-1].startswith("\x1b[?1049l\x1b[?1049h\x1b[2Jresized"))
        self.assertIn("width=4;height=2;", frames[-1])

        display.image_mode = "blocks"
        display.draw("overlay", None, None, terminal.Size(40, 12, 2.0))
        self.assertTrue(frames[-1].startswith("\x1b[?1049l\x1b[?1049h\x1b[2Joverlay"))
        self.assertNotIn("\x1b]1337;File=", frames[-1])

    def test_iterm_exit_clears_before_leaving_the_alternate_screen(self):
        session = self.open_session(FORCED)
        session.wait_for(b"\x1b]1337;File=")
        session.send(b"q")
        session.wait_for(b"\x1b[?1049l")
        session.wait_for(b"restored:True", events=True)
        self.assertTrue(session.output.endswith(b"\x1b[2J\x1b[0m\x1b[?25h\x1b[?1049l"))


if __name__ == "__main__":
    unittest.main()
