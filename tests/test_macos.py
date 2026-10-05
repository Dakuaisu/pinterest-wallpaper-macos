import copy
import os
import plistlib
import subprocess
import unittest
from unittest import mock

from helpers import REAL, SCREEN, Sandbox, pw


def completed(stdout="", returncode=0, stderr=""):

    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


class OsascriptTests(Sandbox):

    def test_returns_stripped_output(self):

        with mock.patch.object(pw.subprocess, "run", return_value=completed("ok\n")) as run:
            self.assertEqual(pw.osascript(["-e", "x"]), "ok")

        self.assertEqual(run.call_args[0][0], ["osascript", "-e", "x"])

    def test_failure_raises_with_stderr_or_exit_status(self):

        stderr, crashed = completed(returncode=1, stderr="nope"), completed(returncode=139)

        with mock.patch.object(pw.subprocess, "run", return_value=stderr), self.assertRaisesRegex(RuntimeError, "nope"):
            pw.osascript([])

        with mock.patch.object(pw.subprocess, "run", return_value=crashed), self.assertRaisesRegex(RuntimeError, "status 139"):
            pw.osascript([])


class ScreenTests(Sandbox):

    def test_detect_screens_parses_jxa_output(self):

        output = (
            '[{"id": 1, "name": "Built-in", "width": 3456, "height": 2234},'
            ' {"id": 5, "name": null, "width": 1920.0, "height": 1080}]'
        )

        with mock.patch.object(pw, "osascript", return_value=output):
            self.assertEqual(REAL["detect_screens"](), [
                {"id": 1, "name": "Built-in", "width": 3456, "height": 2234},
                {"id": 5, "name": "Display", "width": 1920, "height": 1080},
            ])

    def test_detect_screens_failure_is_logged_not_raised(self):

        for effect in ({"side_effect": RuntimeError("no window server")}, {"return_value": "not json"}, {"return_value": "[]"}):
            with self.subTest(effect=effect), mock.patch.object(pw, "osascript", **effect):
                self.assertIsNone(REAL["detect_screens"]())

        self.assertEqual(pw.log_error.call_count, 2)

    def test_current_screens_falls_back_to_last_known_then_default(self):

        state = copy.deepcopy(pw.DEFAULT_STATE)
        self.screens = []

        self.assertEqual(
            pw.current_screens(self.config, state),
            [{"id": None, "name": "Display", "width": 2560, "height": 1600}],
        )

        state["screens"] = [{"id": 1, "name": "A", "width": 100, "height": 50}]
        self.assertEqual(pw.current_screens(self.config, state)[0]["width"], 100)

    def test_current_screens_remembers_detection_and_applies_forced_size(self):

        state = copy.deepcopy(pw.DEFAULT_STATE)
        self.config["resolution"] = "800x600"

        screens = pw.current_screens(self.config, state)

        self.assertEqual(state["screens"], [SCREEN])
        self.assertEqual((screens[0]["width"], screens[0]["height"]), (800, 600))

    def test_detect_false_never_calls_detection(self):

        with mock.patch.object(pw, "detect_screens") as detect:
            pw.current_screens(self.config, copy.deepcopy(pw.DEFAULT_STATE), detect=False)

        detect.assert_not_called()

    def test_set_wallpapers_passes_screen_ids_and_paths(self):

        first, second = (os.path.join(self.root, name) for name in ("a.jpg", "b.jpg"))

        for path in (first, second):
            open(path, "wb").close()

        with mock.patch.object(pw, "osascript") as osascript:
            REAL["set_wallpapers"]([({"id": None}, first), ({"id": 7}, second)])

        self.assertEqual(
            osascript.call_args[0][0],
            ["-l", "JavaScript", "-e", pw.JXA_SET_WALLPAPERS, "*", first, "7", second],
        )


class SessionTests(Sandbox):

    def test_screen_locked_reads_the_console_session(self):

        locked = {"IOConsoleUsers": [{"kCGSSessionOnConsoleKey": True, "CGSSessionScreenIsLocked": True}]}
        unlocked = {"IOConsoleUsers": [{"kCGSSessionOnConsoleKey": True}]}
        other_user = {"IOConsoleUsers": [{"kCGSSessionOnConsoleKey": False, "CGSSessionScreenIsLocked": True}]}

        for root, expected in ((locked, True), ([locked], True), (unlocked, False), (other_user, False), ({}, False)):
            with self.subTest(root=root), \
                    mock.patch.object(pw.subprocess, "run", return_value=completed(plistlib.dumps(root))):
                self.assertEqual(REAL["screen_locked"](), expected)

    def test_screen_locked_is_false_when_ioreg_fails(self):

        with mock.patch.object(pw.subprocess, "run", side_effect=OSError("no ioreg")):
            self.assertFalse(REAL["screen_locked"]())

    def test_dark_mode(self):

        with mock.patch.object(pw.subprocess, "run", return_value=completed("Dark\n")):
            self.assertTrue(pw.dark_mode())

        with mock.patch.object(pw.subprocess, "run", return_value=completed("", 1, "does not exist")):
            self.assertFalse(pw.dark_mode())

    def test_notify_passes_text_as_arguments_and_never_raises(self):

        with mock.patch.object(pw, "osascript") as osascript:
            REAL["notify"](dict(self.config, notifications=False), "Title", "Body")
            osascript.assert_not_called()
            REAL["notify"](self.config, 'Ti"tle', "Bo\\dy")

        self.assertEqual(osascript.call_args[0][0][-2:], ["Bo\\dy", 'Ti"tle'])

        with mock.patch.object(pw, "osascript", side_effect=RuntimeError("denied")):
            REAL["notify"](self.config, "T", "B")

    def test_launchctl_interval(self):

        cases = [
            (completed(returncode=113), None),
            (completed("\trun interval = 900 seconds\n"), 900),
            (completed("state = running"), 0),
        ]

        for result, expected in cases:
            with self.subTest(expected=expected), mock.patch.object(pw.subprocess, "run", return_value=result):
                self.assertEqual(pw.launchctl_interval("x"), expected)


if __name__ == "__main__":
    unittest.main()
