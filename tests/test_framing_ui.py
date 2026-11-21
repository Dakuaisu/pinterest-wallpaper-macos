import os
import unittest
from dataclasses import replace
from unittest import mock

from PIL import Image

import pinterest_wallpaper as pw
from helpers import Sandbox
from wallpaper_framing import Framing
from wallpaper_tui import Session
from wallpaper_tui_backend import Backend, Draft, Pin
from wallpaper_tui_terminal import Size


class FramingBackend:
    def __init__(self):
        self.items = [Pin("one", "1920x1080 source", 0, False)]
        self.events = []
        self.action_busy = False

    def pins(self):
        return list(self.items)

    def overview(self):
        return False, "Rotation on", 2

    def preview_task(self, pin, draft, original, bounds, *, display=1, focus_editor=False):
        def preview():
            self.events.append(("preview", pin, draft, original, display, focus_editor))
            framing = draft.framing if draft.framing_changed else pin.framing
            return Image.new("RGB", (8, 4), (framing.zoom if framing else 0, display, 0))

        return preview

    def save(self, pin, draft):
        current = self.items[0]
        if draft.framing_changed and current.framing != pin.framing:
            raise ValueError("framing changed since this pin was loaded")
        saved = replace(current, angle=current.angle if draft.angle is None else draft.angle,
                        fill=current.fill if draft.fill is None else draft.fill,
                        focus=draft.focus if draft.focus_changed else current.focus,
                        framing=draft.framing if draft.framing_changed else current.framing)
        self.events.append(("save", pin, draft))
        self.items[0] = saved
        return saved

    def apply(self, pin, draft):
        self.events.append(("apply", pin, draft))


class Screen:
    def draw(self, content, image, box, size):
        self.content = content


class FramingEditorTests(unittest.TestCase):
    def setUp(self):
        self.backend = FramingBackend()
        self.session = Session(self.backend, Draft)
        self.addCleanup(self.session.worker.close)

    def keys(self, *keys):
        for key in keys:
            self.session.handle(key)

    def image(self):
        self.session.preview((80, 80))
        self.assertTrue(self.session.worker.ready.wait(2))
        return self.session.preview((80, 80))

    def test_opening_editor_does_not_create_a_draft(self):
        self.keys("f")
        self.assertEqual(self.session.controls.mode, "framing")
        self.assertEqual(self.session.drafts, {})
        self.assertEqual(self.backend.events, [])

    def test_proportional_arrows_change_zoom_and_cycle_aspect(self):
        self.keys("f", "up", "right")
        self.assertEqual(self.session.draft.framing, Framing("proportional", "original", 105))
        self.keys("left", "down")
        self.assertEqual(self.session.draft.framing, Framing("proportional"))

    def test_stretch_arrows_adjust_independent_dimensions(self):
        self.keys("f", "m", "right", "up")
        self.assertEqual(self.session.draft.framing, Framing("stretch", width=105, height=105))
        self.keys("left", "down")
        self.assertEqual(self.session.draft.framing, Framing("stretch"))

    def test_percentages_are_clamped_and_aspects_cycle(self):
        self.keys("f")
        for _ in range(100):
            self.keys("down")
        self.assertEqual(self.session.draft.framing.zoom, 25)
        for _ in range(100):
            self.keys("up")
        self.assertEqual(self.session.draft.framing.zoom, 400)
        self.keys("left")
        self.assertEqual(self.session.draft.framing.aspect, "21:9")
        self.keys("right")
        self.assertEqual(self.session.draft.framing.aspect, "display")

    def test_reset_stages_removal_of_saved_custom_framing(self):
        self.backend.items[0] = replace(self.backend.items[0], framing=Framing("stretch", width=150))
        self.session.reload()
        self.keys("f", "0", "enter")
        self.assertTrue(self.session.draft.framing_changed)
        self.assertIsNone(self.session.draft.framing)
        self.keys("s")
        self.assertIsNone(self.backend.items[0].framing)

    def test_cancel_only_discards_the_current_edit(self):
        self.keys("r", "c", "right", "enter", "f", "up", "enter")
        previous = self.session.draft
        self.keys("f", "m", "right", "esc")
        self.assertEqual(self.session.draft, previous)
        self.assertEqual(self.session.draft.angle, 90)
        self.assertTrue(self.session.draft.focus_changed)

    def test_legacy_fill_toggle_clears_custom_framing_and_preserves_other_edits(self):
        self.backend.items[0] = replace(self.backend.items[0], framing=Framing("proportional", zoom=125))
        self.session.reload()
        self.keys("r", "F")
        self.assertEqual(self.session.draft.angle, 90)
        self.assertTrue(self.session.draft.fill)
        self.assertTrue(self.session.draft.framing_changed)
        self.assertIsNone(self.session.draft.framing)

    def test_accepted_edit_is_staged_until_save_or_browse_apply(self):
        self.keys("f", "up", "enter")
        self.assertEqual(self.session.draft.framing, Framing("proportional", zoom=105))
        self.assertEqual(self.backend.events, [])
        self.keys("enter")
        self.assertEqual([event[0] for event in self.backend.events], ["apply", "save"])
        self.assertEqual(self.backend.items[0].framing, Framing("proportional", zoom=105))

    def test_display_cycle_and_original_comparison_update_live_preview(self):
        self.keys("v", "f", "up")
        first = self.image()
        self.assertEqual(first.getpixel((0, 0)), (105, 1, 0))
        self.assertFalse(self.backend.events[-1][3])
        self.keys("]")
        second = self.image()
        self.assertEqual(second.getpixel((0, 0)), (105, 2, 0))
        self.keys("esc")
        self.image()
        self.assertTrue(self.backend.events[-1][3])

    def test_saved_framing_change_invalidates_preview_cache(self):
        self.image()
        self.backend.items[0] = replace(self.backend.items[0], framing=Framing("proportional", zoom=125))
        self.session.reload()
        image = self.image()
        self.assertEqual(image.getpixel((0, 0)), (125, 1, 0))

    def test_external_change_during_edit_remains_a_stale_conflict(self):
        self.keys("f", "up")
        self.backend.items[0] = replace(self.backend.items[0], framing=Framing("stretch", width=150))
        self.session.reload()
        self.keys("enter", "s")
        self.assertIn("framing changed", self.session.message)
        self.assertEqual(self.backend.items[0].framing, Framing("stretch", width=150))
        self.assertTrue(self.session.draft.framing_changed)

    def test_cancel_after_reload_retains_earlier_stale_draft(self):
        self.keys("f", "up", "enter", "f", "up")
        self.backend.items[0] = replace(self.backend.items[0], framing=Framing("stretch", width=150))
        self.session.reload()
        self.keys("esc", "s")
        self.assertIn("framing changed", self.session.message)
        self.assertEqual(self.session.draft.framing, Framing("proportional", zoom=105))

    def test_minimum_terminal_shows_values_and_editor_controls(self):
        self.keys("f", "up")
        screen = Screen()
        self.session.render(screen, Size(36, 10, 2.0))
        for text in ("proportional", "display", "105%", "ratio", "zoom", "swap-mode", "reset-legacy", "Esc"):
            self.assertIn(text, screen.content)
        self.keys("m", "right")
        self.session.render(screen, Size(36, 10, 2.0))
        for text in ("stretch", "105%", "100%", "width", "height"):
            self.assertIn(text, screen.content)


class FramingPtyBackend(FramingBackend):
    def __init__(self, event_fd, failure):
        super().__init__()
        self.event_fd = event_fd

    def save(self, pin, draft):
        saved = super().save(pin, draft)
        os.write(self.event_fd, b"saved\n")
        return saved

    def apply(self, pin, draft):
        super().apply(pin, draft)
        os.write(self.event_fd, b"applied\n")


class FramingPtyTests(unittest.TestCase):
    def test_open_editor_redraws_and_keeps_draft_through_resize(self):
        from test_tui_pty import PtySession

        with mock.patch("test_tui_pty.Draft", Draft), mock.patch("test_tui_pty.PtyBackend", FramingPtyBackend):
            terminal = PtySession(cols=80, rows=24)
        try:
            terminal.wait_for(b"Pinwall")
            terminal.send(b"f\x1b[A")
            status = b"D1 proportional display 105%"
            terminal.wait_for(status)
            for cols, rows in ((36, 10), (80, 24)):
                count = terminal.output.count(status)
                terminal.resize(cols, rows)
                terminal.wait_for(status, count=count + 1)
                self.assertIn(b"m:swap-mode 0:reset-legacy ]:disp", terminal.output)
            self.assertNotIn(b"saved", terminal.events)
            self.assertNotIn(b"applied", terminal.events)
            terminal.send(b"\x1b")
            terminal.send(b"q")
            terminal.wait_for(b"exit:0", events=True)
            terminal.wait_for(b"restored:True", events=True)
        finally:
            terminal.close()

    def test_editor_stages_then_saves_in_a_real_pty(self):
        from test_tui_pty import PtySession

        for cols, rows in ((80, 24), (36, 10)):
            with self.subTest(cols=cols, rows=rows):
                with mock.patch("test_tui_pty.Draft", Draft), mock.patch("test_tui_pty.PtyBackend", FramingPtyBackend):
                    terminal = PtySession(cols=cols, rows=rows)
                try:
                    terminal.wait_for(b"Pinwall")
                    terminal.send(b"f\x1b[A")
                    terminal.wait_for(b"105%")
                    terminal.send(b"]")
                    terminal.wait_for(b"D2")
                    terminal.send(b"\r")
                    terminal.wait_for(b"Draft: s save, Enter apply")
                    self.assertNotIn(b"saved", terminal.events)
                    self.assertNotIn(b"applied", terminal.events)
                    terminal.send(b"s")
                    terminal.wait_for(b"saved\n", events=True)
                    self.assertNotIn(b"applied", terminal.events)
                    terminal.send(b"q")
                    terminal.wait_for(b"exit:0", events=True)
                    terminal.wait_for(b"restored:True", events=True)
                finally:
                    terminal.close()


class FramingSandboxTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.add_pin("123", size=(160, 80))
        self.config.update(resolution="auto", fit_mode="cover", crop="center", max_upscale=0)
        pw.save_json(pw.CONFIG_FILE, self.config)
        self.screens[:] = [dict(self.screens[0], id=1, width=160, height=80),
                           dict(self.screens[0], id=2, width=80, height=80)]
        with pw.locked_state() as state:
            state["screens"] = self.screens
        self.session = Session(Backend(pw, self.config), Draft)
        self.addCleanup(self.session.worker.close)

    def test_real_preview_and_save_keep_rotation_and_focus_without_desktop_change(self):
        for key in ("r", "c", "right", "enter", "f", "up", "right", "]"):
            self.session.handle(key)
        self.session.preview((80, 80))
        self.assertTrue(self.session.worker.ready.wait(2))
        image = self.session.preview((80, 80))
        self.assertEqual(image.size, (80, 80))
        self.assertEqual(self.shown, [])
        self.session.handle("enter")
        self.assertNotIn("123", self.state().get("framing", {}))
        self.session.handle("s")
        state = self.state()
        self.assertEqual(state["framing"]["123"], Framing("proportional", "original", 105)._asdict())
        self.assertEqual(state["rotations"]["123"], 90)
        self.assertEqual(state["crop_focus"]["123"], [0.5, 0.45])
        self.assertEqual(self.shown, [])

    def test_real_backend_rejects_stale_framing_after_reload(self):
        self.session.handle("f")
        with pw.locked_state() as state:
            state.setdefault("framing", {})["123"] = Framing("stretch", width=150)._asdict()
        self.session.reload()
        for key in ("up", "enter", "s"):
            self.session.handle(key)
        self.assertIn("framing changed since", self.session.message)
        self.assertEqual(self.state()["framing"]["123"], Framing("stretch", width=150)._asdict())
        self.assertEqual(self.shown, [])
