from __future__ import annotations

import json
from typing import TYPE_CHECKING

from wallpaper_tui_commands import HELP_TEXT, MENU, SHORTCUTS, Action

if TYPE_CHECKING:
    from wallpaper_tui import Session

INPUT_MODES = ("search", "edit", "duration", "display", "board")
LIST_MODES = ("menu", "boards", "settings", "unban", "result", "help")
PAGE = 10
ADD_ROW = "+ Add a board…"


class Controls:
    """Own which view is open, its cursor, and the text being typed."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.mode = "browse"
        self.cursor = 0
        self.text = ""
        self.search = ""
        self.search_before = ""
        self.board = ""
        self.favorites = False
        self.setting_key = ""
        self.setting_expected = ""
        self.setting_is_text = False
        self.pending: tuple[str, tuple[str, ...]] | None = None
        self.pending_label = ""
        self.prompt = ""
        self.return_mode = "browse"
        self.action: Action | None = None
        self.running = ""
        self.show_output = False
        self.then: tuple[str, tuple[str, ...], str] | None = None
        self.output: list[str] = []
        self.output_snapshot: list[str] = []
        self.output_rows: list[str] = []
        self.board_items: list[tuple[str, str, int]] = []
        self.boards_off: set[str] = set()
        self.pending_then: tuple[str, tuple[str, ...], str] | None = None
        self.setting_items: list[tuple[str, str]] = []
        self.ban_items: list[str] = []

    @property
    def view(self) -> str:
        """The list that is on screen, including behind a prompt or confirmation."""
        if self.mode == "edit":
            return "settings"
        return self.return_mode if self.mode == "confirm" else self.mode

    def rows(self) -> list[str]:
        mode = self.view
        if mode == "menu":
            return [item if isinstance(item, str) else f"  {item.label}" for item in MENU]
        if mode == "boards":
            return (["All boards"]
                    + [f"{label}  ({'switched off' if url in self.boards_off else count})"
                       for url, label, count in self.board_items] + [ADD_ROW])
        if mode == "settings":
            width = max((len(key) for key, _ in self.setting_items), default=0)
            return [f"{key.replace('_', ' ').ljust(width)}  {self.shown_value(value)}" for key, value in self.setting_items]
        if mode == "unban":
            return list(self.ban_items)
        if mode == "result":
            return self.output_rows or self.output_snapshot
        if mode == "help":
            return list(HELP_TEXT)
        return []

    @staticmethod
    def shown_value(value: str) -> str:
        if value in ("true", "false"):
            return "on" if value == "true" else "off"
        try:
            parsed = json.loads(value)
        except ValueError:
            return value
        return parsed if isinstance(parsed, str) else value

    def selectable(self, index: int) -> bool:
        return self.view != "menu" or isinstance(MENU[index], Action)

    def move(self, delta: int) -> None:
        count = len(self.rows())
        if not count:
            self.cursor = 0
            return
        target = max(0, min(count - 1, self.cursor + delta))
        step = 1 if delta > 0 else -1
        while 0 <= target < count and not self.selectable(target):
            target += step
        if 0 <= target < count:
            self.cursor = target
        elif not self.selectable(self.cursor):
            self.cursor = next(index for index in range(count) if self.selectable(index))

    def open(self, mode: str, cursor: int = 0) -> None:
        self.mode = mode
        self.cursor = cursor
        self.session.message = ""
        if mode == "menu":
            self.move(0)

    def close(self) -> None:
        self.mode = "browse"
        self.cursor = 0

    def load(self, mode: str) -> bool:
        backend = self.session.backend
        try:
            if mode == "boards":
                self.board_items = backend.boards()
                self.boards_off = set(getattr(backend, "disabled_boards", set)())
            elif mode == "settings":
                self.setting_items = backend.settings()
            elif mode == "unban":
                self.ban_items = backend.bans()
        except (OSError, ValueError) as exc:
            self.session.message = str(exc)
            return False
        return True

    def refresh(self) -> None:
        if self.mode in ("boards", "settings", "unban") and self.load(self.mode):
            self.cursor = max(0, min(self.cursor, len(self.rows()) - 1))

    def drop_missing_board(self) -> None:
        """Stop filtering by a board once it is really gone, not when its removal is merely asked for."""
        if not self.board:
            return
        try:
            remaining = {url for url, _, _ in self.session.backend.boards()}
        except (OSError, ValueError):
            return
        if self.board not in remaining:
            self.board = ""
            self.session.filter_pins()

    def start(self, command: str, args: tuple[str, ...] = (), label: str = "", show_output: bool = False,
              then: tuple[str, tuple[str, ...], str] | None = None) -> None:
        backend = self.session.backend
        try:
            if backend.action_busy:
                self.session.message = f"Still busy: {self.running or 'another action'}"
                return
            backend.start_action(command, args)
        except (OSError, ValueError, RuntimeError) as exc:
            self.session.message = str(exc)
            return
        self.running = label or command.capitalize()
        self.show_output = show_output
        self.then = then
        self.session.message = ""

    def confirm(self, command: str, args: tuple[str, ...], prompt: str, label: str = "",
                then: tuple[str, tuple[str, ...], str] | None = None) -> None:
        self.pending = (command, args)
        self.pending_label = label
        self.pending_then = then
        self.prompt = prompt
        self.return_mode = self.mode if self.mode in LIST_MODES else "browse"
        self.mode = "confirm"
        self.session.message = ""

    def ask(self, mode: str, text: str, action: Action | None = None) -> None:
        self.mode = mode
        self.text = text
        self.action = action
        self.session.message = ""

    def invoke(self, action: Action) -> None:
        session = self.session
        kind = action.kind
        if kind in ("job", "report"):
            self.close()
            self.start(action.command, action.args, action.label, show_output=kind == "report")
        elif kind == "status":
            self.close()
            self.start(action.command, action.args, action.label, show_output=True)
        elif kind == "pause":
            self.close()
            if session.paused:
                self.start("resume", (), "Resume")
            else:
                self.start("pause", (), "Pause")
        elif kind == "confirm":
            self.close()
            self.confirm(action.command, action.args, action.confirm, action.label.rstrip("…"))
        elif kind == "duration":
            self.close()
            self.ask("duration", "30m", action)
        elif kind == "screen":
            self.close()
            if session.displays > 1:
                self.ask("display", "1", action)
            else:
                self.target_screen(action, "1")
        elif kind == "add-board":
            self.close()
            self.ask("board", "")
        elif kind == "favorites":
            self.close()
            self.favorites = not self.favorites
            session.filter_pins()
            session.message = "Showing favorites only" if self.favorites else "Showing all wallpapers"
        elif kind == "output":
            self.output_snapshot = list(self.output) or ["Nothing has run yet."]
            self.output_rows = []
            self.open("result")
        elif kind == "help":
            self.open("help")
        elif kind == "boards":
            if self.load("boards"):
                self.open("boards", next((index for index, item in enumerate(self.board_items, 1)
                                          if item[0] == self.board), 0))
        elif kind == "settings":
            if self.load("settings"):
                self.open("settings")
        elif kind == "bans":
            if self.load("unban"):
                self.open("unban")
                if not self.ban_items:
                    self.close()
                    session.message = "No banned wallpapers"

    def target_screen(self, action: Action, display: str) -> None:
        session = self.session
        if action.command != "ban":
            self.start(action.command, (display,), action.label)
            return
        try:
            pin_id = session.backend.displayed_pin(int(display))
        except (OSError, ValueError, RuntimeError) as exc:
            session.message = str(exc)
            return
        if not pin_id:
            session.message = f"Nothing is known to be showing on display {display}"
            return
        where = f" on display {display}" if session.displays > 1 else ""
        self.confirm("ban", (display, "--expected-pin", pin_id),
                     f"Ban the wallpaper showing{where} and move to the next one?", "Ban")

    def select(self) -> None:
        rows = self.rows()
        if not rows:
            return
        self.cursor = min(self.cursor, len(rows) - 1)
        if self.mode == "menu":
            item = MENU[self.cursor]
            if isinstance(item, Action):
                self.invoke(item)
        elif self.mode == "boards":
            if self.cursor > len(self.board_items):
                self.close()
                self.ask("board", "")
                return
            chosen = "" if self.cursor == 0 else self.board_items[self.cursor - 1][0]
            if chosen in self.boards_off:
                self.session.message = "That board is switched off. Press d to switch it back on"
                return
            self.board = chosen
            self.close()
            self.session.filter_pins()
        elif self.mode == "settings":
            key, value = self.setting_items[self.cursor]
            name = key.replace("_", " ")
            if value in ("true", "false"):
                self.start("config", (key, "false" if value == "true" else "true", value), f"Change {name}")
                return
            self.setting_key, self.setting_expected = key, value
            self.setting_is_text = value.startswith('"')
            self.ask("edit", self.shown_value(value))
        elif self.mode == "unban":
            self.start("unban", (self.ban_items[self.cursor],), "Unban")

    def submit(self) -> None:
        session = self.session
        if self.mode == "search":
            self.close()
            return
        if self.mode == "edit":
            raw = json.dumps(self.text) if self.setting_is_text else self.text
            try:
                json.dumps(json.loads(raw), allow_nan=False)
            except (ValueError, RecursionError):
                session.message = "That isn't a valid value; lists look like [\"a\", \"b\"], numbers like 15"
                return
            self.mode = "settings"
            if raw != self.setting_expected:
                self.start("config", (self.setting_key, raw, self.setting_expected),
                           f"Change {self.setting_key.replace('_', ' ')}")
            return
        if self.mode == "board":
            try:
                change = session.backend.add_board(self.text)
            except (OSError, ValueError) as exc:
                session.message = str(exc)
                return
            self.close()
            self.start("config", change, "Add board", then=("refresh", (), "Fetch pins from the new board"))
            return
        action = self.action
        if action is None:
            self.close()
            return
        if self.mode == "display":
            if not self.text.isdecimal() or int(self.text) < 1:
                session.message = "Type a display number, like 1"
                return
            self.close()
            self.target_screen(action, self.text)
            return
        self.close()
        self.start(action.command, action.args + (self.text,), f"Pause for {self.text}")

    def handle(self, key: str) -> bool:
        if self.mode == "framing":
            from wallpaper_tui_framing import key as framing_key

            framing_key(self.session, key)
            return True
        if self.mode == "focus":
            from wallpaper_tui_keys import focus_key

            focus_key(self.session, key)
            return True
        if self.mode == "confirm":
            self.handle_confirm(key)
            return True
        if self.mode in INPUT_MODES:
            self.handle_input(key)
            return True
        if self.mode in LIST_MODES:
            self.handle_list(key)
            return True
        return self.handle_browse(key)

    def handle_confirm(self, key: str) -> None:
        session = self.session
        if key in ("y", "Y") and self.pending is not None:
            if session.backend.action_busy:
                session.message = f"Still busy: {self.running or 'another action'}"
                return
            command, args = self.pending
            self.pending = None
            self.mode = self.return_mode
            if command == "library-ban":
                session.ban(args[0])
            else:
                self.start(command, args, self.pending_label, then=self.pending_then)
        elif key in ("enter", "esc", "q", "n", "N"):
            self.pending = None
            self.mode = self.return_mode
            session.message = "Cancelled"

    def handle_input(self, key: str) -> None:
        session = self.session
        if key == "esc":
            if self.mode == "search":
                self.search = self.search_before
                session.filter_pins()
            self.mode = "settings" if self.mode == "edit" else "browse"
            session.message = ""
            return
        if key == "enter":
            self.submit()
            return
        if key == "backspace":
            self.text = self.text[:-1]
        elif key == "ctrl-u":
            self.text = ""
        elif len(key) == 1 and key.isprintable():
            self.text += key
        else:
            return
        session.message = ""
        if self.mode == "search":
            self.search = self.text
            session.filter_pins()

    def handle_list(self, key: str) -> None:
        if key in ("esc", "q") or (key == "enter" and self.mode in ("result", "help")):
            self.close()
        elif key in ("up", "k"):
            self.move(-1)
        elif key in ("down", "j"):
            self.move(1)
        elif key == "pgup":
            self.move(-PAGE)
        elif key == "pgdn":
            self.move(PAGE)
        elif key in ("home", "g"):
            self.cursor = 0
            self.move(0)
        elif key in ("end", "G"):
            self.cursor = max(0, len(self.rows()) - 1)
            self.move(0)
        elif key == "enter":
            self.select()
        elif self.mode == "boards" and key in ("d", "x", "+"):
            self.manage_board(key)
        elif key == "A" and self.mode == "unban" and self.ban_items:
            self.confirm("unban", ("all",), f"Bring back all {len(self.ban_items)} banned wallpapers?", "Unban all")

    def manage_board(self, key: str) -> None:
        session = self.session
        if key == "+":
            self.close()
            self.ask("board", "")
            return
        if not 1 <= self.cursor <= len(self.board_items):
            session.message = "Move to a board first"
            return
        url, label, _ = self.board_items[self.cursor - 1]
        try:
            if key == "x":
                change = session.backend.remove_board(url)
                self.confirm("config", change, f"Remove {label} and delete its downloaded wallpapers?",
                             "Remove board", then=("refresh", (), "Tidy up after removing the board"))
                return
            off = session.backend.toggle_board(url)
            session.reload()
        except (OSError, ValueError) as exc:
            session.message = str(exc)
            return
        self.load("boards")
        if off and self.board == url:
            self.board = ""
            session.filter_pins()
        session.message = ("Switched off: its wallpapers are out of rotation until you switch it back on" if off
                           else "Switched back on")

    def handle_browse(self, key: str) -> bool:
        session = self.session
        if key in ("a", ":"):
            self.open("menu")
        elif key == "/":
            self.search_before = self.search
            self.ask("search", self.search)
        elif key == "x":
            pin = session.pin
            if pin is None:
                session.message = "Nothing is selected"
            else:
                self.confirm("library-ban", (pin.key,), "Ban this wallpaper so it never shows again?", "Ban")
        elif key == "esc":
            if self.search or self.board or self.favorites:
                self.search, self.board, self.favorites = "", "", False
                session.filter_pins()
                session.message = "Showing all wallpapers"
            else:
                session.message = ""
        elif key in SHORTCUTS:
            self.invoke(SHORTCUTS[key])
        else:
            return False
        return True
