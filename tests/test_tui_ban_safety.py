import unittest
from unittest import mock

from test_tui_control_center import ControlCase

import pinterest_wallpaper as pw
from helpers import Sandbox
from wallpaper_tui import Session

BAN = "Ban the wallpaper on screen…"


class BanSafetyTests(ControlCase):
    def setUp(self):
        super().setUp()
        self.backend.display_count = 2
        self.session.reload()

    def select_ban(self, display):
        self.choose(BAN)
        self.assertEqual(self.controls.mode, "display")
        self.keys("ctrl-u", *str(display), "enter")

    def test_confirmation_captures_displayed_pin_even_if_display_changes(self):
        with mock.patch.object(self.backend, "displayed_pin", wraps=self.backend.displayed_pin) as lookup:
            self.select_ban(2)
            self.assertIn("on display 2", self.controls.prompt)
            self.backend.display_pins[2] = "replacement-pin"
            self.keys("y")
        lookup.assert_called_once_with(2)
        self.assertEqual(self.backend.jobs, [("ban", ("2", "--expected-pin", "screen-2-pin"))])

    def test_single_display_skips_the_display_question(self):
        self.backend.display_count = 1
        self.session.reload()
        self.choose(BAN)
        self.assertEqual(self.controls.mode, "confirm")
        self.assertNotIn("display", self.controls.prompt)
        self.keys("y")
        self.assertEqual(self.backend.jobs, [("ban", ("1", "--expected-pin", "screen-1-pin"))])

    def test_unknown_display_pin_never_opens_confirmation_or_starts_job(self):
        for value in (None, ""):
            with self.subTest(value=value):
                self.backend.display_pins[3] = value
                self.select_ban(3)
                self.assertEqual(self.controls.mode, "browse")
                self.assertIn("display 3", self.session.message)
                self.assertEqual(self.backend.jobs, [])

    def test_unreadable_display_pin_never_opens_confirmation_or_starts_job(self):
        for error in (OSError("state unavailable"), ValueError("invalid display")):
            with self.subTest(error=error), mock.patch.object(self.backend, "displayed_pin", side_effect=error):
                self.select_ban(2)
                self.assertEqual(self.controls.mode, "browse")
                self.assertEqual(self.backend.jobs, [])
                self.assertIn(str(error), self.session.message)

    def test_display_must_be_a_number(self):
        self.choose(BAN)
        self.keys("ctrl-u", "x", "enter")
        self.assertEqual(self.controls.mode, "display")
        self.assertIn("display number", self.session.message)

    def test_cancelling_targeted_ban_leaves_backend_unchanged(self):
        for key in ("enter", "esc", "q", "n"):
            with self.subTest(key=key):
                self.select_ban(2)
                self.keys(key)
                self.assertEqual(self.backend.jobs, [])
                self.assertEqual(self.session.message, "Cancelled")

    def test_other_screen_actions_run_without_confirmation(self):
        self.choose("Open it on Pinterest")
        self.keys("enter")
        self.assertEqual(self.backend.jobs, [("open", ("1",))])


class BanBackendIntegrationTests(Sandbox):
    def test_real_adapter_target_is_frozen_before_confirmation(self):
        from wallpaper_tui_backend import Backend
        from wallpaper_tui_backend import Draft as BackendDraft

        self.add_pin("shown-pin", size=(8, 4))
        backend = Backend(pw, self.config)
        with pw.locked_state() as state:
            state["current"] = ["shown-pin"]
        session = Session(backend, BackendDraft)
        self.addCleanup(session.worker.close)
        session.controls.target_screen(next(item for item in __import__("wallpaper_tui_commands").MENU
                                            if getattr(item, "label", "") == BAN), "1")
        self.assertEqual(session.controls.mode, "confirm")
        with pw.locked_state() as state:
            state["current"] = ["replacement-pin"]
        with mock.patch.object(backend, "start_action") as start:
            session.handle("y")
        start.assert_called_once_with("ban", ("1", "--expected-pin", "shown-pin"))


if __name__ == "__main__":
    unittest.main()
