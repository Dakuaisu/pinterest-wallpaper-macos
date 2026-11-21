from __future__ import annotations

import os
import unittest
from dataclasses import dataclass, replace
from unittest import mock

from PIL import Image

import pinterest_wallpaper as pw
from helpers import Sandbox
from wallpaper_tui import Session
from wallpaper_tui_backend import Backend
from wallpaper_tui_backend import Draft as BackendDraft


@dataclass(frozen=True)
class Pin:
    key: str = "one"
    note: str = "800x600 source"
    angle: int = 0
    fill: bool = False
    boards: tuple[str, ...] = ()
    favorite: bool = False
    focus: tuple[float, float] | None = None


@dataclass(frozen=True)
class Draft:
    angle: int | None = None
    fill: bool | None = None
    focus: tuple[float, float] | None = None
    focus_changed: bool = False


class FocusBackend:
    def __init__(self):
        self.item = Pin()
        self.display_count = 2
        self.events = []
        self.action_busy = False
        self.preview_error = ""

    def pins(self):
        return [self.item]

    def overview(self):
        return False, "Rotation on", self.display_count

    def preview_task(self, pin, draft, original, bounds, *, display=1, focus_editor=False):
        self.events.append(("preview", pin, draft, original, bounds, display, focus_editor))
        def preview():
            if self.preview_error:
                raise ValueError(self.preview_error)
            return Image.new("RGB", (2, 2), "red")

        return preview

    def save(self, pin, draft):
        self.events.append(("save", pin, draft))
        if draft.angle is not None and pin.angle != self.item.angle:
            raise ValueError("rotation changed since this pin was loaded")
        if draft.fill is not None and pin.fill != self.item.fill:
            raise ValueError("fill changed since this pin was loaded")
        if draft.focus_changed and pin.focus != self.item.focus:
            raise ValueError("focus changed since this pin was loaded")
        self.item = replace(
            self.item,
            angle=self.item.angle if draft.angle is None else draft.angle,
            fill=self.item.fill if draft.fill is None else draft.fill,
            focus=draft.focus if draft.focus_changed else self.item.focus,
        )
        return self.item

    def apply(self, pin, draft):
        self.events.append(("apply", pin, draft))


class FocusPtyBackend(FocusBackend):
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


class FocusEditorTests(unittest.TestCase):
    def setUp(self):
        self.backend = FocusBackend()
        self.session = Session(self.backend, Draft)
        self.addCleanup(self.session.worker.close)

    def test_accept_creates_draft_without_saving_or_applying(self):
        for key in ("c", "right", "enter"):
            self.session.handle(key)
        self.assertTrue(self.session.draft.focus_changed)
        self.assertGreater(self.session.draft.focus[0], 0.5)
        self.assertFalse(any(event[0] in ("save", "apply") for event in self.backend.events))

    def test_cancel_restores_prior_focus_draft_after_new_edit(self):
        for key in ("c", "right", "enter"):
            self.session.handle(key)
        prior = self.session.draft
        self.assertTrue(prior.focus_changed)
        for key in ("c", "down", "esc"):
            self.session.handle(key)
        self.assertEqual(self.session.draft, prior)

    def test_reset_clears_saved_focus_after_accept_and_save(self):
        self.backend.item = replace(self.backend.item, focus=(0.2, 0.7))
        self.session.reload()
        for key in ("c", "0", "enter", "s"):
            self.session.handle(key)
        self.assertIsNone(self.backend.item.focus)
        self.assertEqual(self.session.draft, Draft())
        self.assertFalse(any(event[0] == "apply" for event in self.backend.events))

    def test_focus_survives_rotation_and_fill_changes(self):
        for key in ("c", "right", "enter"):
            self.session.handle(key)
        focus = self.session.draft.focus
        for key in ("r", "F"):
            self.session.handle(key)
        self.assertEqual(self.session.draft.focus, focus)
        self.assertTrue(self.session.draft.focus_changed)
        self.assertEqual((self.session.draft.angle, self.session.draft.fill), (90, True))

    def test_reload_during_editor_preserves_focus_conflict_baseline(self):
        self.backend.item = replace(self.backend.item, focus=(0.2, 0.7))
        self.session.reload()
        self.session.handle("c")
        self.backend.item = replace(self.backend.item, focus=(0.8, 0.7))
        self.session.reload()
        for key in ("right", "enter", "s"):
            self.session.handle(key)
        self.assertTrue(self.session.draft.focus_changed)
        self.assertEqual(self.backend.item.focus, (0.8, 0.7))
        self.assertIn("changed since", self.session.message)

    def test_reload_after_accepted_draft_preserves_focus_conflict_baseline(self):
        for key in ("c", "right", "enter"):
            self.session.handle(key)
        self.backend.item = replace(self.backend.item, focus=(0.8, 0.7))
        self.session.reload()
        self.session.handle("s")
        self.assertTrue(self.session.draft.focus_changed)
        self.assertIn("changed since", self.session.message)

    def test_editor_preview_cycles_displays_and_never_applies(self):
        self.session.handle("c")
        self.session.preview()
        self.assertTrue(self.session.worker.ready.wait(2))
        self.session.handle("]")
        self.session.preview()
        self.assertTrue(self.session.worker.ready.wait(2))
        previews = [event for event in self.backend.events if event[0] == "preview"]
        self.assertEqual([event[-2:] for event in previews], [(1, True), (2, True)])
        self.assertFalse(any(event[0] == "apply" for event in self.backend.events))

    def test_right_nudges_in_displayed_axes_for_each_rotation(self):
        for angle, axis, direction in ((0, 0, 1), (90, 1, -1), (180, 0, -1), (270, 1, 1)):
            with self.subTest(angle=angle):
                self.backend.item = replace(self.backend.item, angle=angle, focus=(0.5, 0.5))
                self.session.reload()
                self.session.handle("c")
                self.session.handle("right")
                self.assertGreater(direction * (self.session.draft.focus[axis] - 0.5), 0)
                self.session.handle("esc")

    def test_nudges_clamp_to_the_photo_edges(self):
        self.backend.item = replace(self.backend.item, focus=(1.0, 0.0))
        self.session.reload()
        for key in ("c", "right", "up"):
            self.session.handle(key)
        self.assertEqual(self.session.draft.focus, (1.0, 0.0))

    def test_draft_rotation_controls_arrow_direction(self):
        for key in ("r", "c", "right"):
            self.session.handle(key)
        self.assertLess(self.session.draft.focus[1], 0.5)

    def test_reset_of_automatic_focus_does_not_create_a_draft(self):
        for key in ("c", "0", "enter"):
            self.session.handle(key)
        self.assertEqual(self.session.draft, Draft())
        self.assertNotIn("one", self.session.drafts)

    def test_cancel_after_reload_refreshes_an_unedited_focus(self):
        self.session.handle("c")
        self.backend.item = replace(self.backend.item, focus=(0.8, 0.7))
        self.session.reload()
        self.session.handle("esc")
        self.assertEqual(self.session.pin.focus, (0.8, 0.7))
        self.assertEqual(self.session.draft, Draft())

    def test_cancel_retains_previous_stale_focus_baseline(self):
        for key in ("c", "right", "enter", "c"):
            self.session.handle(key)
        self.backend.item = replace(self.backend.item, focus=(0.8, 0.7))
        self.session.reload()
        for key in ("down", "esc", "s"):
            self.session.handle(key)
        self.assertTrue(self.session.draft.focus_changed)
        self.assertIn("changed since", self.session.message)

    def test_cancel_keeps_prior_rotation_conflict_baseline(self):
        self.session.handle("r")
        self.backend.item = replace(self.backend.item, angle=180)
        self.session.reload()
        for key in ("c", "right", "esc", "s"):
            self.session.handle(key)
        self.assertEqual(self.session.draft.angle, 90)
        self.assertIn("rotation changed since", self.session.message)

    def test_preview_reacts_to_editor_draft_and_restores_original_view(self):
        self.session.handle("v")
        self.session.preview()
        self.assertTrue(self.session.worker.ready.wait(2))
        self.session.handle("c")
        self.session.preview()
        self.assertTrue(self.session.worker.ready.wait(2))
        self.session.handle("right")
        self.session.preview()
        self.assertTrue(self.session.worker.ready.wait(2))
        self.session.handle("esc")
        self.session.preview()
        previews = [event for event in self.backend.events if event[0] == "preview"]
        self.assertEqual([(event[3], event[-1]) for event in previews],
                         [(True, False), (False, True), (False, True)])
        self.assertNotEqual(previews[1][2], previews[2][2])
        self.assertTrue(self.session.original)

    def test_saved_focus_change_invalidates_ordinary_preview_cache(self):
        self.session.preview()
        self.assertTrue(self.session.worker.ready.wait(2))
        self.backend.item = replace(self.backend.item, focus=(0.2, 0.7))
        self.session.reload()
        self.session.preview()
        self.assertTrue(self.session.worker.ready.wait(2))
        previews = [event for event in self.backend.events if event[0] == "preview"]
        self.assertEqual([event[1].focus for event in previews], [None, (0.2, 0.7)])

    def test_reset_invalidates_editor_cache_when_focus_value_remains_none(self):
        self.backend.item = replace(self.backend.item, focus=(0.2, 0.7))
        self.session.reload()
        self.session.handle("c")
        self.session.preview()
        self.assertTrue(self.session.worker.ready.wait(2))
        self.session.handle("0")
        self.session.preview()
        self.assertTrue(self.session.worker.ready.wait(2))
        previews = [event for event in self.backend.events if event[0] == "preview"]
        self.assertEqual([(event[2].focus, event[2].focus_changed) for event in previews],
                         [(None, False), (None, True)])

    def test_preview_display_wraps_and_clamps_after_reload(self):
        self.session.handle("]")
        self.assertEqual(self.session.display, 2)
        self.session.handle("]")
        self.assertEqual(self.session.display, 1)
        self.session.handle("]")
        self.backend.display_count = 1
        self.session.reload()
        self.assertEqual(self.session.display, 1)

    def test_editor_absorbs_other_shortcuts_without_applying(self):
        for key in ("c", "s", "r", "f", "q"):
            self.assertTrue(self.session.handle(key))
        self.assertEqual(self.session.draft, Draft())
        self.assertEqual(self.backend.events, [])

    def test_small_terminal_shows_focus_status_and_actions(self):
        from wallpaper_tui_terminal import Size

        class Screen:
            kitty = False

            def draw(self, content, image, box, size):
                self.content = content

        screen = Screen()
        self.session.handle("c")
        self.session.render(screen, Size(36, 10, 2.0))
        self.assertIn("Focus D1", screen.content)
        self.assertIn("D1 auto draft  ] display", screen.content)
        self.assertIn("0 reset ↵ ok Esc back", screen.content)

    def test_editor_shows_preview_errors_instead_of_hiding_them(self):
        from wallpaper_tui_terminal import Size

        class Screen:
            kitty = False

            def draw(self, content, image, box, size):
                self.content = content

        self.backend.preview_error = "preview unavailable"
        self.session.handle("c")
        self.session.preview()
        self.assertTrue(self.session.worker.ready.wait(2))
        self.session.preview()
        screen = Screen()
        self.session.render(screen, Size(80, 24, 2.0))
        self.assertIn("preview unavailable", screen.content)

    def test_small_terminal_explains_save_and_apply_after_accept(self):
        from wallpaper_tui_terminal import Size

        class Screen:
            kitty = False

            def draw(self, content, image, box, size):
                self.content = content

        for key in ("c", "right", "enter"):
            self.session.handle(key)
        screen = Screen()
        self.session.render(screen, Size(36, 10, 2.0))
        self.assertIn("s save", screen.content)
        self.assertIn("Enter apply", screen.content)

    def test_editor_enter_does_not_apply_but_browse_enter_does(self):
        for key in ("c", "right", "enter"):
            self.session.handle(key)
        self.assertFalse(any(event[0] == "apply" for event in self.backend.events))
        self.session.handle("enter")
        self.assertEqual([event[0] for event in self.backend.events], ["apply", "save"])


class FocusPtyTests(unittest.TestCase):
    def test_keyboard_flow_in_real_pty_at_normal_and_minimum_sizes(self):
        from test_tui_pty import PtySession

        for cols, rows in ((80, 24), (36, 10)):
            with self.subTest(cols=cols, rows=rows):
                with mock.patch("test_tui_pty.Draft", Draft), mock.patch("test_tui_pty.PtyBackend", FocusPtyBackend):
                    terminal = PtySession(cols=cols, rows=rows)
                try:
                    terminal.wait_for(b"Pinwall")
                    terminal.send(b"c\x1b[C")
                    terminal.wait_for(b"D1 55%,50% draft")
                    terminal.send(b"]")
                    terminal.wait_for(b"D2 55%,50% draft")
                    terminal.send(b"\r")
                    terminal.wait_for(b"Draft: s save, Enter apply")
                    self.assertNotIn(b"applied", terminal.events)
                    self.assertNotIn(b"saved", terminal.events)
                    terminal.send(b"s")
                    terminal.wait_for(b"saved\n", events=True)
                    self.assertNotIn(b"applied", terminal.events)
                    terminal.send(b"q")
                    terminal.wait_for(b"exit:0", events=True)
                finally:
                    terminal.close()


class FocusSandboxTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.add_pin("123", size=(160, 80))
        self.config.update(resolution="auto", fit_mode="cover", crop="center", max_upscale=0)
        pw.save_json(pw.CONFIG_FILE, self.config)
        self.screens[:] = [dict(self.screens[0], id=1, width=80, height=160),
                           dict(self.screens[0], id=2, width=80, height=80)]
        with pw.locked_state() as state:
            state["screens"] = self.screens
        self.session = Session(Backend(pw, self.config), BackendDraft)
        self.addCleanup(self.session.worker.close)

    def image(self):
        self.session.preview((80, 80))
        self.assertTrue(self.session.worker.ready.wait(2))
        image = self.session.preview((80, 80))
        self.assertIsNotNone(image)
        return image

    def test_editor_uses_real_bounded_preview_and_saves_without_desktop_change(self):
        self.session.handle("c")
        first = self.image()
        self.session.handle("]")
        second = self.image()
        self.assertEqual(first.size, (80, 40))
        self.assertEqual(second.size, (80, 40))
        self.assertNotEqual(first.tobytes(), second.tobytes())
        for key in ("right", "enter"):
            self.session.handle(key)
        self.assertNotIn("123", self.state().get("crop_focus", {}))
        self.session.handle("s")
        self.assertEqual(self.state()["crop_focus"]["123"], [0.55, 0.5])
        self.assertEqual(self.shown, [])

    def test_real_backend_rejects_stale_focus_after_editor_reload(self):
        self.session.handle("c")
        with pw.locked_state() as state:
            state.setdefault("crop_focus", {})["123"] = [0.8, 0.7]
        self.session.reload()
        for key in ("right", "enter", "s"):
            self.session.handle(key)
        self.assertEqual(self.state()["crop_focus"]["123"], [0.8, 0.7])
        self.assertIn("focus changed since", self.session.message)
        self.assertTrue(self.session.draft.focus_changed)
        self.assertEqual(self.shown, [])

    def test_apply_after_accept_only_uses_stubbed_desktop(self):
        for key in ("c", "right", "enter"):
            self.session.handle(key)
        self.assertEqual(self.shown, [])
        self.session.handle("enter")
        self.assertEqual(len(self.shown), 1)
        self.assertEqual(self.state()["crop_focus"]["123"], [0.55, 0.5])


if __name__ == "__main__":
    unittest.main()
