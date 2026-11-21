import json
import os
import subprocess
import zlib
from unittest import mock

import requests

import pinterest_wallpaper as pw
from helpers import BOARD, FakeResponse, Sandbox, board_page, feed_pin, make_listing


class BookmarkReviewTests(Sandbox):
    def test_bad_initial_bookmark_is_partial_and_keeps_old_pin_through_apply(self):
        self.add_pin("old")
        self.add_pin("new")
        item = feed_pin("new", "https://i.pinimg.com/originals/new.jpg")
        page = board_page([item], bookmark=[], pin_count=2)
        with mock.patch.object(pw.SESSION, "get", return_value=FakeResponse(page)):
            result = pw.sync_boards(self.config, self.state())
        self.assertFalse(result["listings"][0]["complete"])
        with pw.locked_state() as state:
            pw.apply_sync(state, result, self.config)
        self.assertIn("old", self.state()["pins"])
        self.assertTrue(self.state()["refresh_pending"])

    def test_bookmark_terminals_and_invalid_types_are_distinguished(self):
        item = feed_pin("new", "https://i.pinimg.com/originals/new.jpg")
        for bookmark in (None, "", "-end-"):
            with self.subTest(bookmark=bookmark), mock.patch.object(
                pw.SESSION, "get", return_value=FakeResponse(board_page([item], bookmark=bookmark, pin_count=1))
            ):
                self.assertTrue(pw.scrape_board(BOARD, 10)["complete"])
        for bookmark in ([], {}, 0, False):
            with self.subTest(bookmark=bookmark), mock.patch.object(
                pw.SESSION, "get", return_value=FakeResponse(board_page([item], bookmark=bookmark, pin_count=2))
            ):
                self.assertFalse(pw.scrape_board(BOARD, 1)["complete"])

    def test_bad_pagination_bookmark_after_page_is_partial(self):
        first = feed_pin("1", "https://i.pinimg.com/originals/1.jpg")
        initial = FakeResponse(board_page([first], bookmark="next", pin_count=2))
        page = FakeResponse(body={"resource_response": {"data": [], "bookmark": {}}})
        with mock.patch.object(pw.SESSION, "get", side_effect=[initial, page]):
            listing = pw.scrape_board(BOARD, 10)
        self.assertFalse(listing["complete"])

    def test_missing_initial_bookmark_is_partial(self):
        item = feed_pin("new", "https://i.pinimg.com/originals/new.jpg")
        page = board_page([item], bookmark="-end-", pin_count=1)
        page = page.replace(', "nextBookmark": "-end-"', "")
        with mock.patch.object(pw.SESSION, "get", return_value=FakeResponse(page)):
            self.assertFalse(pw.scrape_board(BOARD, 10)["complete"])

    def test_malformed_initial_entries_make_partial_listing_and_preserve_old_pin(self):
        self.add_pin("old")
        self.add_pin("new")
        pin = feed_pin("new", "https://i.pinimg.com/originals/new.jpg")
        page = FakeResponse(board_page([pin, None], bookmark="-end-", pin_count=2))
        with mock.patch.object(pw.SESSION, "get", return_value=page):
            result = pw.sync_boards(self.config, self.state())
        self.assertEqual(len(result["listings"]), 1)
        self.assertFalse(result["listings"][0]["complete"])
        with pw.locked_state() as state:
            pw.apply_sync(state, result, self.config)
        self.assertIn("old", self.state()["pins"])
        self.assertTrue(self.state()["refresh_pending"])

    def test_malformed_paginated_entry_makes_listing_partial(self):
        first = feed_pin("1", "https://i.pinimg.com/originals/1.jpg")
        initial = FakeResponse(board_page([first], bookmark="next", pin_count=2))
        page = FakeResponse(body={"resource_response": {"data": [None], "bookmark": "-end-"}})
        with mock.patch.object(pw.SESSION, "get", side_effect=[initial, page]):
            listing = pw.scrape_board(BOARD, 10)
        self.assertFalse(listing["complete"])

    def test_boolean_zero_pin_count_cannot_authorize_empty_board_removal(self):
        self.add_pin("old")
        page = FakeResponse(board_page([], bookmark="-end-", pin_count=False))
        with mock.patch.object(pw.SESSION, "get", return_value=page):
            result = pw.sync_boards(self.config, self.state())
        with pw.locked_state() as state:
            pw.apply_sync(state, result, self.config)
        self.assertIn("old", self.state()["pins"])


class PersistedSchemaReviewTests(Sandbox):
    def test_malformed_pin_and_cached_listing_records_are_filtered(self):
        raw = {
            "version": pw.STATE_VERSION,
            "pins": {
                "valid": {"width": 300, "height": 200, "luma": 1, "avg": "#112233", "board": BOARD,
                          "boards": [BOARD, {}, 4], "url": "https://i.pinimg.com/originals/a.jpg",
                          "color": "#112233", "video": False},
                "bad": {"width": 300, "height": 200, "boards": {"oops": 1}, "board": []},
            },
            "listings": {BOARD: {"stamp": {}, "pins": [{"key": "only-key"}]}},
            "screens": [{"id": 1, "name": "Good", "width": 100, "height": 80},
                        {"id": 2, "name": "Fraction", "width": 100.5, "height": 80}],
        }
        with open(pw.STATE_FILE, "w") as handle:
            json.dump(raw, handle)
        state = pw.load_state()
        self.assertEqual(state["pins"]["valid"]["boards"], [BOARD])
        self.assertEqual(state["pins"]["valid"]["board"], BOARD)
        self.assertNotIn("board", state["pins"]["bad"])
        self.assertNotIn("boards", state["pins"]["bad"])
        self.assertEqual(state["listings"], {})
        self.assertEqual(len(state["screens"]), 1)
        result = {"listings": [], "failures": {}, "offline": [], "pins": {}, "retained": [], "retry": False}
        pw.apply_sync(state, result, self.config)
        self.assertIn("valid", state["pins"])

    def test_malformed_version_fails_closed_without_rewriting_bytes(self):
        raw = b'{"version":"2","banned":[null]}'
        with open(pw.STATE_FILE, "wb") as handle:
            handle.write(raw)
        with self.assertRaises(ValueError):
            pw.load_state()
        with open(pw.STATE_FILE, "rb") as handle:
            self.assertEqual(handle.read(), raw)

    def test_legacy_banned_none_migrates_without_crashing(self):
        with open(pw.STATE_FILE, "w") as handle:
            json.dump({"version": 1, "banned": [None, "legacy"]}, handle)
        self.assertEqual(pw.load_state()["banned"], ["legacy"])


class SyncAndSelectionReviewTests(Sandbox):
    def test_unreadable_cached_source_is_retained_and_scheduled_for_retry(self):
        self.add_pin("1")
        with pw.locked_state() as state:
            state["pins"]["1"].pop("luma")
            state["pins"]["1"].pop("avg")
        with mock.patch.object(pw, "fetch_board", return_value=make_listing(["1"])), \
                mock.patch.object(pw, "decode", side_effect=OSError("bad cached file")):
            result = pw.sync_boards(self.config, self.state())
        self.assertIn("1", result["retained"])
        self.assertTrue(result["retry"])
        with pw.locked_state() as state:
            pw.apply_sync(state, result, self.config)
        self.assertIn("1", self.state()["pins"])
        self.assertTrue(self.state()["refresh_pending"])

    def test_offline_plus_rate_limited_board_is_bounded_retry_not_offline(self):
        other = "https://www.pinterest.com/someone/dogs/"
        self.config["boards"] = [BOARD, other]
        def fetch(url, *args):
            if url == BOARD:
                raise pw.Offline("disconnected")
            raise requests.exceptions.RetryError("rate limited")
        with mock.patch.object(pw, "fetch_board", side_effect=fetch):
            result = pw.sync_boards(self.config, self.state())
        self.assertEqual(result["offline"], [BOARD])
        self.assertTrue(result["retry"])
        with pw.locked_state() as state:
            previous_success = state["last_refresh_ok"]
            pw.apply_sync(state, result, self.config)
        self.assertTrue(self.state()["refresh_pending"])
        self.assertEqual(self.state()["last_refresh_ok"], previous_success)

    def test_two_display_draw_prefers_unique_new_assignments(self):
        for key in ("a", "b"):
            self.add_pin(key)
        state = self.state()
        state["history"] = [["b"]]
        state["current"] = ["b"]
        state["deck"] = ["a", "a"]
        picks = pw.draw(state, ["a", "b"], 2, self.config, None)
        self.assertEqual(picks, ["a", "b"])


class TimeAndBoundaryReviewTests(Sandbox):
    def test_unrepresentable_pause_duration_is_rejected_before_state_write(self):
        with self.assertRaises(ValueError):
            pw.cmd_pause(self.config, ["1" + "0" * 30 + "d"])
        self.assertFalse(os.path.exists(pw.STATE_FILE))

    def test_unrepresentable_persisted_pause_and_time_labels_are_safe(self):
        with open(pw.STATE_FILE, "w") as handle:
            json.dump({"version": pw.STATE_VERSION, "paused_until": 1e300, "last_refresh_ok": 1e300}, handle)
        state = pw.load_state()
        self.assertEqual(state["paused_until"], 0)
        self.assertEqual(state["last_refresh_ok"], 0)
        self.assertIn("unknown", pw.pause_label(dict(state, paused_until=1e300)))
        self.assertIn("unknown", pw.fmt_time(1e300))

    def test_hour_configuration_that_overflows_seconds_is_rejected(self):
        self.assertFalse(pw.CONFIG_RULES["refresh_hours"][0](1e308))
        self.assertFalse(pw.CONFIG_RULES["retry_hours"][0](1e308))

    def test_load_config_warns_and_defaults_for_huge_integer_hour_values(self):
        with open(pw.CONFIG_FILE, "w") as handle:
            json.dump({"refresh_hours": 10 ** 305, "retry_hours": 10 ** 308}, handle)
        config, warnings = pw.load_config()
        self.assertEqual(config["refresh_hours"], pw.DEFAULT_CONFIG["refresh_hours"])
        self.assertEqual(config["retry_hours"], pw.DEFAULT_CONFIG["retry_hours"])
        self.assertEqual(len([warning for warning in warnings if "hours representable" in warning]), 2)

    def test_launchctl_probe_has_timeout_and_failure_is_reported(self):
        with mock.patch.object(pw.subprocess, "run", side_effect=subprocess.TimeoutExpired("launchctl", 5)) as run, self.assertRaises(RuntimeError):
            pw.launchctl_interval(pw.LABEL)
        self.assertEqual(run.call_args.kwargs["timeout"], 5)
        with mock.patch.object(pw, "launchctl_interval", side_effect=RuntimeError("probe timed out")):
            self.assertIn("unavailable", pw.agent_summary(self.config))

    def test_launchctl_probe_timeout_does_not_delete_agent_or_claim_install(self):
        os.makedirs(pw.LAUNCH_AGENTS)
        plist = pw.plist_path()
        with open(plist, "w") as handle:
            handle.write("keep")
        with mock.patch.object(pw.subprocess, "run", side_effect=subprocess.TimeoutExpired("launchctl", 5)):
            self.assertEqual(pw.cmd_install(self.config, []), 1)
            self.assertEqual(pw.cmd_uninstall(self.config, []), 1)
        self.assertTrue(os.path.exists(plist))

    def test_status_survives_launchctl_probe_failure(self):
        with mock.patch.object(pw, "launchctl_interval", side_effect=RuntimeError("probe timed out")):
            code, output = self.printed(pw.cmd_status)
        self.assertEqual(code, 0)
        self.assertIn("status unavailable", output)

    def test_requests_adapter_does_not_retry_status_before_bounded_helper(self):
        session = pw.make_session()
        retry = session.get_adapter("https://www.pinterest.com/").max_retries
        self.assertEqual(retry.status, 0)
        self.assertEqual(retry.status_forcelist, set())
        self.assertFalse(retry.respect_retry_after_header)
        session.close()

    def test_external_image_pixel_limit_precedes_decode(self):
        content = bytearray(self.png_header())
        content[16:20] = (pw.MAX_DOWNLOAD_PIXELS + 1).to_bytes(4, "big")
        content[20:24] = (1).to_bytes(4, "big")
        content[29:33] = zlib.crc32(content[12:29]).to_bytes(4, "big")
        response = FakeResponse(content=bytes(content), content_type="image/png")
        session = mock.Mock(get=lambda url, **kwargs: response)
        with mock.patch.object(pw, "worker_session", return_value=session), mock.patch.object(
            pw, "decode", side_effect=AssertionError("decode must not run")
        ), self.assertRaisesRegex(ValueError, "pixel limit"):
            pw.download_source({"key": "large", "url": "https://i.pinimg.com/originals/large.png"})

    @staticmethod
    def png_header():
        from helpers import image_bytes
        return image_bytes((1, 1), fmt="PNG")


class SelectionAndSchedulingFollowUpTests(Sandbox):
    def test_second_display_avoids_what_is_on_screen_when_the_pool_allows(self):
        for key in ("a", "b", "c", "d"):
            self.add_pin(key)
        state = self.state()
        state["current"] = ["a", "b"]
        state["deck"] = ["c", "a", "b", "d"]
        self.assertEqual(pw.draw(state, ["a", "b", "c", "d"], 2, self.config, None), ["c", "d"])

    def test_slow_board_is_retried_rather_than_reported_offline(self):
        with mock.patch.object(pw, "scrape_board", side_effect=pw.DeadlineExceeded("HTTP deadline elapsed")), \
                self.assertRaises(requests.exceptions.RetryError):
            pw.fetch_board(BOARD, 250)

    def test_manual_rotation_decides_the_low_resolution_filter(self):
        self.config.update(skip_upscale_over=1.5, orientation_model="model.onnx")
        state = self.state()
        state["screens"] = [{"id": 1, "name": "Wide", "width": 1000, "height": 400}]
        state["rotations"] = {"1": 90}
        with mock.patch.object(pw, "fetch_board", return_value=make_listing(["1"], width=1000, height=500)), \
                mock.patch.object(pw, "try_download", side_effect=AssertionError("must be skipped")):
            self.assertEqual(pw.sync_boards(self.config, state)["pins"], {})
