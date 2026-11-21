import unittest

from PIL import Image
from test_tui_control_center import ControlCase, Pin, Screen

import pinterest_wallpaper as pw
from helpers import Sandbox
from wallpaper_tui import Session
from wallpaper_tui_backend import Backend
from wallpaper_tui_backend import Draft as BackendDraft
from wallpaper_tui_terminal import Size
from wallpaper_tui_view import fit_hints, wrap_prose


class PixelScreen(Screen):
    def preview_bounds(self, box, size):
        self.requested_box = box
        return (901, 607)


class LibraryLayoutTests(ControlCase):
    def screen(self, columns, rows, screen=None):
        screen = screen or Screen()
        self.session.render(screen, Size(columns, rows, 2.0))
        return screen

    def test_rows_are_numbered_labelled_and_flagged(self):
        self.backend.items = [Pin("opaque-id-1", "1920x1080 source", favorite=True, color="#336699"),
                              Pin("opaque-id-2", "800x600 source", showing=True),
                              Pin("opaque-id-3", "")]
        self.session.reload()
        self.keys("down", "r")
        content = self.render()
        self.assertIn(" 1    1920×1080", content)
        self.assertIn("38;2;51;102;153m██", content)
        self.assertIn("~● ", content)
        self.assertIn("…que-id-3", content)
        self.assertNotIn("opaque-id-1", content)
        self.assertIn(" Preview · on screen now ", content)
        self.assertIn(" 800×600 · turned 90° · not saved ", content)

    def test_position_and_total_are_always_visible(self):
        self.assertIn(" Wallpapers · 1 of 2 ", self.render(100, 30))
        self.assertIn("Rotation on · 2 wallpapers", self.render(100, 30))
        self.keys("down")
        self.assertIn(" 2 of 2 ", self.render(36, 10))

    def test_paused_state_is_shown_in_the_header(self):
        self.backend.paused = True
        self.session.reload()
        self.assertIn("Paused · 2 wallpapers", self.render())

    def test_essential_keys_survive_every_supported_width(self):
        for columns in (36, 60, 80, 120):
            with self.subTest(columns=columns):
                content = self.render(columns, 10)
                for label in ("More", "Help", "Quit"):
                    self.assertIn(label, content)
        self.assertIn("Set wallpaper", self.render(80, 24))

    def test_hints_drop_whole_entries_never_half_a_label(self):
        hints = (("Enter", "Set wallpaper"), ("Space", "Favorite"), ("r", "Rotate"))
        self.assertEqual(fit_hints(hints, (("q", "Quit"),), 30), [("Enter", "Set wallpaper"), ("q", "Quit")])
        self.assertEqual(fit_hints(hints, (("q", "Quit"),), 10), [("q", "Quit")])

    def test_wide_window_gives_the_image_most_of_the_room(self):
        self.session.preview((42, 34))
        self.assertTrue(self.session.worker.ready.wait(2))
        box = self.screen(120, 30).box
        self.assertGreaterEqual(box.columns, 76)
        self.assertEqual(box.rows, 25)

    def test_minimum_window_still_shows_the_image(self):
        self.session.preview((18, 10))
        self.assertTrue(self.session.worker.ready.wait(2))
        box = self.screen(36, 10).box
        self.assertEqual((box.columns, box.rows), (18, 5))

    def test_preview_uses_terminal_reported_pixel_bounds(self):
        screen = PixelScreen()
        self.backend.preview_task = lambda pin, draft, original, bounds: (
            self.backend.events.append(("bounds", bounds)) or (lambda: Image.new("RGB", (8, 4))))
        self.screen(100, 30, screen)
        self.assertIn(("bounds", (901, 607)), self.backend.events)
        self.assertEqual((screen.requested_box.row, screen.requested_box.rows), (3, 25))

    def test_overlays_hide_the_image(self):
        self.session.preview((50, 38))
        self.assertTrue(self.session.worker.ready.wait(2))
        self.assertIsNotNone(self.screen(80, 24).image)
        for key in ("a", "?", ",", "b"):
            with self.subTest(key=key):
                self.keys(key)
                self.assertIsNone(self.screen(80, 24).image)
                self.keys("esc")
        self.assertIsNotNone(self.screen(80, 24).image)

    def test_empty_library_says_what_to_do(self):
        self.backend.items = []
        self.session.reload()
        self.assertIn("No wallpapers yet. Press + to add a board, or R to fetch the ones you have.", self.render(120, 24))

    def test_too_small_window_says_how_big_it_needs_to_be(self):
        self.assertIn("q quit | Resize 36x10", self.render(35, 10))
        self.assertIn("q quit | Resize 36x10", self.render(36, 9))

    def test_error_and_caption_are_visible_together(self):
        self.backend.failure = "save"
        self.keys("r", "s")
        content = self.render()
        self.assertIn("cannot save", content)
        self.assertIn(" Mountains · turned 90° · not saved ", content)

    def test_help_lists_every_browse_key_and_scrolls(self):
        self.keys("?")
        content = self.render(80, 40)
        for text in ("Put it on your desktop", "Add to or remove from favorites", "Ban: never show it again",
                     "More actions", "★ favorite"):
            self.assertIn(text, content)
        self.keys("end")
        self.assertIn("★ favorite", self.render(80, 12))

    def test_menu_shows_shortcut_keys_next_to_actions(self):
        self.keys("a")
        content = self.render(100, 30)
        self.assertIn("  Next wallpaper", content)
        self.assertIn(" More actions ", content)
        self.assertRegex(content, r"\x1b\[4;47H[^H]*n\x1b\[0m")

    def test_word_wrapping_preserves_spaces(self):
        self.assertEqual(wrap_prose("Ban the wallpaper showing now", 12), ["Ban the ", "wallpaper ", "showing now"])


class LibraryActionTests(ControlCase):
    def test_x_bans_the_selected_wallpaper_only_after_yes(self):
        self.keys("down", "x")
        self.assertEqual(self.controls.mode, "confirm")
        self.assertIn("Ban this wallpaper", self.render())
        self.assertIn("Yes", self.render())
        for cancel in ("enter", "esc", "n", "q"):
            with self.subTest(cancel=cancel):
                self.keys(cancel)
                self.assertEqual((self.controls.mode, self.session.message), ("browse", "Cancelled"))
                self.keys("x")
        self.keys("x", "r", " ")
        self.assertEqual((self.backend.banned, self.backend.jobs, self.changes()), ([], [], []))
        self.keys("y")
        self.assertEqual(self.backend.banned, ["two"])
        self.assertEqual([pin.key for pin in self.session.pins], ["one"])
        self.assertIn("Banned", self.session.message)

    def test_failed_favorite_is_reported(self):
        self.backend.failure = "favorite"
        self.keys(" ")
        self.assertEqual(self.session.message, "cannot favorite")


class RealBackendLibraryTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.add_pin("first", size=(80, 40), color="red")
        self.add_pin("second", size=(40, 80), color="blue")
        with pw.locked_state() as state:
            state["current"] = ["second"]
            state["deck"] = ["first", "second"]
        self.session = Session(Backend(pw, self.config), BackendDraft)
        self.addCleanup(self.session.worker.close)

    def test_pins_carry_colour_and_on_screen_state(self):
        first, second = self.session.pins
        self.assertEqual((first.color, first.showing), ("#ff0000", False))
        self.assertTrue(second.showing)
        screen = Screen()
        self.session.render(screen, Size(80, 24, 2.0))
        self.assertIn("80×40", screen.content)
        self.assertNotIn("scaled", screen.content.lower())

    def test_space_and_x_change_state_under_the_lock(self):
        self.session.handle(" ")
        self.assertEqual(self.state()["favorites"], ["first"])
        self.session.handle(" ")
        self.assertEqual(self.state()["favorites"], [])
        self.session.handle("x")
        self.session.handle("y")
        state = self.state()
        self.assertEqual((state["banned"], state["deck"]), (["first"], ["second"]))
        self.assertEqual([pin.key for pin in self.session.pins], ["second"])

    def test_favoriting_a_wallpaper_a_refresh_removed_reports_it_and_reloads(self):
        with pw.locked_state() as state:
            del state["pins"]["first"]
        self.session.handle(" ")
        self.assertEqual(self.state()["favorites"], [])
        self.assertEqual(self.session.message, "That wallpaper is no longer in your library")
        self.assertEqual([pin.key for pin in self.session.pins], ["second"])

    def test_overview_reports_pause_without_the_epoch_date(self):
        self.assertEqual(self.session.status, "Rotation on")
        with pw.locked_state() as state:
            state["paused_until"] = -1
        self.session.reload()
        self.assertEqual((self.session.paused, self.session.status), (True, "Paused until resumed"))


if __name__ == "__main__":
    unittest.main()
