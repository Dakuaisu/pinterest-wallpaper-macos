import copy
import json
import os
import plistlib
import subprocess
import time
import unittest
from unittest import mock

from helpers import BOARD, SCREEN, Sandbox, make_listing, pw


def completed(returncode=0, stdout="", stderr=""):

    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def local(hour, day=27):

    return time.struct_time((2026, 9, day, hour, 0, 0, 6, 270, -1))


class RotationEdgeTests(Sandbox):

    def test_commands_on_an_empty_collection_fail_cleanly(self):

        for command in (pw.cmd_next, pw.cmd_shuffle, pw.cmd_ban, pw.cmd_fav, pw.cmd_rotate, pw.cmd_prev):
            with self.subTest(command=command.__name__):
                self.assertEqual(command(self.config, []), 1)

    def test_all_sources_unreadable(self):

        self.add_pin("a")

        with open(os.path.join(pw.IMAGE_DIR, "a.jpg"), "wb") as handle:
            handle.write(b"junk")

        self.assertEqual(pw.cmd_next(self.config, []), 1)
        self.assertIn("No readable wallpapers left", pw.log_error.call_args[0][0])

    def test_failed_set_puts_the_pick_back(self):

        for key in "ab":
            self.add_pin(key)

        with pw.locked_state() as state:
            state["deck"] = ["a", "b"]

        with mock.patch.object(pw, "set_wallpapers", side_effect=RuntimeError("denied")):
            self.assertEqual(pw.cmd_next(self.config, []), 1)

        self.assertEqual(self.state()["deck"], ["a", "b"])
        self.assertEqual(self.state()["history"], [])

    def test_prev_when_nothing_earlier_is_available(self):

        self.add_pin("c")

        with pw.locked_state() as state:
            state["history"] = [["gone1"], ["gone2"], ["c"]]

        self.assertEqual(pw.cmd_prev(self.config, []), 1)
        self.assertEqual(self.state()["history"], [["gone1"], ["gone2"], ["c"]])

    def test_expired_pause_is_cleared_by_the_tick(self):

        state = copy.deepcopy(pw.DEFAULT_STATE)
        state["paused_until"] = int(time.time()) - 5

        self.assertIsNone(pw.tick_skip_reason(self.config, state))
        self.assertEqual(state["paused_until"], 0)

    def test_tick_reports_failure_to_launchd(self):

        with pw.locked_state() as state:
            state["last_refresh_ok"] = state["last_refresh_attempt"] = time.time()

        self.assertEqual(pw.cmd_tick(self.config, []), 1)

    def test_mood_preference(self):

        self.assertIsNone(pw.mood_preference(self.config))

        self.config["mood"] = "appearance"

        for dark in (True, False):
            with mock.patch.object(pw, "dark_mode", return_value=dark):
                self.assertIs(pw.mood_preference(self.config), dark)

        self.config["mood"] = "night"

        for hours, hour, expected in (([19, 7], 22, True), ([19, 7], 3, True), ([19, 7], 12, False),
                                      ([1, 5], 3, True), ([1, 5], 6, False)):
            with self.subTest(hours=hours, hour=hour):
                self.config["night_hours"] = hours
                with mock.patch.object(pw.time, "localtime", return_value=local(hour)):
                    self.assertIs(pw.mood_preference(self.config), expected)

    def test_more_displays_than_pins_repeats_pins(self):

        state = copy.deepcopy(pw.DEFAULT_STATE)
        state["pins"] = {"a": {"luma": 1}}

        self.assertEqual(pw.draw(state, ["a"], 3, self.config, None), ["a", "a", "a"])

    def test_parse_helpers(self):

        self.assertEqual(pw.parse_display(["2"]), 2)
        self.assertEqual(pw.parse_duration("1.5h"), 5400)
        self.assertEqual(pw.parse_duration("30m"), 1800)
        self.assertEqual(pw.parse_duration("2d"), 172800)

        for bad in (["0"], ["x"]):
            with self.assertRaises(ValueError):
                pw.parse_display(bad)

    def test_rotate_argument_errors(self):

        self.add_pin("a")
        pw.cmd_next(self.config, [])

        with self.assertRaises(ValueError):
            pw.cmd_rotate(self.config, ["cw", "ccw"])

        with self.assertRaises(ValueError):
            pw.cmd_rotate(self.config, ["sideways"])


class LibraryCommandTests(Sandbox):

    def test_bans_listing(self):

        code, out = self.printed(pw.cmd_bans)
        self.assertEqual((code, out), (0, "No banned pins."))

        with pw.locked_state() as state:
            state["banned"] = ["123", "d41d8cd9"]

        code, out = self.printed(pw.cmd_bans)

        self.assertIn("Banned pins (2):", out)
        self.assertIn("123  https://www.pinterest.com/pin/123/", out)
        self.assertIn("d41d8cd9  (from an older version", out)

    def test_unban_unknown_pin(self):

        self.assertEqual(pw.cmd_unban(self.config, ["999"]), 1)

    def test_open_opens_the_pin_page(self):

        self.add_pin("123")
        pw.cmd_next(self.config, [])

        with mock.patch.object(pw.subprocess, "run") as run, mock.patch("builtins.print"):
            self.assertEqual(pw.cmd_open(self.config, []), 0)

        run.assert_called_once_with(["open", "https://www.pinterest.com/pin/123/"], check=False)

    def test_open_without_a_pinterest_pin(self):

        self.assertEqual(pw.cmd_open(self.config, []), 1)

        with pw.locked_state() as state:
            state["history"] = [["d41d8cd9"]]

        self.assertEqual(pw.cmd_open(self.config, []), 1)

    def test_pause_until_resumed(self):

        pw.cmd_pause(self.config, [])

        self.assertEqual(self.state()["paused_until"], -1)
        self.assertIn("until you run ./pw resume", pw.log.call_args[0][0])

        with self.assertRaises(ValueError):
            pw.cmd_pause(self.config, ["1h", "2h"])

    def test_pause_label(self):

        state = copy.deepcopy(pw.DEFAULT_STATE)
        state["paused_until"] = -1
        self.assertEqual(pw.pause_label(state), "paused until resumed")

        state["paused_until"] = 1

        with mock.patch.object(pw.time, "localtime", side_effect=[local(18), local(9)]):
            self.assertEqual(pw.pause_label(state), "paused until 18:00")

        with mock.patch.object(pw.time, "localtime", side_effect=[local(18, day=29), local(9)]):
            self.assertEqual(pw.pause_label(state), "paused until Sun 29 Sep 18:00")


class StatusTests(Sandbox):

    def agent(self, intervals):

        return mock.patch.object(pw, "launchctl_interval", side_effect=lambda label: intervals.get(label))

    def test_agent_summary_variants(self):

        cases = [
            ({pw.LEGACY_LABEL: 900}, "old com.pinterest.wallpaper agent"),
            ({}, "not installed"),
            ({pw.LABEL: 0}, "couldn't read its interval"),
            ({pw.LABEL: 600}, "runs every 10 min but config says 15"),
            ({pw.LABEL: 900}, "installed, every 15 min"),
        ]

        for intervals, expected in cases:
            with self.subTest(expected=expected), self.agent(intervals):
                self.assertIn(expected, pw.agent_summary(self.config))

    def test_status_shows_everything_about_the_pin_on_screen(self):

        self.add_pin("123", size=(40, 25))
        pw.cmd_next(self.config, [])

        with pw.locked_state() as state:
            state["favorites"] = ["123"]
            state["rotations"] = {"123": 180}
            state["paused_until"] = -1
            state["board_fails"] = {BOARD: {"count": 2, "error": "HTTP 500"}}
            state["board_names"] = ["Cats"]

        with self.agent({pw.LABEL: 900}):
            code, out = self.printed(pw.cmd_status)

        self.assertEqual(code, 0)
        self.assertIn("123  (rotated 180, favorite)  https://www.pinterest.com/pin/123/", out)
        self.assertIn("40x25 source, scaled 4.0x to fill 160x100", out)
        self.assertIn("(paused until resumed)", out)
        self.assertIn("crop=smart", out)
        self.assertIn("Next refresh  : due now", out)
        self.assertIn(f"Board errors  : {BOARD} failed 2x: HTTP 500", out)

    def test_status_before_the_first_sync(self):

        with self.agent({}):
            code, out = self.printed(pw.cmd_status)

        self.assertIn("(not synced yet)", out)
        self.assertIn("Showing       : -", out)
        self.assertIn("Up next       : (reshuffle)", out)

    def test_status_counts_down_to_the_next_refresh(self):

        with pw.locked_state() as state:
            state["last_refresh_ok"] = time.time() - 3600

        with self.agent({}):
            _, out = self.printed(pw.cmd_status)

        self.assertIn("Next refresh  : in 22h 59m", out)

    def test_up_next_follows_the_mood(self):

        self.add_pin("dark", luma=10)
        self.add_pin("light", luma=250)
        self.add_pin("mid", luma=128)

        with pw.locked_state() as state:
            state["deck"] = ["light", "dark", "mid"]

        with self.agent({}), mock.patch.object(pw, "mood_preference", return_value=True):
            _, out = self.printed(pw.cmd_status)

        self.assertIn("Up next       : dark", out)


class MenubarTests(Sandbox):

    def test_not_synced_and_not_paused(self):

        _, out = self.printed(pw.cmd_menubar)

        self.assertIn("Not synced yet", out)
        self.assertIn('Pause for 1 hour | shell="', out)
        self.assertNotIn("Resume", out)

    def test_paused_menu_offers_resume(self):

        with pw.locked_state() as state:
            state["paused_until"] = -1

        _, out = self.printed(pw.cmd_menubar)

        self.assertTrue(out.startswith("PW (paused)"))
        self.assertIn("Paused until resumed", out)
        self.assertIn('Resume | shell="', out)

    def test_rotated_favorite_pin_offers_reset_and_unfavorite(self):

        self.add_pin("1", size=(40, 25))
        pw.cmd_next(self.config, [])

        with pw.locked_state() as state:
            state["favorites"] = ["1"]
            state["rotations"] = {"1": 90}

        _, out = self.printed(pw.cmd_menubar)

        self.assertIn('Unfavorite | shell="', out)
        self.assertIn('param1="rotate" param2="reset"', out)
        self.assertIn("25x40 source, scaled 6.4x to fill 160x100", out)
        self.assertIn('Open on Pinterest | shell="', out)
        self.assertIn("refresh=false", out)

    def test_each_display_gets_its_own_submenu(self):

        for key in "ab":
            self.add_pin(key)

        self.screens = [dict(SCREEN), {"id": 2, "name": "External", "width": 200, "height": 100}]
        self.config["different_per_display"] = True
        pw.cmd_next(self.config, [])

        _, out = self.printed(pw.cmd_menubar)
        second = self.state()["history"][-1][1]

        self.assertIn(f"Display 2: pin {second}", out)
        self.assertIn('--Ban this pin | shell="', out)
        self.assertIn('param1="ban" param2="2"', out)
        self.assertIn("--400x300 source", out)


class LaunchdTests(Sandbox):

    def setUp(self):

        super().setUp()
        self.loaded = {}
        self.calls = []
        self.bootstrap_codes = []

        patchers = [
            mock.patch.object(pw, "launchctl_interval", side_effect=lambda label: self.loaded.get(label)),
            mock.patch.object(pw.subprocess, "run", side_effect=self.fake_run),
            mock.patch.object(pw.time, "sleep"),
        ]

        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake_run(self, cmd, **kwargs):

        self.calls.append(cmd)

        if cmd[1] == "bootout":
            self.loaded.pop(cmd[2].rsplit("/", 1)[1], None)
            return completed()

        code = self.bootstrap_codes.pop(0) if self.bootstrap_codes else 0

        if code == 0:
            self.loaded[pw.LABEL] = 900

        return completed(code, stderr="Bootstrap failed: 5: Input/output error" if code else "")

    def test_install_writes_the_agent_and_replaces_the_old_one(self):

        os.makedirs(pw.LAUNCH_AGENTS)
        legacy = os.path.join(pw.LAUNCH_AGENTS, f"{pw.LEGACY_LABEL}.plist")
        open(legacy, "w").close()
        self.loaded[pw.LEGACY_LABEL] = 900
        self.bootstrap_codes = [5, 0]
        self.config["interval_minutes"] = 10

        self.assertEqual(pw.cmd_install(self.config, []), 0)

        with open(os.path.join(pw.LAUNCH_AGENTS, f"{pw.LABEL}.plist"), "rb") as handle:
            agent = plistlib.load(handle)

        self.assertEqual(agent["ProgramArguments"], [pw.PW, "tick"])
        self.assertEqual(agent["StartInterval"], 600)
        self.assertTrue(agent["RunAtLoad"])
        self.assertEqual(agent["StandardErrorPath"], os.path.join(self.root, "agent.err"))
        self.assertFalse(os.path.exists(legacy))
        self.assertNotIn(pw.LEGACY_LABEL, self.loaded)
        self.assertEqual(sum(1 for cmd in self.calls if cmd[1] == "bootstrap"), 2)

    def test_install_gives_up_after_repeated_bootstrap_failures(self):

        self.bootstrap_codes = [5] * 5

        self.assertEqual(pw.cmd_install(self.config, []), 1)
        self.assertIn("Input/output error", pw.log_error.call_args[0][0])

    def test_reinstall_boots_out_the_running_agent_first(self):

        self.loaded[pw.LABEL] = 900

        pw.cmd_install(self.config, [])

        self.assertEqual(self.calls[0][:2], ["launchctl", "bootout"])

    def test_uninstall(self):

        os.makedirs(pw.LAUNCH_AGENTS)

        for label in (pw.LABEL, pw.LEGACY_LABEL):
            open(os.path.join(pw.LAUNCH_AGENTS, f"{label}.plist"), "w").close()

        self.loaded[pw.LABEL] = 900

        pw.cmd_uninstall(self.config, [])

        self.assertEqual(os.listdir(pw.LAUNCH_AGENTS), [])
        self.assertEqual(self.loaded, {})
        self.assertIn("Removed", pw.log.call_args[0][0])

        pw.cmd_uninstall(self.config, [])
        self.assertIn("No launchd agent", pw.log.call_args[0][0])

    def test_bootout_waits_until_the_job_is_gone(self):

        polls = iter([900, 900, 900, None])

        with mock.patch.object(pw, "launchctl_interval", side_effect=lambda label: next(polls)):
            self.assertTrue(pw.bootout(pw.LABEL))

        self.assertEqual(pw.time.sleep.call_count, 2)


class DoctorTests(Sandbox):

    def setUp(self):

        super().setUp()
        patchers = [
            mock.patch.object(pw, "osascript", return_value="/x/current.jpg"),
            mock.patch.object(pw, "launchctl_interval", side_effect=lambda label: 900 if label == pw.LABEL else None),
            mock.patch.object(pw, "fetch_board", return_value=make_listing(["1", "2"])),
        ]

        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def doctor(self):

        return self.printed(pw.cmd_doctor)

    def test_healthy_setup(self):

        self.add_pin("big", size=(400, 300))
        code, out = self.doctor()

        self.assertEqual(code, 0)

        for line in (
            "ok    Displays: Built-in 160x100",
            "ok    Wallpaper API reachable (current: current.jpg)",
            "ok    Agent installed, every 15 min",
            "ok    Board Cats: 2 pins listed",
            "ok    No pin needs more than 2x enlargement",
        ):
            self.assertIn(line, out)

    def test_failures_are_reported(self):

        self.config["boards"] = list(pw.DEFAULT_CONFIG["boards"]) + [BOARD]
        self.screens = []

        with mock.patch.object(pw, "osascript", side_effect=RuntimeError("denied")), \
                mock.patch.object(pw, "fetch_board", side_effect=[pw.Offline("dns"), RuntimeError("HTTP 404")]):
            code, out = self.doctor()

        self.assertEqual(code, 1)

        for line in ("FAIL  config.json \"boards\" still has the placeholder URL", "FAIL  Couldn't list displays",
                     "FAIL  Wallpaper API: denied", "offline (dns)", "HTTP 404"):
            self.assertIn(line, out)

    def test_config_problems_and_partial_boards_are_warnings(self):

        with open(pw.CONFIG_FILE, "w") as handle:
            json.dump({"boards": [BOARD], "refresh_hour": 1}, handle)

        with mock.patch.object(pw, "fetch_board", return_value=make_listing(["1"], complete=False)):
            code, out = self.doctor()

        self.assertEqual(code, 0)
        self.assertIn("warn  config.json: unknown key 'refresh_hour' ignored.", out)
        self.assertIn("warn  Board Cats: 1 pins listed (partial", out)

    def test_upscaler_checks(self):

        missing = os.path.join(self.root, "nope")
        self.config["upscaler"] = missing
        self.assertIn(f"FAIL  Upscaler {missing} isn't an executable file", self.doctor()[1])

        self.config["upscaler"] = self.make_upscaler()
        self.assertIn("warn  Upscaler found, but no models/ folder", self.doctor()[1])

        os.makedirs(os.path.join(self.root, "models"))
        self.assertIn(f"ok    Upscaler {self.config['upscaler']} (realesrgan-x4plus)", self.doctor()[1])

    def test_enlargement_report(self):

        self.add_pin("small", size=(40, 25))
        self.add_pin("big", size=(400, 300))

        self.assertIn("warn  1 of 2 pin(s) need more than 2x enlargement to fill 160x100", self.doctor()[1])

        self.config["max_upscale"] = 2.5
        self.assertIn("handled by max_upscale", self.doctor()[1])


class MainTests(Sandbox):

    def test_help_and_bad_commands(self):

        with mock.patch("builtins.print") as printed:
            self.assertEqual(pw.main(["--help"]), 0)

        self.assertIn("usage: pw", printed.call_args[0][0])

        with mock.patch("builtins.print"):
            self.assertEqual(pw.main([]), 2)
            self.assertEqual(pw.main(["bogus"]), 2)

    def test_argument_errors_exit_2_with_a_message(self):

        with mock.patch("builtins.print") as printed:
            self.assertEqual(pw.main(["pause", "soon"]), 2)

        self.assertIn("pw pause: can't read duration", printed.call_args[0][0])

    def test_config_problems_are_logged_except_for_quiet_commands(self):

        with open(pw.CONFIG_FILE, "w") as handle:
            json.dump({"boards": [BOARD], "bogus": 1}, handle)

        with mock.patch("builtins.print"):
            pw.main(["menubar"])
            pw.log_error.assert_not_called()
            pw.main(["bans"])

        self.assertIn("unknown key 'bogus'", pw.log_error.call_args[0][0])

    def test_cache_folders_are_recreated(self):

        os.rmdir(pw.WALLPAPERS_DIR)

        with mock.patch("builtins.print"):
            pw.main(["bans"])

        self.assertTrue(os.path.isdir(pw.WALLPAPERS_DIR))


if __name__ == "__main__":
    unittest.main()
