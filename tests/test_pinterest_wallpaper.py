import copy
import hashlib
import json
import os
import random
import shutil
import time
import unittest
from unittest import mock

import requests
from PIL import Image

from helpers import (
    BOARD,
    REAL,
    SCREEN,
    FakeResponse,
    Sandbox,
    board_page,
    feed_pin,
    image_bytes,
    make_listing,
    pw,
)


class ConfigTests(Sandbox):

    def write_config(self, data):

        with open(pw.CONFIG_FILE, "w") as handle:
            json.dump(data, handle)

    def test_wrong_types_fall_back_to_defaults_with_a_warning(self):

        self.write_config({"boards": [BOARD], "refresh_hours": "24", "fit_mode": "stretch", "refresh_hour": 3})
        config, problems = pw.load_config()

        self.assertEqual(config["refresh_hours"], 24)
        self.assertEqual(config["fit_mode"], "cover")
        self.assertEqual(len(problems), 3)
        self.assertTrue(any("unknown key 'refresh_hour'" in p for p in problems))

    def test_single_board_string_is_accepted(self):

        self.write_config({"boards": BOARD})

        self.assertEqual(pw.load_config()[0]["boards"], [BOARD])

    def test_string_refresh_hours_no_longer_crashes_tick(self):

        self.write_config({"boards": [BOARD], "refresh_hours": "24"})
        self.add_pin("1")
        config, _ = pw.load_config()

        with pw.locked_state() as state:
            state["last_refresh_ok"] = time.time()

        self.assertEqual(pw.cmd_tick(config, []), 0)
        self.assertEqual(len(self.shown), 1)


class StateTests(Sandbox):

    def test_v1_state_is_migrated_and_forces_a_refresh(self):

        with open(pw.STATE_FILE, "w") as handle:
            json.dump({"last_refresh_ok": 123, "deck": ["abc.100x100.jpg"], "history": ["abc.100x100.jpg"], "banned": ["d41d8cd9"]}, handle)

        state = pw.load_state()

        self.assertEqual(state["version"], pw.STATE_VERSION)
        self.assertEqual(state["history"], [])
        self.assertEqual(state["banned"], ["d41d8cd9"])
        self.assertEqual(state["last_refresh_ok"], 0)

    def test_corrupt_state_is_backed_up_not_silently_dropped(self):

        with open(pw.STATE_FILE, "w") as handle:
            handle.write("{not json")

        with mock.patch.object(pw, "log_error") as log_error:
            pw.load_state()

        backups = [n for n in os.listdir(self.root) if n.startswith(".state.json.corrupt-")]
        self.assertEqual(len(backups), 1)
        log_error.assert_called_once()

    def test_ban_during_refresh_survives(self):

        self.add_pin("1")
        self.add_pin("2")

        with pw.locked_state() as state:
            state["history"] = [["1"]]

        listing = make_listing(["1", "2"])

        def slow_fetch(url, *rest):
            pw.cmd_ban(self.config, [])
            return listing

        with mock.patch.object(pw, "fetch_board", slow_fetch):
            pw.do_refresh(self.config)

        self.assertEqual(self.state()["banned"], ["1"])

    def test_second_refresh_is_skipped_while_one_runs(self):

        with pw.file_lock(pw.REFRESH_LOCK, wait=False) as acquired:
            self.assertTrue(acquired)
            with mock.patch.object(pw, "fetch_board") as fetch:
                self.assertFalse(pw.do_refresh(self.config))
                fetch.assert_not_called()


class ScrapeTests(Sandbox):

    def test_parses_raw_and_escaped_initial_props(self):

        items = [
            feed_pin("11", "https://i.pinimg.com/originals/a.jpg"),
            {"type": "story", "id": "s1"},
            feed_pin("12", "https://i.pinimg.com/originals/b.png"),
        ]

        for escape in (False, True):
            with mock.patch.object(pw.SESSION, "get", return_value=FakeResponse(board_page(items, escape=escape))):
                listing = pw.scrape_board(BOARD, 250)

            self.assertEqual([p["key"] for p in listing["pins"]], ["11", "12"])
            self.assertEqual(listing["name"], "Cats")
            self.assertTrue(listing["complete"])

    def test_follows_bookmarks_until_the_end(self):

        page = FakeResponse(board_page([feed_pin("1", "https://i.pinimg.com/originals/1.jpg")], bookmark="b1"))
        api = [
            FakeResponse(body={"resource_response": {"data": [feed_pin("2", "https://i.pinimg.com/originals/2.jpg")], "bookmark": "b2"}}),
            FakeResponse(body={"resource_response": {"data": [feed_pin("3", "https://i.pinimg.com/originals/3.jpg")], "bookmark": None}}),
        ]
        calls = []

        def get(url, **kwargs):
            calls.append(kwargs.get("params"))
            return page if url == BOARD else api.pop(0)

        with mock.patch.object(pw.SESSION, "get", get):
            listing = pw.scrape_board(BOARD, 250)

        self.assertEqual([p["key"] for p in listing["pins"]], ["1", "2", "3"])
        self.assertTrue(listing["complete"])
        self.assertEqual(json.loads(calls[1]["data"])["options"]["bookmarks"], ["b1"])
        self.assertEqual(json.loads(calls[1]["data"])["options"]["board_id"], "42")

    def test_paging_stops_at_max_pins(self):

        page = FakeResponse(board_page([feed_pin(str(i), f"https://i.pinimg.com/originals/{i}.jpg") for i in range(3)], bookmark="b1"))

        with mock.patch.object(pw.SESSION, "get", return_value=page):
            listing = pw.scrape_board(BOARD, 2)

        self.assertEqual(len(listing["pins"]), 2)
        self.assertTrue(listing["complete"])

    def test_paging_error_marks_listing_partial(self):

        page = FakeResponse(board_page([feed_pin("1", "https://i.pinimg.com/originals/1.jpg")], bookmark="b1"))

        def get(url, **kwargs):
            return page if url == BOARD else FakeResponse(status=403)

        with mock.patch.object(pw.SESSION, "get", get), mock.patch.object(pw, "log_error"):
            listing = pw.scrape_board(BOARD, 250)

        self.assertFalse(listing["complete"])

    def test_rss_yields_pin_ids_and_original_urls(self):

        item = (
            "<item><title> </title><link>https://www.pinterest.com/pin/77/</link>"
            "<description>&lt;img src=&quot;https://i.pinimg.com/236x/ab/cd/abcd.jpg&quot;&gt;</description></item>"
        )
        rss = f"<rss><channel><title>Cats &amp; co</title>{item}</channel></rss>"

        with mock.patch.object(pw.SESSION, "get", return_value=FakeResponse(rss)):
            listing = pw.scrape_board_rss(BOARD, 250)

        self.assertEqual(listing["name"], "Cats & co")
        self.assertEqual(listing["pins"][0]["key"], "77")
        self.assertEqual(listing["pins"][0]["url"], "https://i.pinimg.com/originals/ab/cd/abcd.jpg")
        self.assertFalse(listing["complete"])

    def test_network_error_is_offline_and_skips_rss(self):

        offline = requests.ConnectionError("dns")

        with mock.patch.object(pw.SESSION, "get", side_effect=offline) as get, self.assertRaises(pw.Offline):
            pw.fetch_board(BOARD, 250)

        self.assertEqual(get.call_count, 1)


class RefreshTests(Sandbox):

    def listing(self, keys, url=BOARD, complete=True):

        return make_listing(keys, url, complete, color="#123456")

    def serve_images(self):

        return mock.patch.object(
            pw, "worker_session",
            return_value=mock.Mock(get=lambda url, **kw: FakeResponse(content=image_bytes(), content_type="image/jpeg")),
        )

    def test_offline_does_not_count_as_a_failure(self):

        with pw.locked_state() as state:
            state["last_refresh_attempt"] = 5

        with mock.patch.object(pw, "fetch_board", side_effect=pw.Offline("dns")):
            for _ in range(4):
                pw.do_refresh(self.config)

        state = self.state()
        self.assertEqual(state["board_fails"], {})
        self.assertEqual(state["last_refresh_attempt"], 5)
        pw.notify.assert_not_called()

    def test_real_failures_alert_once_per_board(self):

        with mock.patch.object(pw, "fetch_board", side_effect=RuntimeError("HTTP 404")), mock.patch.object(pw, "log_error"):
            for _ in range(4):
                pw.do_refresh(self.config)

        self.assertEqual(self.state()["board_fails"][BOARD]["count"], 4)
        self.assertEqual(pw.notify.call_count, 1)
        self.assertIn("404", pw.notify.call_args[0][2])

    def test_one_broken_board_keeps_the_others_and_its_own_pins(self):

        other = "https://www.pinterest.com/someone/dogs/"
        self.config["boards"] = [BOARD, other]

        with self.serve_images(), mock.patch.object(pw, "fetch_board", side_effect=lambda url, *rest: self.listing(["1", "2"], url) if url == BOARD else self.listing(["9"], url)):
            pw.do_refresh(self.config)

        def flaky(url, *rest):
            if url == other:
                raise RuntimeError("HTTP 500")
            return self.listing(["1", "3"], url)

        with self.serve_images(), mock.patch.object(pw, "fetch_board", flaky), mock.patch.object(pw, "log_error"):
            pw.do_refresh(self.config)

        state = self.state()
        self.assertEqual(sorted(state["pins"]), ["1", "3", "9"])
        self.assertEqual(sorted(pw.source_files()), ["1", "3", "9"])

    def test_new_pins_notify_and_go_first_but_not_on_first_sync(self):

        with self.serve_images(), mock.patch.object(pw, "fetch_board", return_value=self.listing(["1", "2"])):
            pw.do_refresh(self.config)

        pw.notify.assert_not_called()

        with self.serve_images(), mock.patch.object(pw, "fetch_board", return_value=self.listing(["3", "1", "2"])):
            pw.do_refresh(self.config)

        self.assertEqual(self.state()["deck"][0], "3")
        self.assertIn("1 new pin", pw.notify.call_args[0][2])

    def test_deleting_render_cache_does_not_fake_new_pins(self):

        with self.serve_images(), mock.patch.object(pw, "fetch_board", return_value=self.listing(["1", "2"])):
            pw.do_refresh(self.config)
            shutil.rmtree(pw.WALLPAPERS_DIR)
            os.makedirs(pw.WALLPAPERS_DIR)
            pw.do_refresh(self.config)

        pw.notify.assert_not_called()

    def test_legacy_md5_bans_are_matched_to_pin_ids(self):

        url = "https://i.pinimg.com/originals/1.jpg"

        with pw.locked_state() as state:
            state["banned"] = [hashlib.md5(url.encode()).hexdigest()]

        with self.serve_images(), mock.patch.object(pw, "fetch_board", return_value=self.listing(["1", "2"])):
            pw.do_refresh(self.config)

        self.assertEqual(self.state()["banned"], ["1"])


class RotationTests(Sandbox):

    def test_prev_onto_a_missing_image_leaves_state_consistent(self):

        for key in ("A", "C"):
            self.add_pin(key)

        with pw.locked_state() as state:
            state["history"] = [["A"], ["B"], ["C"]]

        pw.cmd_prev(self.config, [])

        state = self.state()
        self.assertEqual(pw.current_pins(state), ["A"])
        self.assertTrue(self.shown[-1][0].startswith("A."))

    def test_failed_prev_changes_nothing(self):

        for key in ("A", "C"):
            self.add_pin(key)

        with pw.locked_state() as state:
            state["history"] = [["A"], ["C"]]

        with mock.patch.object(pw, "set_wallpapers", side_effect=RuntimeError("boom")), mock.patch.object(pw, "log_error"):
            pw.cmd_prev(self.config, [])

        self.assertEqual(self.state()["history"], [["A"], ["C"]])

        pw.cmd_ban(self.config, [])
        self.assertEqual(self.state()["banned"], ["C"])

    def test_unreadable_cached_image_is_skipped_not_fatal(self):

        self.add_pin("good")
        self.add_pin("bad")

        path = os.path.join(pw.IMAGE_DIR, "bad.jpg")
        with open(path, "rb") as handle:
            data = handle.read()
        with open(path, "wb") as handle:
            handle.write(data[: len(data) // 3])

        with pw.locked_state() as state:
            state["deck"] = ["bad", "good"]

        with mock.patch.object(pw, "log_error"):
            self.assertEqual(pw.cmd_next(self.config, []), 0)

        self.assertTrue(self.shown[-1][0].startswith("good."))
        self.assertNotIn("bad", pw.source_files())

    def test_every_pin_once_per_cycle_and_no_immediate_repeat(self):

        for key in "abcde":
            self.add_pin(key)

        for _ in range(15):
            pw.cmd_next(self.config, [])

        shown = [names[0].split(".")[0] for names in self.shown]
        self.assertEqual(sorted(shown[:5]), list("abcde"))
        self.assertTrue(all(x != y for x, y in zip(shown, shown[1:])))

    def test_favorites_come_up_weight_times_per_cycle_spread_out(self):

        pool = [str(i) for i in range(10)]

        for _ in range(50):
            deck = pw.build_deck(pool, {"3"}, 3)
            positions = [i for i, key in enumerate(deck) if key == "3"]
            self.assertEqual(len(positions), 3)
            self.assertTrue(all(b - a > 1 for a, b in zip(positions, positions[1:])))
            self.assertEqual(sorted(set(deck)), sorted(pool))

    def test_mood_prefers_dark_pins_without_breaking_the_bag(self):

        self.add_pin("dark1", color="black", luma=10)
        self.add_pin("dark2", color="black", luma=20)
        self.add_pin("light1", color="white", luma=240)
        self.add_pin("light2", color="white", luma=250)
        self.config["mood"] = "night"
        self.config["night_hours"] = [0, 0]

        with mock.patch.object(pw, "mood_preference", return_value=True):
            pw.cmd_next(self.config, [])
            pw.cmd_next(self.config, [])
            pw.cmd_next(self.config, [])

        shown = [names[0].split(".")[0] for names in self.shown]
        self.assertEqual(sorted(shown[:2]), ["dark1", "dark2"])
        self.assertTrue(shown[2].startswith("light"))

    def test_tick_respects_pause_recent_change_and_lock(self):

        self.add_pin("1")
        self.add_pin("2")

        with pw.locked_state() as state:
            state["last_refresh_ok"] = time.time()
            state["paused_until"] = -1

        pw.cmd_tick(self.config, [])
        self.assertEqual(self.shown, [])

        pw.cmd_resume(self.config, [])
        pw.cmd_next(self.config, [])
        pw.cmd_tick(self.config, [])
        self.assertEqual(len(self.shown), 1)

        with pw.locked_state() as state:
            state["last_change"] = 0

        with mock.patch.object(pw, "screen_locked", return_value=True):
            pw.cmd_tick(self.config, [])
        self.assertEqual(len(self.shown), 1)

        pw.cmd_tick(self.config, [])
        self.assertEqual(len(self.shown), 2)

    def test_timed_pause_expires(self):

        with pw.locked_state() as state:
            state["paused_until"] = int(time.time()) - 1

        self.assertFalse(pw.is_paused(self.state()))

    def test_different_pins_per_display(self):

        for key in "abc":
            self.add_pin(key)

        self.screens = [dict(SCREEN), {"id": 2, "name": "External", "width": 200, "height": 100}]
        self.config["different_per_display"] = True

        pw.cmd_next(self.config, [])

        left, right = self.shown[-1]
        self.assertNotEqual(left.split(".")[0], right.split(".")[0])
        self.assertIn(".160x100.", left)
        self.assertIn(".200x100.", right)

        pw.cmd_ban(self.config, ["2"])
        self.assertEqual(self.state()["banned"], [right.split(".")[0]])

    def test_same_pin_rendered_per_display_size(self):

        self.add_pin("a")
        self.screens = [dict(SCREEN), {"id": 2, "name": "External", "width": 200, "height": 100}]

        pw.cmd_next(self.config, [])

        self.assertEqual([n.split(".")[:2] for n in self.shown[-1]], [["a", "160x100"], ["a", "200x100"]])


class RenderTests(Sandbox):

    def settings(self, **overrides):

        base = pw.render_settings("k", self.config, copy.deepcopy(pw.DEFAULT_STATE), False)
        base.update(overrides)
        return base

    def test_rotate_cw_turns_a_vertical_pin_sideways(self):

        img = Image.new("RGB", (100, 200), "blue")
        img.paste(Image.new("RGB", (100, 100), "red"), (0, 0))

        out = pw.render_image(img, (200, 100), self.settings(rotate=90))

        self.assertGreater(out.getpixel((150, 50))[0], 200)
        self.assertGreater(out.getpixel((50, 50))[2], 200)

    def test_rotate_command_redraws_and_persists(self):

        self.add_pin("v", size=(300, 600))
        pw.cmd_next(self.config, [])
        before = self.shown[-1][0]

        pw.cmd_rotate(self.config, ["cw"])
        self.assertEqual(self.state()["rotations"], {"v": 90})
        self.assertNotEqual(self.shown[-1][0], before)

        pw.cmd_rotate(self.config, ["ccw"])
        self.assertEqual(self.state()["rotations"], {})

        pw.cmd_rotate(self.config, ["180"])
        self.assertEqual(self.state()["rotations"], {"v": 180})

        pw.cmd_rotate(self.config, ["reset"])
        self.assertEqual(self.state()["rotations"], {})

    def test_max_upscale_shows_small_pins_on_a_backdrop(self):

        img = Image.new("RGB", (40, 25), "white")

        capped = pw.render_image(img, (160, 100), self.settings(max_upscale=2, backdrop="color"))
        full = pw.render_image(img, (160, 100), self.settings())

        self.assertEqual(capped.getpixel((5, 5)), (255, 255, 255))
        self.assertEqual(capped.getpixel((80, 50)), (255, 255, 255))

        dark = Image.new("RGB", (40, 25), "black")
        dark.paste(Image.new("RGB", (20, 12), "white"), (10, 6))
        capped = pw.render_image(dark, (160, 100), self.settings(max_upscale=2, backdrop="color"))
        self.assertEqual(capped.size, full.size)
        self.assertNotEqual(capped.getpixel((2, 2)), pw.render_image(dark, (160, 100), self.settings()).getpixel((2, 2)))

    def test_sharpen_follows_the_real_scale_factor(self):

        with mock.patch.object(pw, "sharpen", side_effect=lambda img: img) as sharpen:
            pw.render_image(Image.new("RGB", (4000, 1500)), (3456, 2234), self.settings())
            self.assertEqual(sharpen.call_count, 1)

            sharpen.reset_mock()
            pw.render_image(Image.new("RGB", (3000, 4500)), (3456, 2234), self.settings(fit="blur"))
            sharpen.assert_not_called()

    def test_render_tag_ignores_unrelated_settings(self):

        state = copy.deepcopy(pw.DEFAULT_STATE)
        base = pw.settings_tag(pw.render_settings("k", self.config, state, False))

        self.config["notifications"] = False
        self.config["refresh_hours"] = 1
        self.assertEqual(pw.settings_tag(pw.render_settings("k", self.config, state, False)), base)

        self.config["fit_mode"] = "blur"
        self.assertNotEqual(pw.settings_tag(pw.render_settings("k", self.config, state, False)), base)

    def test_config_change_applies_on_next_change_without_refresh(self):

        self.add_pin("a")
        pw.cmd_next(self.config, [])
        first = self.shown[-1][0]

        self.config["fit_mode"] = "blur"
        pw.cmd_shuffle(self.config, [])

        self.assertNotEqual(self.shown[-1][0], first)
        self.assertEqual(self.state()["on_screen"], [self.shown[-1][0]])

    def test_ai_upscaler_output_is_used(self):

        self.config["upscaler"] = self.make_upscaler()
        self.add_pin("low", size=(400, 300))

        state = self.state()
        state["screens"] = [{"id": 1, "name": "Big", "width": 3200, "height": 2000}]
        files = pw.source_files()
        pw.upscale_sources(self.config, state, files, 5)

        with Image.open(os.path.join(pw.UPSCALED_DIR, "low.jpg")) as up:
            self.assertEqual(up.size, (1600, 1200))

        src, ai = pw.source_for("low", self.config, files)
        self.assertTrue(ai)
        self.assertTrue(src.startswith(pw.UPSCALED_DIR))

    def test_download_keeps_original_bytes_and_rejects_truncation(self):

        body = image_bytes((500, 400))
        session = mock.Mock(get=lambda url, **kw: FakeResponse(content=body, content_type="image/jpeg"))

        with mock.patch.object(pw, "worker_session", return_value=session):
            filename, meta = pw.download_source({"key": "5", "url": "https://x/5.jpg"})

        with open(os.path.join(pw.IMAGE_DIR, filename), "rb") as handle:
            self.assertEqual(handle.read(), body)
        self.assertEqual((meta["width"], meta["height"]), (500, 400))

        truncated = mock.Mock(get=lambda url, **kw: FakeResponse(content=body[:200], content_type="image/jpeg"))

        with mock.patch.object(pw, "worker_session", return_value=truncated), self.assertRaises(OSError):
            pw.download_source({"key": "6", "url": "https://x/6.jpg"})

        self.assertNotIn("6", pw.source_files())


class ReviewFixTests(Sandbox):

    def listing(self, keys, complete=True):

        return make_listing(keys, complete=complete)

    def test_mood_never_reshows_the_pin_on_screen(self):

        for seed in range(300):
            random.seed(seed)
            state = copy.deepcopy(pw.DEFAULT_STATE)
            state["pins"] = {key: {"luma": luma} for key, luma in {"d1": 10, "d2": 20, "l1": 240, "l2": 250}.items()}
            state["history"] = [["d1"]]
            self.assertNotEqual(pw.draw(state, list(state["pins"]), 1, self.config, True), ["d1"])

    def test_long_mood_and_favorite_runs_never_repeat_back_to_back(self):

        for key, luma in {"d1": 10, "d2": 20, "l1": 240, "l2": 250, "m": 128}.items():
            self.add_pin(key, luma=luma)

        with pw.locked_state() as state:
            state["favorites"] = ["d1"]

        self.config["favorite_weight"] = 3

        for prefer_dark in (True, False, True):
            with mock.patch.object(pw, "mood_preference", return_value=prefer_dark):
                for _ in range(40):
                    pw.cmd_next(self.config, [])

        shown = [names[0].split(".")[0] for names in self.shown]
        self.assertEqual(len(shown), 120)
        self.assertTrue(all(a != b for a, b in zip(shown, shown[1:])))

    def test_feed_refuses_odd_ids_and_foreign_image_hosts(self):

        base = {"type": "pin", "images": {"orig": {"url": "https://i.pinimg.com/originals/a.jpg"}}}

        self.assertIsNone(pw.pin_from_feed(dict(base, id="../../escaped"), BOARD))
        self.assertIsNone(pw.pin_from_feed(dict(base, id="12.5"), BOARD))
        self.assertIsNone(pw.pin_from_feed(
            {"type": "pin", "id": "1", "images": {"orig": {"url": "https://evil.example/a.jpg"}}}, BOARD
        ))
        self.assertEqual(pw.pin_from_feed(dict(base, id=123), BOARD)["key"], "123")

    def test_menubar_neutralises_hostile_board_names(self):

        self.add_pin("1")
        pw.cmd_next(self.config, [])

        with pw.locked_state() as state:
            state["board_names"] = ["Cats | shell=/bin/sh param1=-c\n--Run me | shell=/usr/bin/say"]

        with mock.patch("builtins.print") as printed:
            pw.cmd_menubar(self.config, [])

        lines = [c.args[0] for c in printed.call_args_list]
        board_line = next(line for line in lines if line.startswith("Cats"))

        self.assertNotIn("|", board_line)
        self.assertFalse(any(line.startswith("--Run") for line in lines))

    def test_rss_png_original_is_found_through_fallbacks(self):

        item = (
            "<item><link>https://www.pinterest.com/pin/77/</link>"
            "<description>&lt;img src=&quot;https://i.pinimg.com/236x/ab/cd/abcd.jpg&quot;&gt;</description></item>"
        )

        with mock.patch.object(pw.SESSION, "get", return_value=FakeResponse(f"<rss><channel><title>C</title>{item}</channel></rss>")):
            pin = pw.scrape_board_rss(BOARD, 250)["pins"][0]

        requested = []

        def get(url, **kwargs):
            requested.append(url)
            if url.endswith(".png"):
                return FakeResponse(content=image_bytes((400, 400), fmt="PNG"), content_type="image/png")
            return FakeResponse(status=403, content_type="application/xml")

        with mock.patch.object(pw, "worker_session", return_value=mock.Mock(get=get)):
            filename, _ = pw.download_source(pin)

        self.assertEqual(filename, "77.png")
        self.assertEqual(requested, [
            "https://i.pinimg.com/originals/ab/cd/abcd.jpg",
            "https://i.pinimg.com/originals/ab/cd/abcd.png",
        ])

    def test_clock_set_back_does_not_freeze_rotation_or_refresh(self):

        self.add_pin("1")
        future = time.time() + 365 * 86400

        with pw.locked_state() as state:
            state["last_change"] = time.time() + 6 * 3600
            state["last_refresh_ok"] = future
            state["last_refresh_attempt"] = future

        state = self.state()
        self.assertIsNone(pw.tick_skip_reason(self.config, state))
        self.assertTrue(pw.refresh_due(self.config, state))

    def test_rate_limited_board_is_not_a_failure_and_skips_rss(self):

        self.add_pin("1")
        calls = []

        def get(url, **kwargs):
            calls.append(url)
            raise requests.exceptions.RetryError("too many 429 error responses")

        with mock.patch.object(pw.SESSION, "get", get):
            for _ in range(4):
                pw.do_refresh(self.config)

        state = self.state()
        self.assertEqual(state["board_fails"], {})
        self.assertEqual(len(calls), 4)
        self.assertIn("1", state["pins"])
        pw.notify.assert_not_called()

    def test_render_error_keeps_the_deck(self):

        for key in "abc":
            self.add_pin(key)

        with pw.locked_state() as state:
            state["deck"] = ["a", "b", "c"]

        with mock.patch.object(pw, "save_jpeg", side_effect=OSError(28, "No space left on device")):
            self.assertEqual(pw.cmd_next(self.config, []), 1)

        self.assertEqual(self.state()["deck"], ["a", "b", "c"])

    def test_refresh_keeps_the_render_on_screen_after_a_settings_change(self):

        self.add_pin("a")
        self.add_pin("b")
        pw.cmd_next(self.config, [])
        on_screen = self.shown[-1][0]

        self.config["fit_mode"] = "blur"

        with mock.patch.object(pw, "fetch_board", return_value=self.listing(["a", "b"])):
            pw.do_refresh(self.config)

        self.assertTrue(os.path.exists(os.path.join(pw.WALLPAPERS_DIR, on_screen)))

    def test_rotating_a_pin_that_left_the_board_explains_why(self):

        self.add_pin("a")
        pw.cmd_next(self.config, [])
        os.remove(os.path.join(pw.IMAGE_DIR, "a.jpg"))

        self.assertEqual(pw.cmd_rotate(self.config, []), 1)
        self.assertIn("no longer cached", pw.log_error.call_args[0][0])

    def test_display_numbers_past_the_last_display_are_refused(self):

        for key in "ab":
            self.add_pin(key)

        self.screens = [dict(SCREEN), {"id": 2, "name": "External", "width": 200, "height": 100}]
        self.config["different_per_display"] = True
        pw.cmd_next(self.config, [])

        with self.assertRaises(ValueError):
            pw.cmd_ban(self.config, ["3"])

        self.assertEqual(self.state()["banned"], [])

    def test_one_pin_on_two_displays_still_accepts_display_two(self):

        self.add_pin("a")
        self.screens = [dict(SCREEN), {"id": 2, "name": "External", "width": 200, "height": 100}]
        pw.cmd_next(self.config, [])

        pw.cmd_fav(self.config, ["2"])
        self.assertEqual(self.state()["favorites"], ["a"])

    def test_tick_changes_the_wallpaper_before_upscaling_a_few(self):

        self.config["upscaler"] = self.make_upscaler()

        for key in "abcd":
            self.add_pin(key, size=(40, 30))

        with pw.locked_state() as state:
            state["last_refresh_ok"] = state["last_refresh_attempt"] = time.time()

        events = []
        real_upscaler = pw.run_upscaler

        with mock.patch.object(pw, "set_wallpapers", lambda assignment: events.append("set")), \
                mock.patch.object(pw, "run_upscaler", lambda *a: (events.append("upscale"), real_upscaler(*a))):
            pw.cmd_tick(self.config, [])

        self.assertEqual(events, ["set"] + ["upscale"] * pw.UPSCALES_PER_TICK)

    def test_failed_upscale_is_not_retried_every_tick(self):

        self.config["upscaler"] = self.make_upscaler()
        self.add_pin("a", size=(40, 30))

        with pw.locked_state() as state:
            state["last_refresh_ok"] = state["last_refresh_attempt"] = time.time()

        with mock.patch.object(pw, "run_upscaler", side_effect=RuntimeError("unsupported")) as upscaler:
            pw.cmd_tick(self.config, [])
            pw.cmd_tick(self.config, [])

        self.assertEqual(upscaler.call_count, 1)

    def test_partial_listing_keeps_pins_it_did_not_list(self):

        session = mock.Mock(get=lambda url, **kw: FakeResponse(content=image_bytes(), content_type="image/jpeg"))

        with mock.patch.object(pw, "worker_session", return_value=session):
            with mock.patch.object(pw, "fetch_board", return_value=self.listing(["1", "2", "3"])):
                pw.do_refresh(self.config)
            with mock.patch.object(pw, "fetch_board", return_value=self.listing(["1"], complete=False)):
                pw.do_refresh(self.config)

        self.assertEqual(sorted(self.state()["pins"]), ["1", "2", "3"])
        self.assertEqual(sorted(pw.source_files()), ["1", "2", "3"])

    def test_tick_refreshes_when_due(self):

        session = mock.Mock(get=lambda url, **kw: FakeResponse(content=image_bytes(), content_type="image/jpeg"))

        with mock.patch.object(pw, "worker_session", return_value=session), \
                mock.patch.object(pw, "fetch_board", return_value=self.listing(["1", "2"])) as fetch:
            pw.cmd_tick(self.config, [])

        fetch.assert_called_once()
        self.assertEqual(len(self.shown), 1)

    def test_setter_refuses_missing_files_before_calling_osascript(self):

        with mock.patch.object(pw, "osascript") as osascript, self.assertRaises(RuntimeError):
            REAL["set_wallpapers"]([({"id": 1}, "/nonexistent/x.jpg")])

        osascript.assert_not_called()


class CommandTests(Sandbox):

    def test_unban_requires_a_target(self):

        with mock.patch("sys.stderr"):
            self.assertEqual(pw.cmd_unban(self.config, []), 2)

    def test_unban_one_and_all(self):

        with pw.locked_state() as state:
            state["banned"] = ["1", "2", "3"]

        pw.cmd_unban(self.config, ["2"])
        self.assertEqual(self.state()["banned"], ["1", "3"])

        pw.cmd_unban(self.config, ["all"])
        self.assertEqual(self.state()["banned"], [])

    def test_fav_toggles(self):

        self.add_pin("1")
        pw.cmd_next(self.config, [])

        pw.cmd_fav(self.config, [])
        self.assertEqual(self.state()["favorites"], ["1"])

        pw.cmd_fav(self.config, [])
        self.assertEqual(self.state()["favorites"], [])

    def test_pause_parses_durations(self):

        pw.cmd_pause(self.config, ["2h"])
        self.assertAlmostEqual(self.state()["paused_until"], time.time() + 7200, delta=5)

        with self.assertRaises(ValueError):
            pw.cmd_pause(self.config, ["soon"])

    def test_status_and_menubar_do_not_write_state(self):

        self.add_pin("1")
        pw.cmd_next(self.config, [])
        before = os.path.getmtime(pw.STATE_FILE)

        with mock.patch("sys.stdout"), mock.patch.object(pw, "launchctl_interval", return_value=None):
            pw.cmd_status(self.config, [])
            pw.cmd_menubar(self.config, [])

        self.assertEqual(os.path.getmtime(pw.STATE_FILE), before)

    def test_menubar_lists_actions_for_the_current_pin(self):

        self.add_pin("123")
        pw.cmd_next(self.config, [])

        with mock.patch("builtins.print") as printed:
            pw.cmd_menubar(self.config, [])

        lines = [c.args[0] for c in printed.call_args_list]
        self.assertTrue(any(line.startswith("Pin 123 | href=https://www.pinterest.com/pin/123/") for line in lines))
        self.assertTrue(any('param1="rotate" param2="cw"' in line for line in lines))


if __name__ == "__main__":
    unittest.main()
