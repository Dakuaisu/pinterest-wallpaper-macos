import os
from unittest import mock

import pinterest_wallpaper as pw
from helpers import Sandbox
from wallpaper_tui_backend import Backend, TuiError


class BanGuardTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.add_pin("123")
        self.add_pin("456")
        with pw.locked_state() as state:
            state["current"] = ["456"]
            state["deck"] = ["123", "456"]
            state["history"] = [["123"], ["456"]]
        pw.save_json(pw.CONFIG_FILE, self.config)
        self.backend = Backend(pw, self.config)

    def test_displayed_pin_reads_latest_disk_without_repair(self):
        self.backend.pins()
        with pw.locked_state() as state:
            state["current"] = ["123"]
        self.assertEqual(self.backend.displayed_pin(1), "123")

    def test_displayed_pin_rejects_corrupt_state_without_repair(self):
        with open(pw.STATE_FILE, "w") as handle:
            handle.write("{")
        before = set(os.listdir(self.root))
        with self.assertRaises(TuiError):
            self.backend.displayed_pin(1)
        self.assertEqual(before, set(os.listdir(self.root)))
        with open(pw.STATE_FILE) as handle:
            self.assertEqual(handle.read(), "{")

    def test_mismatch_leaves_state_file_untouched_and_never_advances(self):
        with open(pw.STATE_FILE, "rb") as handle:
            before = handle.read()
        with mock.patch.object(pw, "advance") as advance, mock.patch.object(pw, "save_state") as save, mock.patch("sys.stderr"):
            code = pw.main(["ban", "1", "--expected-pin", "123"])
        self.assertNotEqual(code, 0)
        advance.assert_not_called()
        save.assert_not_called()
        with open(pw.STATE_FILE, "rb") as handle:
            self.assertEqual(handle.read(), before)

    def test_matching_expected_pin_bans_and_advances(self):
        with mock.patch.object(pw, "advance", return_value=True) as advance:
            code = pw.main(["ban", "1", "--expected-pin", "456"])
        self.assertEqual(code, 0)
        self.assertIn("456", self.state()["banned"])
        self.assertNotIn("456", self.state()["deck"])
        advance.assert_called_once()

    def test_expected_pin_is_checked_while_state_lock_is_held(self):
        lookup = pw.pin_on_display
        def checked_lookup(state, display):
            with pw.file_lock(pw.STATE_LOCK, wait=False) as acquired:
                self.assertFalse(acquired)
            return lookup(state, display)
        with mock.patch.object(pw, "pin_on_display", side_effect=checked_lookup), mock.patch("sys.stderr"):
            self.assertNotEqual(pw.main(["ban", "1", "--expected-pin", "123"]), 0)

    def test_invalid_expected_pin_is_rejected_without_mutation(self):
        with mock.patch.object(pw, "advance") as advance, mock.patch.object(pw, "save_state") as save, mock.patch("sys.stderr"):
            code = pw.main(["ban", "1", "--expected-pin", "../bad"])
        self.assertNotEqual(code, 0)
        advance.assert_not_called()
        save.assert_not_called()

    def test_missing_display_rejects_confirmation_without_saving(self):
        with mock.patch.object(pw, "save_state") as save, mock.patch.object(pw, "advance") as advance, mock.patch("sys.stderr"):
            code = pw.main(["ban", "2", "--expected-pin", "123"])
        self.assertNotEqual(code, 0)
        save.assert_not_called()
        advance.assert_not_called()

    def test_public_ban_without_expected_pin_still_works(self):
        with mock.patch.object(pw, "advance", return_value=True):
            code = pw.main(["ban"])
        self.assertEqual(code, 0)
        self.assertIn("456", self.state()["banned"])

    def test_displayed_pin_rejects_invalid_index(self):
        with self.assertRaises(TuiError):
            self.backend.displayed_pin(0)
