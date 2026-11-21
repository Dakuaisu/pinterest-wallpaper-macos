import base64
import importlib
import io
import random
import re
import sys
import unittest
from dataclasses import dataclass, replace
from unittest import mock

from PIL import Image

import pinterest_wallpaper as pw
from helpers import Sandbox


@dataclass(frozen=True)
class Pin:
    key: str
    note: str
    angle: int
    fill: bool
    boards: tuple[str, ...] = ()
    favorite: bool = False


@dataclass(frozen=True)
class Draft:
    angle: int = None
    fill: bool = None


class FakeBackend:
    def __init__(self):
        self.items = [Pin("one", "First", 0, False), Pin("two", "Second", 90, True)]
        self.events = []
        self.failure = ""
        self.action_busy = False
        self.action_result = None
        self.display_pins = {1: "screen-1-pin", 2: "screen-2-pin"}
        self.display_count = 1
        self.paused = False
        self.banned = []
        self.off = set()

    def overview(self):
        return self.paused, "Paused" if self.paused else "Rotation on", self.display_count

    def toggle_favorite(self, pin):
        self.events.append(("favorite", pin.key))
        if self.failure == "favorite":
            raise ValueError("cannot favorite")
        self.items = [replace(item, favorite=not item.favorite) if item.key == pin.key else item for item in self.items]
        return not pin.favorite

    def add_board(self, text):
        if "pinterest.com/" not in text:
            raise ValueError("That isn't a board link.")
        return "boards", f'["old","{text}"]', '["old"]'

    def disabled_boards(self):
        return set(self.off)

    def toggle_board(self, url):
        self.off ^= {url}
        return url in self.off

    def remove_board(self, url):
        if url == "only":
            raise ValueError("This is your only board.")
        return "boards", '["kept"]', f'["kept","{url}"]'

    def ban(self, key):
        self.events.append(("ban", key))
        self.banned.append(key)
        self.items = [item for item in self.items if item.key != key]

    def pins(self):
        if self.failure == "pins":
            raise ValueError("cannot list")
        return list(self.items)

    def preview_task(self, pin, draft, original, bounds):
        return lambda: self.preview(pin, draft, original)

    def preview(self, pin, draft, original):
        self.events.append(("preview", pin.key, draft.angle, draft.fill, original))
        if self.failure == "preview":
            raise ValueError("cannot preview")
        return Image.new("RGB", (8, 4), (12, 34, 56))

    def save(self, pin, draft):
        self.events.append(("save", pin.key, draft.angle, draft.fill))
        if self.failure == "save":
            raise ValueError("cannot save")
        result = Pin(pin.key, pin.note, pin.angle if draft.angle is None else draft.angle,
                     pin.fill if draft.fill is None else draft.fill, pin.boards, pin.favorite)
        self.items = [result if item.key == pin.key else item for item in self.items]
        return result

    def apply(self, pin, draft):
        self.events.append(("apply", pin.key, draft.angle, draft.fill))
        if self.failure == "apply":
            raise ValueError("cannot apply")

    def boards(self):
        return []

    def rotation(self):
        return []

    def bans(self):
        return []

    def settings(self):
        return []

    def displayed_pin(self, display):
        return self.display_pins.get(display)

    def start_action(self, command, args=()):
        self.events.append(("action", command, args))
        self.action_busy = True

    def take_action(self):
        result, self.action_result = self.action_result, None
        if result is not None:
            self.action_busy = False
        return result

    def close_actions(self):
        self.action_busy = False


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.session = importlib.import_module("wallpaper_tui").Session(self.backend, Draft)
        self.addCleanup(self.session.worker.close)

    def test_enter_sets_the_wallpaper_and_remembers_the_adjustments(self):
        self.session.handle("F")
        self.session.handle("r")
        keep_running = self.session.handle("enter")
        self.assertTrue(keep_running)
        self.assertEqual(self.backend.events[-2:], [("apply", "one", 90, True), ("save", "one", 90, True)])
        self.assertEqual(self.session.draft, Draft())
        self.assertEqual((self.session.pin.angle, self.session.pin.fill), (90, True))
        self.assertEqual(self.session.message, "Wallpaper set")

    def test_enter_without_adjustments_only_applies(self):
        self.session.handle("enter")
        self.assertEqual(self.backend.events, [("apply", "one", None, None)])

    def test_space_toggles_favorite_and_keeps_the_selection(self):
        self.session.handle("down")
        self.session.handle(" ")
        self.assertEqual(self.backend.events[-1], ("favorite", "two"))
        self.assertTrue(self.session.pin.favorite)
        self.assertEqual(self.session.pin.key, "two")
        self.assertEqual(self.session.message, "Added to favorites")

    def test_vim_and_paging_keys_move_the_selection(self):
        self.backend.items = [Pin(str(number), "", 0, False) for number in range(30)]
        session = importlib.import_module("wallpaper_tui").Session(self.backend, Draft)
        self.addCleanup(session.worker.close)
        for key, expected in (("j", 1), ("k", 0), ("pgdn", 10), ("end", 29), ("pgup", 19), ("home", 0), ("G", 29), ("g", 0)):
            session.handle(key)
            self.assertEqual(session.index, expected, key)

    def test_saving_without_adjustments_explains_itself(self):
        self.session.handle("s")
        self.assertEqual(self.backend.events, [])
        self.assertIn("Nothing to save", self.session.message)

    def test_drafts_follow_each_pin_and_save_clears_only_selected(self):
        self.session.handle("r")
        self.session.handle("down")
        self.session.handle("F")
        self.session.handle("s")
        self.assertEqual(self.backend.events[-1], ("save", "two", None, False))
        self.assertEqual(self.session.draft, Draft())
        self.session.handle("up")
        self.assertEqual(self.session.draft, Draft(90, None))
        self.assertFalse(self.session.handle("q"))

    def test_preview_toggle_passes_the_same_draft_to_backend(self):
        self.session.handle("r")
        self.session.handle("v")
        self.session.preview()
        self.assertTrue(self.session.worker.ready.wait(2))
        image = self.session.preview()
        self.assertEqual(image.getpixel((0, 0)), (12, 34, 56))
        self.assertEqual(self.backend.events[-1], ("preview", "one", 90, None, True))

    def test_failed_save_keeps_draft_and_reports_error(self):
        self.backend.failure = "save"
        self.session.handle("r")
        self.session.handle("s")
        self.assertEqual(self.session.draft, Draft(90, None))
        self.assertIn("cannot save", self.session.message)

    def test_list_failure_and_empty_collection_remain_quittable(self):
        self.backend.failure = "pins"
        session = importlib.import_module("wallpaper_tui").Session(self.backend, Draft)
        self.assertIn("cannot list", session.message)
        self.assertIsNone(session.pin)
        self.assertFalse(session.handle("q"))

    def test_empty_collection_remains_quittable(self):
        self.backend.items = []
        session = importlib.import_module("wallpaper_tui").Session(self.backend, Draft)
        self.assertIsNone(session.pin)
        self.assertTrue(session.handle("r"))
        self.assertFalse(session.handle("q"))

    def test_failed_apply_preserves_draft_and_remains_usable(self):
        self.backend.failure = "apply"
        self.session.handle("F")
        self.assertTrue(self.session.handle("enter"))
        self.assertEqual(self.session.draft, Draft(None, True))
        self.assertIn("cannot apply", self.session.message)
        self.assertFalse(any(event[0] == "save" for event in self.backend.events))

    def test_preview_failure_is_visible(self):
        self.backend.failure = "preview"
        self.assertIsNone(self.session.preview())
        self.assertTrue(self.session.worker.ready.wait(2))
        self.assertIsNone(self.session.preview())
        self.assertIn("cannot preview", self.session.message)

    def test_list_identifies_pins_even_when_notes_are_identical(self):
        self.backend.items = [Pin("one", "same", 0, False), Pin("two", "same", 0, False)]
        session = importlib.import_module("wallpaper_tui").Session(self.backend, Draft)

        class Screen:
            def draw(self, content, image, box, size):
                self.content = content

        terminal = importlib.import_module("wallpaper_tui_terminal")
        screen = Screen()
        session.render(screen, terminal.Size(80, 24, 2.0))
        self.assertIn(" 1 same", screen.content)
        self.assertIn(" 2 same", screen.content)

    def test_opaque_ids_stay_out_of_the_list(self):
        key = "shared-prefix-" + "a" * 20
        self.backend.items = [Pin(key, "1920x1080 source", 0, False)]
        session = importlib.import_module("wallpaper_tui").Session(self.backend, Draft)

        class Screen:
            def draw(self, content, image, box, size):
                self.content = content

        terminal = importlib.import_module("wallpaper_tui_terminal")
        screen = Screen()
        session.render(screen, terminal.Size(80, 24, 2.0))
        self.assertNotIn("shared-prefix", screen.content)
        self.assertIn("1920×1080", screen.content)

    def test_status_describes_selected_pin_not_last_visible_pin(self):
        class Screen:
            def draw(self, content, image, box, size):
                self.content = content

        terminal = importlib.import_module("wallpaper_tui_terminal")
        screen = Screen()
        self.session.handle("down")
        self.session.handle("r")
        self.session.render(screen, terminal.Size(100, 24, 2.0))
        self.assertIn(" Second · turned 180° · fills screen · not saved ", screen.content)
        self.session.render(screen, terminal.Size(60, 24, 2.0))
        self.assertIn(" Second · 180° · fill · unsaved ", screen.content)
        self.session.render(screen, terminal.Size(36, 10, 2.0))
        self.assertIn(" fill · unsaved ", screen.content)

    def test_run_rejects_noninteractive_streams_without_backend_calls(self):
        output = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO()), mock.patch.object(sys, "stderr", output):
            result = importlib.import_module("wallpaper_tui").run(self.backend)
        self.assertEqual(result, 1)
        self.assertIn("interactive terminal", output.getvalue())
        self.assertEqual(self.backend.events, [])


class BackendIntegrationTests(Sandbox):
    def test_real_backend_preview_and_save_stay_in_sandbox(self):
        from wallpaper_tui import Session
        from wallpaper_tui_backend import Backend
        from wallpaper_tui_backend import Draft as BackendDraft

        self.add_pin("one", size=(8, 4), color="red")
        session = Session(Backend(pw, self.config), BackendDraft)
        self.addCleanup(session.worker.close)
        session.preview()
        self.assertTrue(session.worker.ready.wait(2))
        self.assertIsNotNone(session.preview())
        session.handle("r")
        session.handle("F")
        session.handle("s")
        self.assertEqual(session.draft, BackendDraft())
        self.assertEqual(self.state()["rotations"]["one"], 90)
        self.assertTrue(self.state()["fill_overrides"]["one"])


class TerminalImageTests(unittest.TestCase):
    def setUp(self):
        self.terminal = importlib.import_module("wallpaper_tui_terminal")

    def test_half_blocks_encode_the_actual_top_and_bottom_pixels(self):
        image = Image.new("RGB", (1, 2), (255, 0, 0))
        image.putpixel((0, 1), (0, 0, 255))
        self.assertEqual(self.terminal.fit_image(image, 8, 8, 2.0, across=2).size[0],
                         self.terminal.fit_image(image, 8, 8, 2.0).size[0] * 2)
        output = self.terminal.half_blocks(image, 1, 1)
        self.assertIn("38;2;255;0;0", output)
        self.assertIn("48;2;0;0;255", output)
        self.assertIn("▀", output)

    def test_fit_uses_physical_cell_aspect_and_flattens_transparency(self):
        image = Image.new("RGBA", (8, 4), (255, 0, 0, 0))
        fitted = self.terminal.fit_image(image, 8, 8, 2.0)
        self.assertEqual(fitted.size, (8, 4))
        self.assertEqual(fitted.getpixel((0, 0)), (0, 0, 0))

    def test_fit_respects_reported_square_cells(self):
        image = Image.new("RGB", (8, 4), (1, 2, 3))
        fitted = self.terminal.fit_image(image, 8, 8, 1.0)
        self.assertEqual(fitted.size, (8, 8))

    def test_kitty_requires_positive_direct_detection_and_has_manual_fallback(self):
        detect = importlib.import_module("wallpaper_tui_graphics").kitty_supported
        self.assertTrue(detect({"TERM": "xterm-kitty", "KITTY_WINDOW_ID": "7"}))
        self.assertFalse(detect({"TERM": "xterm-kitty"}))
        self.assertFalse(detect({"TERM": "xterm-kitty", "KITTY_WINDOW_ID": "7", "TMUX": "yes"}))
        self.assertFalse(detect({"TERM": "xterm-kitty", "KITTY_WINDOW_ID": "7",
                                 "PINTEREST_WALLPAPER_IMAGE": "blocks"}))

    def test_clipping_counts_wide_characters_and_removes_terminal_controls(self):
        clip = self.terminal.clip_text
        self.assertEqual(clip("猫ab", 3), "猫a")
        self.assertNotIn("\x1b", clip("one\x1b[2Jtwo", 20))

    def test_clipping_keeps_zero_width_variation_selectors(self):
        self.assertEqual(self.terminal.clip_text("A\ufe0fB", 1), "A\ufe0f")

    def test_paging_keys_are_decoded(self):
        terminal = self.terminal.Terminal.__new__(self.terminal.Terminal)
        terminal.pending = bytearray(b"\x1b[5~\x1b[6~\x1b[H\x1b[F\x1b[1~\x1b[4~ ")
        self.assertEqual([terminal._next_key() for _ in range(7)],
                         ["pgup", "pgdn", "home", "end", "home", "end", " "])

    def test_fragmented_arrow_and_escape_followed_by_quit_preserve_input(self):
        terminal = self.terminal.Terminal.__new__(self.terminal.Terminal)
        terminal.pending = bytearray(b"\x1b")
        self.assertIsNone(terminal._next_key())
        terminal.pending.extend(b"[")
        self.assertIsNone(terminal._next_key())
        terminal.pending.extend(b"Bq")
        self.assertEqual(terminal._next_key(), "down")
        self.assertEqual(terminal._next_key(), "q")
        terminal.pending.extend(b"\x1by")
        self.assertEqual(terminal._next_key(), "esc")
        self.assertEqual(terminal._next_key(), "y")
        terminal.pending.extend(b"\x1b[12\rq")
        self.assertEqual(terminal._next_key(), "enter")
        self.assertEqual(terminal._next_key(), "q")

    def test_kitty_chunks_decode_to_the_original_pixels(self):
        randomizer = random.Random(8)
        pixels = bytes(randomizer.getrandbits(8) for _ in range(128 * 64 * 3))
        image = Image.frombytes("RGB", (128, 64), pixels)
        result = self.terminal.kitty_image(image, self.terminal.Box(2, 3, 16, 4), 12345)
        chunks = re.findall(r"\x1b_G([^;]*);([A-Za-z0-9+/=]*)\x1b\\", result)
        self.assertGreater(len(chunks), 1)
        self.assertIn("i=12345", chunks[0][0])
        self.assertTrue(all(len(data) <= 4096 for _, data in chunks))
        self.assertTrue(all("m=1" in control for control, _ in chunks[:-1]))
        self.assertIn("m=0", chunks[-1][0])
        with Image.open(io.BytesIO(base64.b64decode("".join(data for _, data in chunks)))) as decoded:
            self.assertEqual(decoded.tobytes(), pixels)


if __name__ == "__main__":
    unittest.main()
