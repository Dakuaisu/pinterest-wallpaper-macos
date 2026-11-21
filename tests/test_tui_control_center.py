import contextlib
import io
import json
import os
import unittest
from dataclasses import dataclass
from unittest import mock

from test_tui_session import Draft, FakeBackend

import pinterest_wallpaper as pw
from helpers import BOARD, Sandbox, make_listing
from wallpaper_tui import Session
from wallpaper_tui_commands import MENU, Action
from wallpaper_tui_terminal import Size, Terminal


@dataclass(frozen=True)
class Pin:
    key: str
    note: str
    angle: int = 0
    fill: bool = False
    boards: tuple[str, ...] = ()
    favorite: bool = False
    color: str = ""
    showing: bool = False


class ControlBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.items = [Pin("one", "Mountains", boards=("a", "b"), favorite=True),
                      Pin("two", "Coast", boards=("b",))]
        self.jobs = []
        self.result = None
        self.action_busy = False
        self.setting_items = [("mood", '"off"'), ("boards", '["a","b"]'), ("sharpen", "true"), ("max_pins", "250")]
        self.ban_list = ["old-pin"]

    def boards(self):
        return [("a", "Alpine", 1), ("b", "Beach", 2)]

    def bans(self):
        return list(self.ban_list)

    def settings(self):
        return list(self.setting_items)

    def start_action(self, command, args=()):
        self.jobs.append((command, args))
        self.action_busy = True

    def take_action(self):
        result, self.result = self.result, None
        if result is not None:
            self.action_busy = False
        return result

    def close_actions(self):
        self.action_busy = False


class Screen:
    def draw(self, content, image, box, size):
        self.content = content
        self.image = image
        self.box = box


class ControlCase(unittest.TestCase):
    def setUp(self):
        self.backend = ControlBackend()
        self.session = Session(self.backend, Draft)
        self.controls = self.session.controls
        self.addCleanup(self.session.worker.close)

    def keys(self, *keys):
        for key in keys:
            self.session.handle(key)

    def choose(self, label):
        """Open the menu and walk to an entry with the arrow keys, as a person would."""
        self.keys("a", "home")
        target = next(index for index, item in enumerate(MENU) if isinstance(item, Action) and item.label == label)
        while self.controls.cursor != target:
            self.keys("down")
        self.keys("enter")

    def changes(self):
        return [event for event in self.backend.events if event[0] != "preview"]

    def finish(self, success=True, output="done"):
        self.backend.result = (success, output)
        self.session.poll_actions()

    def render(self, columns=80, rows=24):
        screen = Screen()
        self.session.render(screen, Size(columns, rows, 2.0))
        return screen.content


class MenuTests(ControlCase):
    def test_menu_cursor_starts_on_an_action_and_skips_section_titles(self):
        self.keys("a")
        self.assertEqual(MENU[self.controls.cursor].label, "Next wallpaper")
        for _ in range(len(MENU) * 2):
            self.keys("down")
            self.assertIsInstance(MENU[self.controls.cursor], Action)
        self.assertEqual(MENU[self.controls.cursor], MENU[-1])
        for _ in range(len(MENU) * 2):
            self.keys("up")
            self.assertIsInstance(MENU[self.controls.cursor], Action)
        self.assertEqual(MENU[self.controls.cursor].label, "Next wallpaper")

    def test_menu_enter_runs_the_action_and_never_applies_the_selected_pin(self):
        self.choose("Shuffle")
        self.assertEqual(self.backend.jobs, [("shuffle", ())])
        self.assertEqual(self.backend.events, [])
        self.assertEqual(self.controls.mode, "browse")

    def test_single_key_shortcuts_run_the_same_actions(self):
        for key, job in (("n", ("next", ())), ("p", ("prev", ())), ("S", ("shuffle", ())), ("R", ("refresh", ()))):
            with self.subTest(key=key):
                self.backend.jobs.clear()
                self.backend.action_busy = False
                self.keys(key)
                self.assertEqual(self.backend.jobs, [job])

    def test_pause_key_follows_the_current_state(self):
        self.keys("P")
        self.assertEqual(self.backend.jobs, [("pause", ())])
        self.backend.paused = True
        self.finish()
        self.keys("P")
        self.assertEqual(self.backend.jobs[-1], ("resume", ()))

    def test_pause_for_a_while_asks_for_a_duration(self):
        self.choose("Pause for a while…")
        self.assertEqual((self.controls.mode, self.controls.text), ("duration", "30m"))
        self.keys("ctrl-u", "2", "h", "enter")
        self.assertEqual(self.backend.jobs, [("pause", ("2h",))])

    def test_escape_and_q_leave_every_view_without_quitting(self):
        for opener in ("a", "?", ",", "b"):
            for closer in ("esc", "q"):
                with self.subTest(opener=opener, closer=closer):
                    self.keys(opener)
                    self.assertNotEqual(self.controls.mode, "browse")
                    self.assertTrue(self.session.handle(closer))
                    self.assertEqual(self.controls.mode, "browse")

    def test_agent_changes_require_explicit_yes(self):
        for label, command in (("Start changing wallpapers automatically…", "install"),
                               ("Stop changing wallpapers automatically…", "uninstall")):
            with self.subTest(command=command):
                self.choose(label)
                self.assertEqual(self.controls.mode, "confirm")
                self.keys("enter")
                self.assertEqual(self.backend.jobs, [])
                self.assertEqual(self.session.message, "Cancelled")
                self.choose(label)
                self.keys("y")
                self.assertEqual(self.backend.jobs, [(command, ())])
                self.backend.jobs.clear()
                self.backend.action_busy = False

    def test_a_second_action_waits_for_the_first(self):
        self.keys("R", "n")
        self.assertEqual(self.backend.jobs, [("refresh", ())])
        self.assertEqual(self.session.message, "Still busy: Refresh boards")
        self.keys("enter", "s", " ")
        self.assertEqual(self.backend.events, [])


class CompletionTests(ControlCase):
    def test_success_is_reported_inline_by_name(self):
        self.keys("R")
        self.assertIn("Refresh boards…", self.render())
        self.finish(output="Refreshing from Pinterest...")
        self.assertEqual(self.session.message, "Refresh boards: done")
        self.assertEqual(self.controls.mode, "browse")

    def test_one_line_failure_is_reported_inline(self):
        self.keys("n")
        self.finish(False, "No wallpapers available yet")
        self.assertEqual(self.session.message, "Next wallpaper failed: No wallpapers available yet")

    def test_long_failure_and_status_open_the_output(self):
        self.keys("n")
        self.finish(False, "first\nsecond")
        self.assertEqual((self.controls.mode, self.controls.output_snapshot), ("result", ["first", "second"]))
        self.keys("esc", "i")
        self.assertEqual(self.backend.jobs[-1], ("status", ()))
        self.finish(output="Boards : Cats")
        self.assertEqual(self.controls.mode, "result")
        self.assertIn("Boards : Cats", self.render())

    def test_long_output_wraps_and_scrolls(self):
        self.keys("i")
        self.finish(output="\n".join(f"line {number} " + "x" * 90 for number in range(30)))
        self.assertIn("line 0 ", self.render(60, 12))
        self.keys("pgdn", "pgdn", "pgdn")
        content = self.render(60, 12)
        self.assertNotIn("line 0 ", content)
        self.keys("end")
        self.assertIn("line 29 ", self.render(60, 12))

    def test_open_output_keeps_its_snapshot_while_another_action_finishes(self):
        self.keys("i")
        self.finish(output="first")
        self.keys("esc", "n", "a", "end", "up", "enter")
        self.assertEqual(self.controls.output_snapshot, ["first"])
        self.finish(output="second")
        self.assertEqual(self.controls.output_snapshot, ["first"])
        self.assertEqual(self.controls.output, ["second"])

    def test_completed_job_invalidates_cached_preview_and_reloads_pins(self):
        revision = self.session.revision
        self.keys("n")
        self.backend.items = self.backend.items[:1]
        self.finish()
        self.assertEqual(self.session.revision, revision + 1)
        self.assertEqual([pin.key for pin in self.session.pins], ["one"])

    def test_failed_reload_keeps_the_output(self):
        self.keys("n")
        self.backend.failure = "pins"
        self.finish(output="changed")
        self.assertIn("reloading failed: cannot list", self.session.message)
        self.assertEqual(self.controls.output, ["changed"])


class DraftReloadTests(ControlCase):
    def test_completed_job_preserves_original_edited_field_baseline(self):
        self.keys("r")
        self.backend.items[0] = Pin("one", "Mountains", angle=180, boards=("a", "b"), favorite=True)
        self.keys("n")
        self.finish()
        self.assertEqual(self.session.pin.angle, 0)
        self.assertEqual(self.session.draft.angle, 90)

    def test_newly_edited_field_uses_the_value_seen_after_reload(self):
        self.keys("r")
        self.backend.items[0] = Pin("one", "Mountains", fill=True, boards=("a", "b"), favorite=True)
        self.keys("n")
        self.finish()
        self.assertTrue(self.session.pin.fill)

    def test_explicitly_clearing_draft_reveals_latest_external_value(self):
        self.keys("r")
        self.backend.items[0] = Pin("one", "Mountains", angle=180, boards=("a", "b"), favorite=True)
        self.keys("n")
        self.finish()
        self.keys("r", "r", "r")
        self.assertEqual(self.session.draft, Draft())
        self.assertEqual(self.session.pin.angle, 180)


class FilterTests(ControlCase):
    def test_board_picker_filters_without_applying_and_all_boards_restores(self):
        self.keys("b", "down", "enter")
        self.assertEqual([pin.key for pin in self.session.pins], ["one"])
        self.assertIn(" Alpine ", self.render())
        self.keys("b")
        self.assertEqual(self.controls.cursor, 1)
        self.keys("home", "enter")
        self.assertEqual(len(self.session.pins), 2)
        self.assertEqual(self.changes(), [])

    def test_board_picker_escape_keeps_filter_and_draft(self):
        self.keys("r", "b", "down", "down", "enter", "b", "home", "esc")
        self.assertEqual(self.controls.board, "b")
        self.assertEqual(self.session.draft.angle, 90)

    def test_search_filters_as_you_type_and_escape_restores(self):
        self.keys("/", "c", "o")
        self.assertEqual([pin.key for pin in self.session.pins], ["two"])
        self.assertIn("Search: ", self.render())
        self.keys("esc")
        self.assertEqual(len(self.session.pins), 2)
        self.keys("/", "c", "o", "enter")
        self.assertEqual((self.controls.mode, self.controls.search), ("browse", "co"))
        self.assertEqual(self.changes(), [])
        self.assertIn(" “co” ", self.render())

    def test_typing_shortcut_letters_in_search_only_types(self):
        self.keys("/", "q", "n", "x", "?")
        self.assertEqual(self.controls.text, "qnx?")
        self.assertEqual(self.backend.jobs, [])

    def test_filters_combine_and_escape_clears_them_all(self):
        self.keys("*")
        self.assertEqual([pin.key for pin in self.session.pins], ["one"])
        self.keys("b", "down", "down", "enter", "/", "m", "o", "enter")
        self.assertEqual([pin.key for pin in self.session.pins], ["one"])
        self.assertIn(" “mo” · ★ only · Beach ", self.render())
        self.keys("esc")
        self.assertEqual(len(self.session.pins), 2)
        self.assertEqual((self.controls.search, self.controls.board, self.controls.favorites), ("", "", False))

    def test_no_match_explains_how_to_get_back(self):
        self.keys("/", "z", "z", "enter")
        self.assertIn("Nothing matches. Esc shows all.", self.render())


class SettingsTests(ControlCase):
    def test_values_are_shown_without_json_noise(self):
        self.keys(",")
        content = self.render()
        self.assertIn("mood      off", content)
        self.assertIn("sharpen   on", content)
        self.assertIn('boards    ["a","b"]', content)

    def test_enter_flips_a_switch_immediately(self):
        self.keys(",", "down", "down", "enter")
        self.assertEqual(self.backend.jobs, [("config", ("sharpen", "false", "true"))])
        self.assertEqual(self.controls.mode, "settings")

    def test_text_is_edited_without_quotes_and_saved_as_json(self):
        self.keys(",", "enter")
        self.assertEqual((self.controls.mode, self.controls.text), ("edit", "off"))
        self.assertIn("mood: ", self.render())
        self.keys("ctrl-u", *"Night", "enter")
        self.assertEqual(self.backend.jobs, [("config", ("mood", '"Night"', '"off"'))])
        self.assertEqual(self.controls.mode, "settings")

    def test_numbers_and_lists_must_be_valid(self):
        self.keys(",", "end", "enter", "ctrl-u", *"NaN", "enter")
        self.assertEqual(self.controls.mode, "edit")
        self.assertIn("valid value", self.session.message)
        self.assertEqual(self.backend.jobs, [])
        self.keys("ctrl-u", *"100", "enter")
        self.assertEqual(self.backend.jobs, [("config", ("max_pins", "100", "250"))])

    def test_unchanged_value_and_escape_save_nothing(self):
        self.keys(",", "enter", "enter")
        self.keys("enter", "x", "esc")
        self.assertEqual(self.backend.jobs, [])
        self.assertEqual(self.controls.mode, "settings")

    def test_list_refreshes_when_the_save_finishes(self):
        self.keys(",", "down", "down", "enter")
        self.backend.setting_items[2] = ("sharpen", "false")
        self.finish(output="Updated sharpen")
        self.assertEqual(self.controls.mode, "settings")
        self.assertIn("sharpen   off", self.render())
        self.assertEqual(self.session.message, "Change sharpen: done")

    def test_finishing_action_does_not_discard_an_open_edit(self):
        self.keys("n", ",", "enter", "x")
        self.finish()
        self.assertEqual((self.controls.mode, self.controls.text), ("edit", "offx"))


class BanListTests(ControlCase):
    def test_enter_brings_one_back_and_capital_a_asks_first(self):
        self.choose("Banned wallpapers…")
        self.assertIn("old-pin", self.render())
        self.keys("enter")
        self.assertEqual(self.backend.jobs, [("unban", ("old-pin",))])
        self.backend.ban_list = ["old-pin", "other"]
        self.finish()
        self.keys("A")
        self.assertEqual(self.controls.mode, "confirm")
        self.assertIn("all 2", self.controls.prompt)
        self.keys("esc")
        self.assertEqual((self.controls.mode, len(self.backend.jobs)), ("unban", 1))
        self.keys("A", "y")
        self.assertEqual(self.backend.jobs[-1], ("unban", ("all",)))

    def test_empty_ban_list_says_so(self):
        self.backend.ban_list = []
        self.choose("Banned wallpapers…")
        self.assertEqual((self.controls.mode, self.session.message), ("browse", "No banned wallpapers"))


class AddBoardTests(ControlCase):
    LINK = "pinterest.com/me/cats"

    def test_plus_asks_for_a_link_then_saves_and_fetches(self):
        self.keys("+")
        self.assertEqual(self.controls.mode, "board")
        self.assertIn("Paste the board's Pinterest link: ", self.render(100, 24))
        self.keys(*self.LINK, "enter")
        self.assertEqual(self.backend.jobs, [("config", ("boards", f'["old","{self.LINK}"]', '["old"]'))])
        self.assertIn("Add board…", self.render())
        self.finish(output="Updated boards")
        self.assertEqual(self.backend.jobs[-1], ("refresh", ()))
        self.assertIn("Fetch pins from the new board…", self.render())
        self.finish(output="Refreshing")
        self.assertEqual(self.session.message, "Fetch pins from the new board: done")
        self.assertEqual(len(self.backend.jobs), 2)

    def test_failed_save_does_not_fetch(self):
        self.keys("+", *self.LINK, "enter")
        self.finish(False, "configuration changed since it was loaded")
        self.assertEqual(len(self.backend.jobs), 1)
        self.assertIn("Add board failed", self.session.message)

    def test_bad_link_stays_in_the_prompt_and_escape_cancels(self):
        self.keys("+", *"not a link", "enter")
        self.assertEqual(self.controls.mode, "board")
        self.assertIn("isn't a board link", self.session.message)
        self.assertIn("isn't a board link", self.render(100, 24))
        self.keys("esc")
        self.assertEqual((self.controls.mode, self.backend.jobs), ("browse", []))

    def test_menu_and_board_picker_both_offer_it(self):
        self.choose("Add a board…")
        self.assertEqual(self.controls.mode, "board")
        self.keys("esc", "b", "end")
        self.assertIn("+ Add a board…", self.render())
        self.keys("enter")
        self.assertEqual((self.controls.mode, self.controls.board), ("board", ""))


class ManageBoardTests(ControlCase):
    def test_d_switches_a_board_off_and_on_without_leaving_the_list(self):
        self.keys("b", "down", "d")
        self.assertEqual((self.controls.mode, self.backend.off), ("boards", {"a"}))
        self.assertIn("Alpine  (switched off)", self.render())
        self.assertIn("Switched off", self.session.message)
        self.keys("enter")
        self.assertEqual((self.controls.mode, self.controls.board), ("boards", ""))
        self.assertIn("Press d to switch it back on", self.session.message)
        self.keys("d")
        self.assertEqual(self.backend.off, set())
        self.assertIn("Alpine  (1)", self.render())
        self.assertEqual(self.backend.jobs, [])

    def test_switching_off_the_board_being_shown_clears_that_filter(self):
        self.keys("b", "down", "enter", "b", "d")
        self.assertEqual(self.controls.board, "")

    def test_x_removes_only_after_yes_and_then_tidies_up(self):
        self.keys("b", "down", "down", "x")
        self.assertEqual(self.controls.mode, "confirm")
        self.assertIn("Remove Beach and delete its downloaded wallpapers?", self.render(100, 24))
        self.keys("esc")
        self.assertEqual((self.controls.mode, self.backend.jobs), ("boards", []))
        self.keys("x", "y")
        self.assertEqual(self.backend.jobs, [("config", ("boards", '["kept"]', '["kept","b"]'))])
        self.finish(output="Updated boards")
        self.assertEqual(self.backend.jobs[-1], ("refresh", ()))
        self.assertIn("Tidy up after removing the board…", self.render())

    def test_board_filter_survives_a_cancelled_or_failed_removal(self):
        self.keys("b", "down", "down", "enter", "b", "x", "esc")
        self.assertEqual((self.controls.board, [pin.key for pin in self.session.pins]), ("b", ["one", "two"]))
        self.keys("x", "y")
        self.finish(False, "configuration changed since it was loaded")
        self.assertEqual(self.controls.board, "b")
        self.assertEqual(len(self.backend.jobs), 1)

    def test_board_filter_is_dropped_once_the_board_is_really_gone(self):
        self.keys("b", "down", "enter", "b", "x", "y")
        self.assertEqual(self.controls.board, "a")
        self.backend.boards = lambda: [("b", "Beach", 2)]
        self.finish(output="Updated boards")
        self.assertEqual((self.controls.board, len(self.session.pins)), ("", 2))

    def test_the_only_board_cannot_be_removed(self):
        self.backend.boards = lambda: [("only", "Only", 2)]
        self.keys("b", "down", "x")
        self.assertEqual((self.controls.mode, self.backend.jobs), ("boards", []))
        self.assertIn("only board", self.session.message)

    def test_keys_need_a_board_under_the_cursor(self):
        self.keys("b", "d", "x")
        self.assertEqual((self.backend.off, self.backend.jobs, self.controls.mode), (set(), [], "boards"))
        self.assertEqual(self.session.message, "Move to a board first")
        self.keys("+")
        self.assertEqual(self.controls.mode, "board")

    def test_board_keys_are_listed(self):
        self.keys("b")
        content = self.render(100, 24)
        for label in ("Show only this", "Off/on", "Remove", "Add", "Back"):
            self.assertIn(label, content)
        self.assertIn("Back", self.render(36, 10))


class ManageBoardBackendTests(Sandbox):
    OTHER = "https://www.pinterest.com/me/dogs/"

    def setUp(self):
        super().setUp()
        from wallpaper_tui_backend import Backend

        self.config["boards"].append(self.OTHER)
        pw.save_json(pw.CONFIG_FILE, self.config)
        self.add_pin("cat")
        self.add_pin("dog")
        self.add_pin("both")
        with pw.locked_state() as state:
            state["pins"]["dog"]["boards"] = [self.OTHER]
            state["pins"]["both"]["boards"] = [BOARD, self.OTHER]
        self.backend = Backend(pw, self.config)

    def test_switching_off_hides_only_pins_that_live_solely_on_that_board(self):
        self.assertTrue(self.backend.toggle_board(self.OTHER))
        self.assertEqual(self.backend.disabled_boards(), {self.OTHER})
        self.assertEqual([pin.key for pin in self.backend.pins()], ["cat", "both"])
        self.assertEqual(pw.available_pins(self.state()), ["cat", "both"])
        self.assertTrue(os.path.exists(os.path.join(pw.IMAGE_DIR, "dog.jpg")))
        self.assertFalse(self.backend.toggle_board(self.OTHER))
        self.assertEqual(len(self.backend.pins()), 3)

    def test_switched_off_board_is_not_fetched_and_keeps_its_pins_and_images(self):
        self.backend.toggle_board(self.OTHER)
        fetched = []

        def fetch(url, max_pins, previous=None):
            fetched.append(url)
            return make_listing(["cat", "both"], url=url)

        with mock.patch.object(pw, "fetch_board", side_effect=fetch):
            self.assertTrue(pw.do_refresh(self.config))
        self.assertEqual(fetched, [BOARD])
        state = self.state()
        self.assertEqual(sorted(state["pins"]), ["both", "cat", "dog"])
        self.assertEqual(state["disabled_boards"], [self.OTHER])
        self.assertTrue(os.path.exists(os.path.join(pw.IMAGE_DIR, "dog.jpg")))
        self.assertEqual(sorted(pw.available_pins(state)), ["both", "cat"])
        self.assertEqual(state["pins"]["both"]["boards"], sorted([BOARD, self.OTHER]))

    def test_rotation_never_picks_from_a_switched_off_board(self):
        self.backend.toggle_board(BOARD)
        with pw.locked_state() as state:
            for _ in range(6):
                self.assertTrue(pw.advance(self.config, state))
                self.assertIn(pw.current_pins(state)[0], ("dog", "both"))

    def test_removed_board_is_forgotten_from_the_switched_off_list(self):
        self.backend.toggle_board(self.OTHER)
        key, new, old = self.backend.remove_board(self.OTHER)
        self.assertEqual((key, json.loads(new), json.loads(old)), ("boards", [BOARD], [BOARD, self.OTHER]))
        self.config["boards"] = [BOARD]
        with mock.patch.object(pw, "fetch_board", return_value=make_listing(["cat", "both"])):
            pw.do_refresh(self.config)
        state = self.state()
        self.assertEqual((state["disabled_boards"], sorted(state["pins"])), ([], ["both", "cat"]))
        self.assertFalse(os.path.exists(os.path.join(pw.IMAGE_DIR, "dog.jpg")))

    def test_only_board_and_unknown_board_are_refused(self):
        from wallpaper_tui_backend import TuiError

        with self.assertRaises(TuiError):
            self.backend.remove_board("https://www.pinterest.com/me/unknown/")
        with self.assertRaises(TuiError):
            self.backend.toggle_board("https://www.pinterest.com/me/unknown/")
        self.config["boards"] = [BOARD]
        pw.save_json(pw.CONFIG_FILE, self.config)
        with self.assertRaisesRegex(TuiError, "only board"):
            self.backend.remove_board(BOARD)

    def test_state_keeps_only_real_board_links(self):
        with open(pw.STATE_FILE) as handle:
            raw = json.load(handle)
        raw["disabled_boards"] = [self.OTHER, self.OTHER, "https://evil.example/a/b/", 5]
        with open(pw.STATE_FILE, "w") as handle:
            json.dump(raw, handle)
        self.assertEqual(pw.load_state()["disabled_boards"], [self.OTHER])

    def test_status_marks_the_switched_off_board(self):
        self.backend.toggle_board(self.OTHER)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.object(pw, "agent_summary", return_value="off"):
            pw.cmd_status(self.config, [])
        self.assertIn(f"{self.OTHER}  (switched off)", output.getvalue())


class AddBoardBackendTests(Sandbox):
    def setUp(self):
        super().setUp()
        from wallpaper_tui_backend import Backend

        pw.save_json(pw.CONFIG_FILE, self.config)
        self.backend = Backend(pw, self.config)

    def boards(self, text):
        key, new, old = self.backend.add_board(text)
        self.assertEqual(key, "boards")
        return json.loads(new), json.loads(old)

    def test_link_is_normalised_and_appended(self):
        current = self.config["boards"]
        for text in ("pinterest.com/me/dogs", "  https://www.pinterest.com/me/dogs/?utm=1#top ",
                     "https://in.pinterest.com/me/dogs"):
            with self.subTest(text=text):
                new, old = self.boards(text)
                self.assertEqual(old, current)
                self.assertEqual(new[:-1], current)
                self.assertRegex(new[-1], r"^https://(www\.|in\.)?pinterest\.com/me/dogs/$")

    def test_placeholder_board_is_replaced(self):
        self.config["boards"] = [f"https://www.pinterest.com/{pw.PLACEHOLDER_BOARD}/board/"]
        pw.save_json(pw.CONFIG_FILE, self.config)
        new, _ = self.boards("pinterest.com/me/dogs")
        self.assertEqual(new, ["https://pinterest.com/me/dogs/"])

    def test_rejects_duplicates_and_things_that_are_not_boards(self):
        from wallpaper_tui_backend import TuiError

        for text in (self.config["boards"][0].rstrip("/"), "https://evil.example/me/dogs", "pinterest.com/me",
                     "http://pinterest.com/me/dogs", "", "https://user@pinterest.com/me/dogs", "https://[bad/me/dogs"):
            with self.subTest(text=text), self.assertRaises(TuiError):
                self.backend.add_board(text)

    def test_change_is_accepted_by_the_config_editor(self):
        from wallpaper_tui_actions import edit_config

        change = self.backend.add_board("pinterest.com/me/dogs")
        edit_config(pw, change)
        self.assertEqual(pw.load_config(create=False)[0]["boards"][-1], "https://pinterest.com/me/dogs/")


class InputDecodingTests(unittest.TestCase):
    def test_text_entry_preserves_case_and_punctuation(self):
        terminal = Terminal.__new__(Terminal)
        terminal.pending = bytearray(b'"Night"')
        self.assertEqual("".join(terminal._next_key() for _ in range(7)), '"Night"')

    def test_control_and_unicode_keys_are_decoded(self):
        terminal = Terminal.__new__(Terminal)
        terminal.pending = bytearray(b"\t\x15\x7f" + "山".encode())
        self.assertEqual([terminal._next_key() for _ in range(4)], ["tab", "ctrl-u", "backspace", "山"])


class DraftConflictIntegrationTests(Sandbox):
    def test_reload_then_save_rejects_external_rotation_change(self):
        from wallpaper_tui_backend import Backend
        from wallpaper_tui_backend import Draft as BackendDraft

        self.add_pin("one", size=(8, 4))
        backend = Backend(pw, self.config)
        session = Session(backend, BackendDraft)
        self.addCleanup(session.worker.close)
        session.handle("r")
        with pw.locked_state() as state:
            state["rotations"]["one"] = 180
        with mock.patch.object(backend, "take_action", return_value=(True, "done")):
            session.poll_actions()
        session.handle("s")
        self.assertIn("changed since", session.message)
        self.assertEqual(self.state()["rotations"]["one"], 180)


if __name__ == "__main__":
    unittest.main()
