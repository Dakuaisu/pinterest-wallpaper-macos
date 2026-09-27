import json
import os
import unittest
from unittest import mock

import requests

from helpers import (
    BOARD, SCREEN, FakeResponse, Sandbox, board_page, feed_pin, image_bytes, image_session,
    make_listing, make_pin, pw, rss_item,
)


def api(pins, bookmark=None):

    return FakeResponse(body={"resource_response": {"data": pins, "bookmark": bookmark}})


def orig(key):

    return f"https://i.pinimg.com/originals/{key}.jpg"


class FeedTests(Sandbox):

    def test_missing_initial_props_is_an_error(self):

        with self.assertRaisesRegex(RuntimeError, "__PWS_INITIAL_PROPS__"):
            pw.parse_initial_props("<html></html>")

    def test_pin_fields(self):

        item = feed_pin("5", orig("5"), width=800, height=600, videos={"video_list": {}})

        self.assertEqual(pw.pin_from_feed(item, BOARD), {
            "key": "5", "url": orig("5"), "board": BOARD, "color": "#112233",
            "width": 800, "height": 600, "video": True,
        })

    def test_falls_back_to_smaller_sizes_and_plain_strings(self):

        item = {"type": "pin", "id": "5", "images": {"736x": "https://i.pinimg.com/736x/5.jpg"}}
        pin = pw.pin_from_feed(item, BOARD)

        self.assertEqual((pin["url"], pin["width"], pin["video"]), ("https://i.pinimg.com/736x/5.jpg", 0, False))

    def test_rejects_non_pins_missing_images_and_plain_http(self):

        self.assertIsNone(pw.pin_from_feed("nope", BOARD))
        self.assertIsNone(pw.pin_from_feed({"type": "pin", "id": "5", "images": []}, BOARD))
        self.assertIsNone(pw.pin_from_feed({"type": "pin", "id": "5", "images": {"orig": {"url": "http://i.pinimg.com/x.jpg"}}}, BOARD))

    def test_odd_sizes_are_ignored(self):

        item = feed_pin("5", orig("5"))
        item["images"]["orig"].update(width=True, height="600")

        pin = pw.pin_from_feed(item, BOARD)
        self.assertEqual((pin["width"], pin["height"]), (0, 0))

    def test_dedupe_by_key_and_url(self):

        pins = [make_pin("1"), make_pin("1"), dict(make_pin("2"), url=orig("1")), make_pin("3")]

        self.assertEqual([p["key"] for p in pw.dedupe(pins)], ["1", "3"])


class BoardTests(Sandbox):

    def serve(self, page, api_pages=()):

        calls = []
        responses = list(api_pages)

        def get(url, **kwargs):
            calls.append(url)
            return page if url == BOARD else responses.pop(0)

        return mock.patch.object(pw.SESSION, "get", get), calls

    def first_listing(self):

        page = FakeResponse(board_page([feed_pin("1", orig("1"))], bookmark="b1", pin_count=2))
        patcher, calls = self.serve(page, [api([feed_pin("2", orig("2"))])])

        with patcher:
            listing = pw.scrape_board(BOARD, 250)

        return listing, calls

    def test_board_without_a_feed_is_an_error(self):

        patcher, _ = self.serve(FakeResponse(board_page([])))

        with patcher, self.assertRaisesRegex(RuntimeError, "no pins"):
            pw.scrape_board(BOARD, 250)

    def test_board_names_are_cleaned(self):

        page = board_page([feed_pin("1", orig("1"))]).replace('"Cats"', '"Cats\\n| evil"')
        patcher, _ = self.serve(FakeResponse(page))

        with patcher:
            self.assertEqual(pw.scrape_board(BOARD, 250)["name"], "Cats | evil")

    def test_unchanged_board_reuses_its_stored_listing(self):

        first, calls = self.first_listing()

        self.assertEqual((first["paged"], first["reused"], len(calls)), (True, False, 2))

        previous = {"stamp": first["stamp"], "pins": first["pins"]}
        page = FakeResponse(board_page([feed_pin("1", orig("1"))], bookmark="b1", pin_count=2))
        patcher, calls = self.serve(page)

        with patcher:
            second = pw.scrape_board(BOARD, 250, previous)

        self.assertTrue(second["reused"])
        self.assertEqual(calls, [BOARD])
        self.assertEqual([p["key"] for p in second["pins"]], ["1", "2"])

    def test_any_change_pages_again(self):

        first, _ = self.first_listing()
        previous = {"stamp": first["stamp"], "pins": first["pins"]}
        first_page = [feed_pin("1", orig("1"))]

        for items, extra in (
            (first_page, {"pin_count": 3}),
            (first_page, {"modified": "t2"}),
            ([feed_pin("0", orig("0"))] + first_page, {}),
        ):
            with self.subTest(extra=extra, items=len(items)):
                page = FakeResponse(board_page(items, bookmark="b1", **dict({"pin_count": 2}, **extra)))
                patcher, calls = self.serve(page, [api([])])

                with patcher:
                    self.assertFalse(pw.scrape_board(BOARD, 250, previous)["reused"])

                self.assertEqual(len(calls), 2)

    def test_unreadable_feed_options_leave_the_listing_partial(self):

        self.assertFalse(pw.paginate(BOARD, "not json", "b1", [], 250))
        pw.log_error.assert_called_once()

    def test_rss_without_pins_is_an_error(self):

        with mock.patch.object(pw.SESSION, "get", return_value=FakeResponse("<rss><channel><title>x</title></channel></rss>")):
            with self.assertRaisesRegex(RuntimeError, "no pins"):
                pw.scrape_board_rss(BOARD, 250)

    def test_rss_pins_match_the_page_pin_shape(self):

        rss = f"<rss><channel><title>C</title>{rss_item('9', 'https://i.pinimg.com/236x/ab/cd/ef.png')}</channel></rss>"

        with mock.patch.object(pw.SESSION, "get", return_value=FakeResponse(rss)):
            listing = pw.scrape_board_rss(BOARD, 250)

        pin = listing["pins"][0]

        self.assertEqual(set(pin) - {"fallbacks"}, set(make_pin("9")))
        self.assertEqual(pin["url"], "https://i.pinimg.com/originals/ab/cd/ef.png")
        self.assertEqual(pin["fallbacks"], [
            "https://i.pinimg.com/originals/ab/cd/ef.gif",
            "https://i.pinimg.com/originals/ab/cd/ef.jpg",
            "https://i.pinimg.com/736x/ab/cd/ef.png",
        ])
        self.assertEqual((listing["paged"], listing["reused"], listing["stamp"]), (False, False, None))

    def test_page_errors_fall_back_to_rss(self):

        rss = f"<rss><channel><title>C</title>{rss_item('9', 'https://i.pinimg.com/236x/ab/cd/ef.jpg')}</channel></rss>"

        def get(url, **kwargs):
            return FakeResponse("<html>redesigned</html>") if url == BOARD else FakeResponse(rss)

        with mock.patch.object(pw.SESSION, "get", get):
            listing = pw.fetch_board(BOARD, 250)

        self.assertFalse(listing["complete"])
        self.assertIn("falling back to RSS", pw.log_error.call_args[0][0])

    def test_rss_network_error_means_offline(self):

        def get(url, **kwargs):
            if url == BOARD:
                return FakeResponse("<html>redesigned</html>")
            raise requests.Timeout("slow")

        with mock.patch.object(pw.SESSION, "get", get), self.assertRaises(pw.Offline):
            pw.fetch_board(BOARD, 250)


class DownloadTests(Sandbox):

    def test_rejects_non_images_and_tiny_images(self):

        for session, message in (
            (image_session(b"<html>", "text/html"), "not an image"),
            (image_session(b"", ""), "no content type"),
            (image_session(image_bytes((100, 100))), "too small"),
        ):
            with self.subTest(message=message), mock.patch.object(pw, "worker_session", return_value=session):
                with self.assertRaisesRegex(ValueError, message):
                    pw.download_source(make_pin("1"))

    def test_png_is_stored_with_its_own_extension(self):

        with mock.patch.object(pw, "worker_session", return_value=image_session(image_bytes(fmt="PNG"), "image/png")):
            filename, _ = pw.download_source(make_pin("1"))

        self.assertEqual(filename, "1.png")

    def test_when_every_url_is_refused_the_last_error_is_raised(self):

        refused = mock.Mock(get=lambda url, **kwargs: FakeResponse(status=403))

        with mock.patch.object(pw, "worker_session", return_value=refused):
            with self.assertRaises(requests.HTTPError):
                pw.download_source(dict(make_pin("1"), fallbacks=["https://i.pinimg.com/736x/1.jpg"]))

    def test_try_download_returns_the_error(self):

        with mock.patch.object(pw, "download_source", side_effect=ValueError("bad")):
            result, error = pw.try_download(make_pin("1"))

        self.assertIsNone(result)
        self.assertIsInstance(error, ValueError)


class SyncTests(Sandbox):

    def refresh(self, listing, session=None):

        with mock.patch.object(pw, "worker_session", return_value=session or image_session()), \
                mock.patch.object(pw, "fetch_board", return_value=listing):
            return pw.do_refresh(self.config)

    def big_screen(self):

        with pw.locked_state() as state:
            state["screens"] = [{"id": 1, "name": "Big", "width": 3000, "height": 2000}]

    def logged(self):

        return [call.args[0] for call in pw.log.call_args_list]

    def test_video_pins_are_skipped_by_default_but_can_be_kept(self):

        listing = make_listing(["1"])
        listing["pins"].append(make_pin("2", video=True))

        self.refresh(listing)
        self.assertEqual(sorted(self.state()["pins"]), ["1"])
        self.assertIn("Skipped 1 video pin(s).", self.logged())

        self.config["skip_video"] = False
        self.refresh(listing)
        self.assertEqual(sorted(self.state()["pins"]), ["1", "2"])

    def test_low_resolution_pins_are_skipped_before_download_when_size_is_known(self):

        self.config["skip_upscale_over"] = 3
        self.big_screen()

        listing = make_listing([])
        listing["pins"] = [make_pin("small", width=500, height=300), make_pin("big", width=1600, height=1000)]
        requested = []

        def get(url, **kwargs):
            requested.append(url)
            return FakeResponse(content=image_bytes((1600, 1000)), content_type="image/jpeg")

        self.refresh(listing, mock.Mock(get=get))

        self.assertEqual(sorted(self.state()["pins"]), ["big"])
        self.assertEqual(requested, [orig("big")])
        self.assertTrue(any("Skipped 1 low-resolution" in line for line in self.logged()))

    def test_low_resolution_rss_pins_are_skipped_after_download(self):

        self.config["skip_upscale_over"] = 3
        self.big_screen()

        self.refresh(make_listing(["small"], complete=False), image_session(image_bytes((400, 300))))

        self.assertEqual(self.state()["pins"], {})

    def test_skip_upscale_over_off_keeps_everything(self):

        self.big_screen()
        self.refresh(make_listing(["small"], width=500, height=300), image_session(image_bytes((500, 300))))

        self.assertEqual(sorted(self.state()["pins"]), ["small"])

    def test_cached_images_reuse_stored_sizes_and_fill_in_missing_ones(self):

        self.add_pin("1")
        self.add_pin("2")

        with pw.locked_state() as state:
            del state["pins"]["2"]["luma"]

        real_decode = pw.decode
        decoded = []

        def spy(source):
            decoded.append(os.path.basename(source))
            return real_decode(source)

        with mock.patch.object(pw, "decode", spy):
            self.refresh(make_listing(["1", "2"]))

        self.assertEqual(decoded, ["2.jpg"])
        self.assertIn("luma", self.state()["pins"]["2"])

    def test_unreadable_cached_image_is_dropped_for_redownload(self):

        with open(os.path.join(pw.IMAGE_DIR, "1.jpg"), "wb") as handle:
            handle.write(b"garbage")

        self.refresh(make_listing(["1"]))

        self.assertNotIn("1", self.state()["pins"])
        self.assertEqual(os.listdir(pw.IMAGE_DIR), [])
        self.assertIn("unreadable", pw.log_error.call_args[0][0])

    def test_download_failures_are_logged_per_pin(self):

        failing = mock.Mock(get=lambda url, **kwargs: FakeResponse(status=404))

        self.refresh(make_listing(["1"]), failing)

        self.assertIn("Pin 1: download failed", pw.log_error.call_args[0][0])
        self.assertIn("Downloaded 0/1 new image(s).", self.logged())

    def test_offline_boards_only_raise_when_nothing_else_happened(self):

        other = "https://www.pinterest.com/someone/dogs/"
        self.config["boards"] = [BOARD, other]

        def fetch(url, *rest):
            if url == BOARD:
                raise pw.Offline("dns")
            raise RuntimeError("HTTP 404")

        with mock.patch.object(pw, "fetch_board", fetch):
            result = pw.sync_boards(self.config, self.state())

        self.assertEqual(result["offline"], [BOARD])
        self.assertIn(other, result["failures"])

    def test_placeholder_board_is_refused_without_network(self):

        self.config["boards"] = list(pw.DEFAULT_CONFIG["boards"])

        with mock.patch.object(pw, "fetch_board") as fetch:
            self.assertFalse(pw.do_refresh(self.config))

        fetch.assert_not_called()
        self.assertIn("placeholder", pw.log_error.call_args[0][0])

    def test_cmd_refresh_exit_codes(self):

        with mock.patch.object(pw, "do_refresh", return_value=True):
            self.assertEqual(pw.cmd_refresh(self.config, []), 0)

        with mock.patch.object(pw, "do_refresh", return_value=False):
            self.assertEqual(pw.cmd_refresh(self.config, []), 1)

    def test_second_refresh_of_an_unchanged_board_skips_paging(self):

        calls = []

        def get(url, **kwargs):
            calls.append(url)
            if url == BOARD:
                return FakeResponse(board_page([feed_pin("1", orig("1"))], bookmark="b1", pin_count=2))
            return api([feed_pin("2", orig("2"))])

        with mock.patch.object(pw.SESSION, "get", get), \
                mock.patch.object(pw, "worker_session", return_value=image_session()):
            pw.do_refresh(self.config)
            self.assertEqual(len(calls), 2)
            self.assertIn(BOARD, self.state()["listings"])

            calls.clear()
            pw.do_refresh(self.config)

        self.assertEqual(calls, [BOARD])
        self.assertEqual(sorted(self.state()["pins"]), ["1", "2"])
        self.assertTrue(any("unchanged since the last refresh" in line for line in self.logged()))

    def test_listings_and_failures_of_removed_boards_are_forgotten(self):

        gone = "https://www.pinterest.com/someone/gone/"

        with pw.locked_state() as state:
            state["listings"][gone] = {"stamp": {}, "pins": []}
            state["board_fails"][gone] = {"count": 2, "error": "x"}

        self.refresh(make_listing(["1"]))

        state = self.state()
        self.assertNotIn(gone, state["listings"])
        self.assertNotIn(gone, state["board_fails"])

    def test_refresh_keeps_renders_only_for_recent_and_upcoming_pins(self):

        keys = [str(i) for i in range(10)]

        for key in keys:
            self.add_pin(key)

        with pw.locked_state() as state:
            state["screens"] = [dict(SCREEN)]
            state["history"] = [["0"], ["1"]]
            state["deck"] = ["2", "3"]
            files = pw.source_files()
            for key in keys:
                pw.ensure_render(key, (160, 100), self.config, state, files)

        self.assertEqual(len(os.listdir(pw.WALLPAPERS_DIR)), 10)

        self.refresh(make_listing(keys))

        self.assertEqual(sorted(n.split(".")[0] for n in os.listdir(pw.WALLPAPERS_DIR)), ["0", "1", "2", "3"])


class UpscalerTests(Sandbox):

    def setUp(self):

        super().setUp()

        with pw.locked_state() as state:
            state["screens"] = [dict(SCREEN)]

    def write_stub(self, body):

        stub = os.path.join(self.root, "stub")

        with open(stub, "w") as handle:
            handle.write(f"#!/bin/bash\n{body}\n")

        os.chmod(stub, 0o755)
        return stub

    def test_non_executable_upscaler_is_reported(self):

        path = os.path.join(self.root, "not-executable")
        open(path, "w").close()
        self.config["upscaler"] = path

        with mock.patch.object(pw, "run_upscaler") as run:
            pw.upscale_sources(self.config, self.state(), pw.source_files(), 5)

        run.assert_not_called()
        pw.log_error.assert_called_once()

    def test_only_unbanned_pins_that_need_real_enlargement_are_upscaled(self):

        self.config["upscaler"] = self.make_upscaler()
        self.add_pin("tiny", size=(40, 25))
        self.add_pin("banned", size=(40, 25))
        self.add_pin("close", size=(120, 75))

        with pw.locked_state() as state:
            state["banned"] = ["banned"]

        with mock.patch.object(pw, "run_upscaler") as run:
            pw.upscale_sources(self.config, self.state(), pw.source_files(), 5)

        self.assertEqual([os.path.basename(call.args[2]) for call in run.call_args_list], ["tiny.jpg"])

    def test_limit_caps_attempts_per_call(self):

        self.config["upscaler"] = self.make_upscaler()

        for key in "abcd":
            self.add_pin(key, size=(40, 25))

        with mock.patch.object(pw, "run_upscaler") as run:
            pw.upscale_sources(self.config, self.state(), pw.source_files(), 3)

        self.assertEqual(run.call_count, 3)

    def test_run_upscaler_passes_model_scale_and_working_directory(self):

        src = os.path.join(pw.IMAGE_DIR, "s.jpg")
        out = os.path.join(pw.UPSCALED_DIR, "s.jpg")
        args_file = os.path.join(self.root, "args.txt")

        with open(src, "wb") as handle:
            handle.write(image_bytes())

        pw.run_upscaler(self.write_stub(f'echo "$PWD $@" > "{args_file}"; cp "$2" "$4"'), "my-model", src, out)

        with open(args_file) as handle:
            recorded = handle.read()

        self.assertTrue(recorded.startswith(os.path.realpath(self.root)))
        self.assertIn("-n my-model -s 4", recorded)
        self.assertTrue(os.path.exists(out))

    def test_run_upscaler_errors(self):

        src = os.path.join(pw.IMAGE_DIR, "s.jpg")
        out = os.path.join(pw.UPSCALED_DIR, "s.jpg")

        with open(src, "wb") as handle:
            handle.write(image_bytes())

        with self.assertRaisesRegex(RuntimeError, "bad model"):
            pw.run_upscaler(self.write_stub("echo 'bad model' >&2; exit 1"), "m", src, out)

        with self.assertRaisesRegex(RuntimeError, "exit status 0"):
            pw.run_upscaler(self.write_stub("exit 0"), "m", src, out)

        self.assertFalse(os.path.exists(out))


if __name__ == "__main__":
    unittest.main()
