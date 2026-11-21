import base64
import io
import os
import re
import signal
import termios
import time
import unittest
from unittest import mock

from PIL import Image
from test_tui_pty import PtySession

import wallpaper_tui_terminal as terminal
from wallpaper_tui_graphics import GraphicsReplies

VSCODE = {"TERM": "xterm-256color", "TERM_PROGRAM": "vscode"}


class TerminalProtocolTests(unittest.TestCase):
    def open_session(self, environment, trace_input=False):
        session = PtySession(environment=environment, trace_input=trace_input)
        self.addCleanup(session.close)
        return session

    def probe_id(self, session):
        session.wait_for(b"a=q")
        session.wait_for(b"\x1b\\")
        match = re.search(rb"\x1b_G([^;]*a=q[^;]*);([^;]*)\x1b\\", session.output)
        self.assertIsNotNone(match)
        control = dict(field.split(b"=", 1) for field in match.group(1).split(b","))
        self.assertEqual({key: control.get(key) for key in (b"a", b"t", b"f", b"s", b"v")},
                         {b"a": b"q", b"t": b"d", b"f": b"24", b"s": b"1", b"v": b"1"})
        self.assertEqual(len(base64.b64decode(match.group(2), validate=True)), 3)
        self.assertGreater(int(control[b"i"]), 0)
        self.assertFalse(termios.tcgetattr(session.slave)[3] & (termios.ICANON | termios.ECHO))
        return control[b"i"]

    def test_vscode_ack_enables_png_and_preserves_partial_arrow(self):
        session = self.open_session(VSCODE)
        image_id = self.probe_id(session)
        session.send(b"\x1b[\x1b_Gi=" + image_id + b";OK\x1b\\BFr\r")
        session.wait_for(b'["apply", "two", 180, false]', events=True)
        session.wait_for(b"a=T,f=100,t=d,i=")
        shown = re.search(rb"\x1b_Ga=T,f=100,t=d,i=(\d+),", session.output)
        self.assertIsNotNone(shown)
        self.assertNotEqual(image_id, shown.group(1))
        self.assertEqual(session.output.count(b"a=q"), 1)
        session.send(b"q")
        session.wait_for(b"exit:0", events=True)
        session.wait_for(b"restored:True", events=True)

    def test_vscode_with_inherited_kitty_markers_still_requires_probe(self):
        session = self.open_session(dict(VSCODE, TERM="xterm-kitty", KITTY_WINDOW_ID="42"))
        image_id = self.probe_id(session)
        session.send(b"\x1b_Gi=" + image_id + b";OK\x1b\\")
        session.wait_for(b"a=T,f=100,t=d,i=")
        session.send(b"q")
        session.wait_for(b"exit:0", events=True)

    def test_silent_vscode_falls_back_within_budget_and_keeps_keys(self):
        session = self.open_session(VSCODE)
        self.probe_id(session)
        started = time.monotonic()
        session.wait_for(b"\x1b[?1049h")
        self.assertLess(time.monotonic() - started, 1.0)
        session.wait_for(b"38;2;12;34;56")
        self.assertNotIn(b"a=T,f=100", session.output)
        session.send(b"\x1b[BFr\r")
        session.wait_for(b'["apply", "two", 180, false]', events=True)
        session.send(b"q")
        session.wait_for(b"exit:0", events=True)

    def test_arrow_split_during_probe_and_after_timeout_is_preserved(self):
        session = self.open_session(VSCODE, trace_input=True)
        self.probe_id(session)
        for fragment in (b"\x1b", b"["):
            session.send(fragment)
            session.wait_for(b"input:" + fragment.hex().encode("ascii") + b"\n", events=True)
        session.wait_for(b"\x1b[?1049h")
        session.send(b"BFr\r")
        session.wait_for(b'["apply", "two", 180, false]', events=True)
        session.send(b"q")
        session.wait_for(b"exit:0", events=True)

    def test_late_fragmented_reply_does_not_interrupt_arrow_or_become_keys(self):
        session = self.open_session(VSCODE, trace_input=True)
        image_id = self.probe_id(session)
        session.wait_for(b"\x1b[?1049h")
        for fragment in (b"\x1b", b"[", b"\x1b_Gi=" + image_id + b";EINVAL:query-", b"failed\x1b\\"):
            session.send(fragment)
            session.wait_for(b"input:" + fragment.hex().encode("ascii") + b"\n", events=True)
        session.send(b"BFr\r")
        session.wait_for(b'["apply", "two", 180, false]', events=True)
        self.assertNotIn(b"exit:", session.events)
        self.assertNotIn(b"a=T,f=100", session.output)
        session.send(b"q")
        session.wait_for(b"exit:0", events=True)

    def test_partial_ack_completed_after_timeout_is_removed(self):
        session = self.open_session(VSCODE)
        image_id = self.probe_id(session)
        session.send(b"\x1b_Gi=" + image_id + b";O")
        session.wait_for(b"\x1b[?1049h")
        session.send(b"K\x1b\\\x1b[BFr\r")
        session.wait_for(b'["apply", "two", 180, false]', events=True)
        self.assertNotIn(b"a=T,f=100", session.output)
        session.send(b"q")
        session.wait_for(b"exit:0", events=True)

    def test_truncated_reply_resynchronizes_at_next_arrow(self):
        session = self.open_session(VSCODE, trace_input=True)
        image_id = self.probe_id(session)
        session.wait_for(b"\x1b[?1049h")
        partial = b"\x1b_Gi=" + image_id + b";O"
        session.send(partial)
        session.wait_for(b"input:" + partial.hex().encode("ascii") + b"\n", events=True)
        session.send(b"\x1b[BFr\r")
        session.wait_for(b'["apply", "two", 180, false]', events=True)
        session.send(b"q")
        session.wait_for(b"exit:0", events=True)

    def test_signal_during_probe_restores_terminal_modes(self):
        session = self.open_session(VSCODE)
        self.probe_id(session)
        os.kill(session.pid, signal.SIGTERM)
        session.wait_for(b"exit:143", events=True)
        session.wait_for(b"restored:True", events=True)
        session.wait_for(b"handler_restored:True", events=True)

    def test_wrong_id_does_not_enable_graphics(self):
        session = self.open_session(VSCODE)
        image_id = self.probe_id(session)
        other_id = str(int(image_id) + 1).encode()
        session.send(b"\x1b[\x1b_Gi=" + other_id + b";OK\x1b\\BFr\r")
        session.wait_for(b'["apply", "two", 180, false]', events=True)
        self.assertNotIn(b"a=T,f=100", session.output)
        session.send(b"q")
        session.wait_for(b"exit:0", events=True)

    def test_matching_error_uses_fallback_without_losing_input(self):
        session = self.open_session(VSCODE)
        image_id = self.probe_id(session)
        session.send(b"F\x1b_Gi=" + image_id + b";EINVAL:bad data\x1b\\r\r")
        session.wait_for(b'["apply", "one", 90, true]', events=True)
        self.assertNotIn(b"a=T,f=100", session.output)
        session.send(b"q")
        session.wait_for(b"exit:0", events=True)

    def test_vscode_override_and_multiplexers_never_probe(self):
        for exclusion in ({"PINTEREST_WALLPAPER_IMAGE": "blocks"}, {"TMUX": "1"}, {"STY": "1"}):
            with self.subTest(exclusion=exclusion):
                session = self.open_session(dict(VSCODE, **exclusion))
                session.wait_for("▀".encode())
                self.assertNotIn(b"\x1b]1337;", session.output)
                self.assertNotIn(b"\x1b_G", session.output)
                session.send(b"q")
                session.wait_for(b"exit:0", events=True)

    def test_apple_terminal_uses_256_color_without_probe(self):
        session = self.open_session({"TERM": "xterm-256color", "TERM_PROGRAM": "Apple_Terminal"})
        session.wait_for(b"\x1b[38;5;")
        self.assertRegex(session.output, rb"\x1b\[38;5;\d+m\x1b\[48;5;\d+m")
        self.assertNotIn(b"\x1b_G", session.output)
        self.assertNotIn(b"\x1b[38;2;", session.output)
        session.send(b"q")
        session.wait_for(b"exit:0", events=True)

    def test_apple_terminal_respects_explicit_truecolor(self):
        session = self.open_session({"TERM": "xterm-256color", "TERM_PROGRAM": "Apple_Terminal",
                                     "COLORTERM": "truecolor"})
        session.wait_for(b"38;2;12;34;56")
        self.assertNotIn(b"\x1b_G", session.output)
        session.send(b"q")
        session.wait_for(b"exit:0", events=True)

    def test_apple_terminal_ignores_inherited_kitty_markers(self):
        session = self.open_session({"TERM": "xterm-kitty", "TERM_PROGRAM": "Apple_Terminal",
                                     "KITTY_WINDOW_ID": "42"})
        session.wait_for(b"\x1b[38;5;")
        self.assertNotIn(b"\x1b_G", session.output)
        session.send(b"q")
        session.wait_for(b"exit:0", events=True)

    def test_kitty_draw_does_not_make_a_block_sized_image(self):
        display = terminal.Terminal()
        display.kitty = True
        display.write = lambda text: None
        image = Image.new("RGB", (4, 2), (12, 34, 56))
        with mock.patch.object(terminal, "fit_image", side_effect=AssertionError("unnecessary resize")):
            display.draw("", image, terminal.Box(1, 1, 10, 4), terminal.Size(80, 24, 2.0))

    def test_unchanged_rgb_frame_is_encoded_once_but_mutations_are_visible(self):
        display = terminal.Terminal()
        display.kitty = True
        frames = []
        display.write = frames.append
        image = Image.new("RGB", (2, 2), (12, 34, 56))
        box, size = terminal.Box(1, 1, 4, 2), terminal.Size(80, 24, 2.0)
        with mock.patch.object(image, "save", wraps=image.save) as save:
            display.draw("", image, box, size)
            display.draw("changed text", image, box, size)
            self.assertEqual(save.call_count, 1)
            image.putpixel((0, 0), (100, 101, 102))
            display.draw("", image, box, size)
            self.assertEqual(save.call_count, 2)
            self.assertEqual(save.call_args.kwargs["compress_level"], 1)
        chunks = re.findall(r"\x1b_G[^;]*;([A-Za-z0-9+/=]*)\x1b\\", frames[-1])
        with Image.open(io.BytesIO(base64.b64decode("".join(chunks)))) as decoded:
            self.assertEqual(decoded.getpixel((0, 0)), (100, 101, 102))

    def test_rgb_rendering_does_not_convert_to_rgba(self):
        image = Image.new("RGB", (2, 2), (12, 34, 56))
        with mock.patch.object(image, "convert", side_effect=AssertionError("unnecessary conversion")):
            terminal.fit_image(image, 4, 4, 2.0)
            terminal.kitty_image(image, terminal.Box(1, 1, 4, 2), 1)

    def test_rgb_with_transparency_metadata_is_composited_on_black(self):
        image = Image.new("RGB", (1, 1), (12, 34, 56))
        image.info["transparency"] = (12, 34, 56)
        fitted = terminal.fit_image(image, 1, 1, 2.0)
        self.assertEqual(fitted.getpixel((0, 0)), (0, 0, 0))
        encoded = terminal.kitty_image(image, terminal.Box(1, 1, 1, 1), 1)
        payload = re.search(r";([A-Za-z0-9+/=]+)\x1b\\", encoded).group(1)
        with Image.open(io.BytesIO(base64.b64decode(payload))) as decoded:
            self.assertEqual(decoded.getpixel((0, 0)), (0, 0, 0))

    def test_256_color_mapping_uses_nearest_xterm_cube_levels(self):
        image = Image.new("RGB", (1, 2), (50, 114, 115))
        self.assertIn("\x1b[38;5;60m\x1b[48;5;60m", terminal.half_blocks(image, 1, 1, False))

    def test_quarter_blocks_follow_the_shape_inside_a_cell(self):
        image = Image.new("RGB", (4, 2), "black")
        image.putpixel((0, 0), (255, 255, 255))
        image.putpixel((3, 0), (255, 255, 255))
        image.putpixel((3, 1), (255, 255, 255))
        output = terminal.quarter_blocks(image, 3, 5)
        self.assertEqual(output, "\x1b[3;5H\x1b[38;2;255;255;255m\x1b[48;2;0;0;0m▘▐\x1b[0m")

    def test_quarter_blocks_keep_exact_colours_where_a_cell_is_nearly_flat(self):
        image = Image.new("RGB", (2, 2), (10, 20, 30))
        image.putpixel((0, 1), (14, 24, 34))
        image.putpixel((1, 1), (14, 24, 34))
        self.assertEqual(terminal.quarter_blocks(image, 1, 1),
                         "\x1b[1;1H\x1b[38;2;10;20;30m\x1b[48;2;14;24;34m▀\x1b[0m")
        self.assertIn("\x1b[38;5;", terminal.quarter_blocks(image, 1, 1, False))

    def test_terminals_that_identify_themselves_get_real_images(self):
        from wallpaper_tui_graphics import native_graphics

        cases = (({"TERM": "xterm-ghostty"}, "kitty"), ({"TERM_PROGRAM": "ghostty", "TERM": "xterm-256color"}, "kitty"),
                 ({"TERM": "xterm-kitty", "KITTY_WINDOW_ID": "3"}, "kitty"),
                 ({"TERM_PROGRAM": "iTerm.app"}, "iterm"), ({"TERM_PROGRAM": "WezTerm"}, "iterm"),
                 ({"LC_TERMINAL": "iTerm2"}, "iterm"),
                 ({"TERM": "xterm-ghostty", "TMUX": "1"}, None), ({"TERM_PROGRAM": "vscode", "TERM": "xterm-ghostty"}, None),
                 ({"TERM_PROGRAM": "Apple_Terminal"}, None), ({"TERM": "xterm-256color"}, None),
                 ({"TERM": "xterm-ghostty", "PINTEREST_WALLPAPER_IMAGE": "blocks"}, None))
        for environment, expected in cases:
            with self.subTest(environment=environment):
                self.assertEqual(native_graphics(environment), expected)

    def test_reply_filter_handles_every_byte_separately(self):
        replies = GraphicsReplies(123)
        received = bytearray()
        answers = []
        stream = b"a\x1b[\x1b_Gi=123;OK\x1b\\Bfq"
        for value in stream:
            ordinary, answer = replies.feed(bytes([value]))
            received.extend(ordinary)
            if answer is not None:
                answers.append(answer)
        self.assertEqual(bytes(received), b"a\x1b[Bfq")
        self.assertEqual(answers, [True])

    def test_non_apc_escape_underscore_still_preserves_input(self):
        display = terminal.Terminal()
        display.pending.extend(b"\x1b_q")
        self.assertEqual(display._next_key(), "esc")
        self.assertEqual(display._next_key(), "_")
        self.assertEqual(display._next_key(), "q")


if __name__ == "__main__":
    unittest.main()
