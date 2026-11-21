from unittest import mock

import pinterest_wallpaper as pw
from helpers import BOARD, FakeResponse, Sandbox, board_page, feed_pin, make_listing


class PaginationRegressionTests(Sandbox):
    def test_empty_feed_is_authoritative(self):
        with mock.patch.object(pw.SESSION, "get", return_value=FakeResponse(board_page([], pin_count=0))):
            listing = pw.scrape_board(BOARD, 10)
        self.assertEqual(listing["pins"], [])
        self.assertTrue(listing["complete"])

    def test_repeated_bookmark_is_partial(self):
        first = feed_pin("1", "https://i.pinimg.com/originals/a.jpg")
        page = FakeResponse(body={"resource_response": {"data": [first], "bookmark": "same"}})
        with mock.patch.object(pw.SESSION, "get", side_effect=[FakeResponse(board_page([first], "same")), page, page, RuntimeError("unguarded cycle")]) as get:
            listing = pw.scrape_board(BOARD, 10)
        self.assertFalse(listing["complete"])
        self.assertEqual([pin["key"] for pin in listing["pins"]], ["1"])
        self.assertEqual(get.call_count, 2)

    def test_last_page_without_its_own_bookmark_completes_the_listing(self):
        first = feed_pin("1", "https://i.pinimg.com/originals/a.jpg")
        second = feed_pin("2", "https://i.pinimg.com/originals/b.jpg")
        last = FakeResponse(body={"resource": {"options": {"bookmarks": ["-end-"]}},
                                  "resource_response": {"data": [second], "status": "success"}})
        with mock.patch.object(pw.SESSION, "get", side_effect=[FakeResponse(board_page([first], "next")), last]):
            listing = pw.scrape_board(BOARD, 10)
        self.assertTrue(listing["complete"])
        self.assertEqual([pin["key"] for pin in listing["pins"]], ["1", "2"])

    def test_page_with_no_end_marker_anywhere_is_partial_and_logged(self):
        first = feed_pin("1", "https://i.pinimg.com/originals/a.jpg")
        second = feed_pin("2", "https://i.pinimg.com/originals/b.jpg")
        for extra in ({}, {"resource": {"options": {"bookmarks": [7]}}}):
            with self.subTest(extra=extra):
                page = FakeResponse(body=dict(extra, resource_response={"data": [second]}))
                with mock.patch.object(pw.SESSION, "get", side_effect=[FakeResponse(board_page([first], "next")), page]):
                    listing = pw.scrape_board(BOARD, 10)
                self.assertFalse(listing["complete"])
                self.assertIn("stopped paging after 1 pins", pw.log_error.call_args[0][0])

    def test_duplicate_pins_do_not_exhaust_cap(self):
        first = feed_pin("1", "https://i.pinimg.com/originals/a.jpg")
        second = feed_pin("2", "https://i.pinimg.com/originals/b.jpg")
        page = FakeResponse(body={"resource_response": {"data": [first, second], "bookmark": "-end-"}})
        with mock.patch.object(pw.SESSION, "get", side_effect=[FakeResponse(board_page([first, first], "next")), page]):
            listing = pw.scrape_board(BOARD, 2)
        self.assertEqual([pin["key"] for pin in listing["pins"]], ["1", "2"])

    def test_malformed_recognized_pin_is_partial(self):
        with mock.patch.object(pw.SESSION, "get", return_value=FakeResponse(board_page([{"type": "pin", "id": "bad"}]))):
            listing = pw.scrape_board(BOARD, 10)
        self.assertFalse(listing["complete"])


class RenderRegressionTests(Sandbox):
    def test_corrupt_upscale_falls_back_to_original(self):
        import os
        self.add_pin("1")
        self.config["upscaler"] = self.make_upscaler()
        upscaled = pw.upscale_cache_paths("1", self.config["upscaler"], self.config["upscaler_model"],
                                           os.path.join(pw.IMAGE_DIR, "1.jpg"))[0]
        with open(upscaled, "wb") as handle:
            handle.write(b"broken")
        state = self.state()
        path = pw.ensure_render("1", (100, 100), self.config, state, pw.source_files())
        self.assertTrue(os.path.exists(path))
        self.assertTrue(os.path.exists(os.path.join(pw.IMAGE_DIR, "1.jpg")))
        self.assertFalse(os.path.exists(upscaled))

    def test_color_change_changes_render_identity(self):
        self.add_pin("1")
        self.config["backdrop"] = "color"
        state = self.state()
        files = pw.source_files()
        state["pins"]["1"]["color"] = "#ff0000"
        first = pw.expected_render("1", (100, 100), self.config, state, files)[2]
        state["pins"]["1"]["color"] = "#0000ff"
        second = pw.expected_render("1", (100, 100), self.config, state, files)[2]
        self.assertNotEqual(first, second)


class SyncRegressionTests(Sandbox):
    def test_shared_pin_retained_when_other_board_offline(self):
        self.add_pin("1")
        other = "https://www.pinterest.com/someone/dogs/"
        self.config["boards"] = [BOARD, other]
        state = self.state()
        first = {"listings": [make_listing(["1"]), make_listing(["1"], url=other)], "failures": {}, "offline": [], "pins": dict(state["pins"])}
        pw.apply_sync(state, first, self.config)
        second = {"listings": [make_listing([])], "failures": {}, "offline": [other], "pins": {}}
        pw.apply_sync(state, second, self.config)
        self.assertIn("1", state["pins"])
        self.assertEqual(state["pins"]["1"]["boards"], [other])

    def test_listed_pin_survives_failed_redownload(self):
        self.add_pin("1")
        state = self.state()
        import os
        os.remove(os.path.join(pw.IMAGE_DIR, "1.jpg"))
        with mock.patch.object(pw, "fetch_board", return_value=make_listing(["1"])), mock.patch.object(pw, "try_download", return_value=(None, RuntimeError("offline"))):
            result = pw.sync_boards(self.config, state)
        pw.apply_sync(state, result, self.config)
        self.assertIn("1", state["pins"])
        self.assertIn("1", state["seen"])

    def test_partial_refresh_uses_retry_interval_despite_old_success(self):
        self.add_pin("1")
        state = self.state()
        result = {"listings": [make_listing(["1"], complete=False)], "failures": {}, "offline": [], "pins": dict(state["pins"])}
        with mock.patch.object(pw.time, "time", return_value=100000):
            state["last_refresh_attempt"] = 100000
            pw.apply_sync(state, result, self.config)
        with mock.patch.object(pw.time, "time", return_value=100000 + self.config["retry_hours"] * 3600 + 1):
            self.assertTrue(pw.refresh_due(self.config, state))


class RotationRegressionTests(Sandbox):
    def test_long_rotation_keeps_every_nonfavorite_in_rotation(self):
        for key in ("fav", "a", "b", "c"):
            self.add_pin(key)
        state = self.state()
        state["favorites"] = ["fav"]
        state["deck"] = []
        shown = []
        for _ in range(500):
            pick = pw.draw(state, ["fav", "a", "b", "c"], 1, dict(self.config, favorite_weight=3), None)[0]
            shown.append(pick)
            state["history"] = [[pick]]
        self.assertTrue(all(key in shown for key in ("a", "b", "c")))
        self.assertGreater(shown.count("fav"), shown.count("a"))

    def test_current_pin_slots_do_not_shift_when_one_is_unknown(self):
        state = self.state()
        state["screens"] = [dict(self.screens[0]), dict(self.screens[0], id=2)]
        state["current"] = [None, "2"]
        self.assertEqual(pw.pin_on_display(state, 1), None)
        self.assertEqual(pw.pin_on_display(state, 2), "2")

    def test_failed_ban_preserves_display_identity(self):
        self.add_pin("1")
        self.add_pin("2")
        with pw.locked_state() as state:
            state["history"] = [["1", "2"]]
            state["screens"] = [dict(self.screens[0]), dict(self.screens[0], id=2)]
        with mock.patch.object(pw, "advance", return_value=False):
            self.assertEqual(pw.cmd_ban(self.config, ["1"]), 1)
        state = self.state()
        self.assertIn("1", state["banned"])
        self.assertEqual(pw.pin_on_display(state, 1), "1")
        self.assertEqual(pw.pin_on_display(state, 2), "2")

    def test_failed_advance_restores_exact_deck(self):
        self.add_pin("1")
        self.add_pin("2")
        state = self.state()
        state["history"] = [["1"]]
        state["deck"] = ["1", "2", "1"]
        with mock.patch.object(pw, "set_wallpapers", side_effect=RuntimeError("setter failed")):
            self.assertFalse(pw.advance(self.config, state))
        self.assertEqual(state["deck"], ["1", "2", "1"])

    def test_failed_shuffle_restores_deck(self):
        self.add_pin("1")
        self.add_pin("2")
        with pw.locked_state() as state:
            state["deck"] = ["1", "2", "1"]
        with mock.patch.object(pw, "set_wallpapers", side_effect=RuntimeError("setter failed")):
            self.assertEqual(pw.cmd_shuffle(self.config, []), 1)
        self.assertEqual(self.state()["deck"], ["1", "2", "1"])


class InputRegressionTests(Sandbox):
    def test_future_state_is_not_migrated_or_overwritten(self):
        import json
        future = {"version": pw.STATE_VERSION + 1, "user_data": {"keep": True}}
        with open(pw.STATE_FILE, "w") as handle:
            json.dump(future, handle)
        with self.assertRaises(ValueError), pw.locked_state():
            pass
        with open(pw.STATE_FILE) as handle:
            self.assertEqual(json.load(handle), future)

    def test_failed_bootout_preserves_plist_and_prevents_bootstrap(self):
        import os
        os.makedirs(pw.LAUNCH_AGENTS)
        path = pw.plist_path()
        with open(path, "w") as handle:
            handle.write("existing")
        failure = mock.Mock(returncode=1)
        with mock.patch.object(pw, "launchctl_interval", return_value=900), mock.patch.object(pw.subprocess, "run", return_value=failure) as run:
            self.assertEqual(pw.cmd_uninstall(self.config, []), 1)
            self.assertTrue(os.path.exists(path))
            self.assertEqual(pw.cmd_install(self.config, []), 1)
        self.assertTrue(all(call.args[0][1] != "bootstrap" for call in run.call_args_list))

    def test_trailing_help_has_no_side_effects(self):
        with mock.patch.object(pw, "load_config", side_effect=AssertionError("config read")), mock.patch("builtins.print"):
            self.assertEqual(pw.main(["install", "--help"]), 0)

    def test_invalid_numeric_and_zero_resolution_values(self):
        self.assertFalse(pw.CONFIG_RULES["resolution"][0]("0x100"))
        self.assertFalse(pw.CONFIG_RULES["refresh_hours"][0](float("inf")))
        with self.assertRaises(ValueError):
            pw.parse_duration("9" * 400 + "h")
