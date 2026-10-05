import copy
import io
import json
import os
import stat
import threading
import time
import unittest
from unittest import mock

from helpers import BOARD, REAL, Sandbox, pw


class TextTests(Sandbox):

    def test_short_flattens_and_truncates(self):

        self.assertEqual(pw.short(" a\nb "), "a b")
        self.assertEqual(pw.short("x" * 300, limit=10), "x" * 10 + "...")

    def test_clean_text_drops_control_characters(self):

        self.assertEqual(pw.clean_text("Cats\x1b[31m\tand\n dogs\x07"), "Cats [31m and dogs")

    def test_menu_text_neutralises_swiftbar_syntax(self):

        self.assertEqual(pw.menu_text("a | shell=/bin/sh"), "a / shell=/bin/sh")
        self.assertEqual(pw.menu_text("--Run"), "Run")
        self.assertEqual(pw.menu_text("---"), "(untitled)")

    def test_fmt_time(self):

        now = time.time()

        self.assertEqual(pw.fmt_time(0), "never")
        self.assertIn("(2m ago)", pw.fmt_time(now - 125))
        self.assertIn("(2h ago)", pw.fmt_time(now - 7300))
        self.assertIn("(3d ago)", pw.fmt_time(now - 3 * 86400 - 5))

    def test_pin_url_only_for_numeric_ids(self):

        self.assertEqual(pw.pin_url("123"), "https://www.pinterest.com/pin/123/")
        self.assertIsNone(pw.pin_url("d41d8cd98f00b204"))

    def test_log_lines_are_timestamped_and_split_by_stream(self):

        with mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            REAL["log"]("hello")
            REAL["log_error"]("oops")

        self.assertRegex(out.getvalue(), r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d  hello\n$")
        self.assertRegex(err.getvalue(), r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d  oops\n$")


class FileTests(Sandbox):

    def test_atomic_write_keeps_the_old_file_when_writing_fails(self):

        path = os.path.join(self.root, "data.json")
        pw.save_json(path, {"a": 1})

        def explode(handle):
            handle.write(b"partial")
            raise RuntimeError("disk full")

        with self.assertRaises(RuntimeError):
            pw.atomic_write(path, explode)

        with open(path) as handle:
            self.assertEqual(json.load(handle), {"a": 1})

        self.assertEqual([n for n in os.listdir(self.root) if n.endswith(".tmp")], [])

    def test_atomic_write_leaves_a_world_readable_file(self):

        path = os.path.join(self.root, "x.bin")
        pw.atomic_write(path, lambda handle: handle.write(b"x"))

        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)


class ConfigValidationTests(Sandbox):

    def load(self, data):

        with open(pw.CONFIG_FILE, "w") as handle:
            json.dump(data, handle)

        return pw.load_config()

    def test_missing_config_is_created_with_defaults(self):

        config, problems = pw.load_config()

        self.assertEqual((config, problems), (pw.DEFAULT_CONFIG, []))

        with open(pw.CONFIG_FILE) as handle:
            self.assertEqual(json.load(handle), pw.DEFAULT_CONFIG)

    def test_unreadable_or_non_object_config_falls_back_to_defaults(self):

        with open(pw.CONFIG_FILE, "w") as handle:
            handle.write("{nope")

        config, problems = pw.load_config()
        self.assertEqual(config, pw.DEFAULT_CONFIG)
        self.assertIn("unreadable", problems[0])

        config, problems = self.load(["not", "an", "object"])
        self.assertEqual(config, pw.DEFAULT_CONFIG)
        self.assertIn("JSON object", problems[0])

    def test_every_rule_rejects_bad_values(self):

        bad = {
            "boards": [], "interval_minutes": 0, "refresh_hours": -1, "retry_hours": "1",
            "max_pins": 2.5, "skip_video": "yes", "skip_upscale_over": 0.5, "resolution": "big",
            "fit_mode": "stretch", "crop": "face", "max_upscale": 0.5, "backdrop": "red",
            "sharpen": 1, "menubar_gradient": None, "upscaler": 5, "upscaler_model": " ",
            "different_per_display": "no", "favorite_weight": 11, "mood": "sad",
            "night_hours": [19], "skip_when_locked": 0, "notifications": "on", "new_pins_first": [],
        }

        self.assertEqual(set(bad), set(pw.CONFIG_RULES))

        config, problems = self.load(bad)

        self.assertEqual(config, pw.DEFAULT_CONFIG)
        self.assertEqual(len(problems), len(bad))

    def test_valid_values_are_kept(self):

        good = {
            "boards": [BOARD], "interval_minutes": 5, "max_pins": 10, "skip_upscale_over": 3,
            "resolution": "1920 x 1080", "crop": "center", "max_upscale": 2.5, "favorite_weight": 10,
            "night_hours": [0, 23], "mood": "night", "skip_video": False,
        }

        config, problems = self.load(good)

        self.assertEqual(problems, [])
        self.assertEqual({k: config[k] for k in good}, good)
        self.assertEqual(pw.forced_size(config), (1920, 1080))
        self.assertIsNone(pw.forced_size(pw.DEFAULT_CONFIG))


class StateFileTests(Sandbox):

    def test_non_object_state_is_backed_up(self):

        with open(pw.STATE_FILE, "w") as handle:
            json.dump([1, 2], handle)

        self.assertEqual(pw.load_state(), pw.DEFAULT_STATE)
        self.assertTrue(any(n.startswith(".state.json.corrupt-") for n in os.listdir(self.root)))

    def test_locked_state_saves_even_when_the_command_fails(self):

        with self.assertRaises(RuntimeError), pw.locked_state() as state:
            state["banned"].append("7")
            raise RuntimeError("boom")

        self.assertEqual(pw.load_state()["banned"], ["7"])

    def test_waiting_lock_goes_ahead_once_released(self):

        path = os.path.join(self.root, "x.lock")
        events = []

        class Terminal(io.StringIO):
            def isatty(self):
                return True

        def second():
            with pw.file_lock(path) as got:
                events.append(("second", got))

        with mock.patch("sys.stdout", new_callable=Terminal) as out:

            with pw.file_lock(path) as first:
                thread = threading.Thread(target=second)
                thread.start()
                time.sleep(0.2)
                events.append(("first releases", first))

            thread.join(5)

        self.assertEqual(events, [("first releases", True), ("second", True)])
        self.assertIn("Waiting for another pw command", out.getvalue())

    def test_is_paused(self):

        state = copy.deepcopy(pw.DEFAULT_STATE)
        self.assertFalse(pw.is_paused(state))

        state["paused_until"] = -1
        self.assertTrue(pw.is_paused(state))

        state["paused_until"] = time.time() + 60
        self.assertTrue(pw.is_paused(state))

    def test_pin_on_display_with_nothing_showing(self):

        self.assertIsNone(pw.pin_on_display(copy.deepcopy(pw.DEFAULT_STATE), 1))

    def test_each_thread_gets_its_own_download_session(self):

        sessions = []

        def grab():
            sessions.append((pw.worker_session(), pw.worker_session()))

        threads = [threading.Thread(target=grab) for _ in range(2)]

        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        (a1, a2), (b1, _) = sessions
        self.assertIs(a1, a2)
        self.assertIsNot(a1, b1)


if __name__ == "__main__":
    unittest.main()
