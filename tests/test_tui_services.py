import json
import os
import threading
from unittest import mock

import pinterest_wallpaper as pw
import wallpaper_tui_actions as actions
from helpers import BOARD, Sandbox
from wallpaper_tui_backend import Backend, Pin, TuiError


class ServiceTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.add_pin("123")
        self.config["boards"] = [BOARD, "https://www.pinterest.com/a/b/"]
        pw.save_json(pw.CONFIG_FILE, self.config)
        self.backend = Backend(pw, self.config)

    def test_board_snapshot_counts_only_available_memberships(self):
        with pw.locked_state() as state:
            state["pins"]["123"]["boards"] = list(self.config["boards"])
            state["favorites"] = ["123"]
        pins = self.backend.pins()
        self.assertEqual(pins[0].boards, tuple(self.config["boards"]))
        self.assertTrue(pins[0].favorite)
        self.assertEqual([row[2] for row in self.backend.boards()], [1, 1])
        self.assertEqual(Pin("a", "", 0, False).boards, ())

    def test_action_rejects_non_allowlisted_commands(self):
        with self.assertRaises(TuiError):
            self.backend.start_action("tui")

    def test_action_runs_in_background_and_does_not_kill_on_close(self):
        entered, release = threading.Event(), threading.Event()
        def run(*args, **kwargs):
            entered.set()
            release.wait(5)
            return (True, "ok")
        with mock.patch("wallpaper_tui_actions.run_action", side_effect=run):
            self.backend.start_action("status")
            self.assertTrue(entered.wait(2))
            self.assertTrue(self.backend.action_busy)
            self.assertIsNone(self.backend.take_action())
            self.backend.close_actions()
            self.assertTrue(self.backend.action_busy)
            release.set()
            self.backend._actions.thread.join(2)
            self.assertEqual(self.backend.take_action(), (True, "ok"))

    def test_config_compare_and_swap_preserves_unknown_keys(self):
        with open(pw.CONFIG_FILE) as handle:
            config = json.load(handle)
        config["custom"] = {"keep": True}
        pw.save_json(pw.CONFIG_FILE, config)
        old = json.dumps(config["boards"])
        new = json.dumps([BOARD])
        self.backend.start_action("config", ("boards", new, old))
        self.backend._actions.thread.join(2)
        self.assertTrue(self.backend.take_action()[0])
        with open(pw.CONFIG_FILE) as handle:
            saved = json.load(handle)
        self.assertEqual(saved["boards"], [BOARD])
        self.assertEqual(saved["custom"], {"keep": True})
        self.backend.start_action("config", ("boards", old, old))
        self.backend._actions.thread.join(2)
        self.assertFalse(self.backend.take_action()[0])

    def test_board_snapshot_does_not_create_files(self):
        before = set(os.listdir(self.root))
        self.backend.boards()
        self.assertEqual(before, set(os.listdir(self.root)))

    def test_child_uses_fixed_launcher_and_bounded_safe_output(self):
        def launch(argv, **kwargs):
            self.assertEqual(argv, [actions.LAUNCHER, "status"])
            self.assertIs(kwargs["stdin"], actions.subprocess.DEVNULL)
            kwargs["stdout"].write(b"\x1b[31m" + b"x" * 20000)
            return mock.Mock(wait=lambda: 0)
        with mock.patch.object(actions.subprocess, "Popen", side_effect=launch):
            ok, output = actions.run_action(pw, "status", ())
        self.assertTrue(ok)
        self.assertLessEqual(len(output), actions.OUTPUT_LIMIT)
        self.assertNotIn("\x1b", output)
        self.assertIn("[truncated]", output)

    def test_child_failure_is_reported_without_interpreting_arguments(self):
        def launch(argv, **kwargs):
            self.assertEqual(argv, [actions.LAUNCHER, "unban", "123;false"])
            self.assertNotIn("shell", kwargs)
            kwargs["stdout"].write(b"failure\n")
            return mock.Mock(wait=lambda: 2)
        with mock.patch.object(actions.subprocess, "Popen", side_effect=launch):
            self.assertEqual(actions.run_action(pw, "unban", ("123;false",)), (False, "failure\n"))

    def test_pending_result_blocks_another_action_until_consumed(self):
        with mock.patch("wallpaper_tui_actions.run_action", return_value=(True, "done")):
            self.backend.start_action("status")
            self.backend._actions.thread.join(2)
            with self.assertRaises(TuiError):
                self.backend.start_action("next")
            self.assertEqual(self.backend.take_action(), (True, "done"))
            self.assertFalse(self.backend.action_busy)

    def test_config_rejects_refresh_lock_and_invalid_values(self):
        with pw.file_lock(pw.REFRESH_LOCK):
            self.backend.start_action("config", ("boards", json.dumps([BOARD]), json.dumps(self.config["boards"])))
            self.backend._actions.thread.join(2)
            self.assertFalse(self.backend.take_action()[0])
        self.backend.start_action("config", ("boards", "[]", json.dumps(self.config["boards"])))
        self.backend._actions.thread.join(2)
        self.assertFalse(self.backend.take_action()[0])

    def test_settings_and_local_status_read_without_desktop(self):
        self.assertEqual(dict(self.backend.settings())["boards"], actions.compact(self.config["boards"]))
        with mock.patch.object(pw, "detect_screens", side_effect=AssertionError("desktop access")):
            self.assertEqual(self.backend.overview(), (False, "Rotation on", 1))
            self.assertEqual(self.backend.bans(), [])
