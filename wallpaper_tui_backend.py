"""State-safe adapter between the terminal browser and wallpaper core."""

from __future__ import annotations

import copy
import json
import os
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from typing import Callable

from PIL import Image, ImageDraw

import wallpaper_tui_actions
from wallpaper_framing import Framing, parse_framing


@dataclass(frozen=True)
class Pin:
    key: str
    note: str
    angle: int
    fill: bool
    boards: tuple[str, ...] = ()
    favorite: bool = False
    color: str = ""
    showing: bool = False
    focus: tuple[float, float] | None = None
    framing: Framing | None = None


@dataclass(frozen=True)
class Draft:
    angle: int | None = None
    fill: bool | None = None
    focus: tuple[float, float] | None = None
    focus_changed: bool = False
    framing: Framing | None = None
    framing_changed: bool = False


class TuiError(ValueError):
    """An operation that the browser can report and recover from."""


class Backend:
    def __init__(self, core, config):
        self.core = core
        self.config = config
        self._snapshot = None
        self._actions = wallpaper_tui_actions.Actions(core)

    @contextmanager
    def _state(self):
        try:
            with self.core.file_lock(self.core.STATE_LOCK, wait=False) as acquired:
                if not acquired:
                    raise TuiError("another wallpaper command is running")
                yield self.core._load_state_unlocked(repair=False)
        except (OSError, ValueError, self.core.BadSource) as error:
            raise TuiError(str(error)) from error

    def _pin(self, key, state, files, config, validate=True):
        if (key not in state["pins"] or key not in files or (validate and key in state["banned"])
                or not os.path.isfile(os.path.join(self.core.IMAGE_DIR, files[key]))):
            raise TuiError(f"pin {key} is no longer available")
        angle = self.core.effective_angle(key, config, state, files)
        fill = state["fill_overrides"].get(key, key in config.get("fill_pins", []))
        width, height = self.core.oriented_size(state["pins"][key], angle)
        note = f"{width}x{height} source" if width and height else ""
        meta = state["pins"][key]
        boards = tuple(meta.get("boards", [meta.get("board")]))
        focus = state.get("crop_focus", {}).get(key)
        framing = parse_framing(state.get("framing", {}).get(key))
        return Pin(key, note, angle, fill, tuple(b for b in boards if b), key in state["favorites"],
                   meta.get("color") or meta.get("avg") or "", key in self.core.current_pins(state),
                   (focus[0], focus[1]) if focus is not None else None, framing)

    def pins(self) -> list[Pin]:
        try:
            state = self.core._load_state_unlocked(repair=False)
            if not os.path.isdir(self.core.IMAGE_DIR):
                self._snapshot = state
                return []
            files = self.core.source_files()
            pins = [self._pin(key, state, files, self.config, validate=False)
                    for key in self.core.available_pins(state, files)]
            self._snapshot = copy.deepcopy(state)
            return pins
        except (OSError, ValueError) as error:
            raise TuiError(str(error)) from error

    def boards(self) -> list[tuple[str, str, int]]:
        pins = self.pins()
        return [(url, urllib.parse.urlsplit(url).path.strip("/") or url,
                 sum(url in pin.boards for pin in pins)) for url in self.config["boards"]]

    def overview(self) -> tuple[bool, str, int]:
        """Whether rotation is paused, a short label for it, and the number of displays."""
        try:
            state = self.core._load_state_unlocked(repair=False)
        except (OSError, ValueError) as error:
            raise TuiError(str(error)) from error
        paused = self.core.is_paused(state)
        label = self.core.pause_label(state).capitalize() if paused else "Rotation on"
        return paused, label, max(1, len(state["screens"]))

    def toggle_favorite(self, pin: Pin) -> bool:
        with self._state() as state:
            if pin.key not in state["pins"]:
                raise TuiError("That wallpaper is no longer in your library")
            favorites = state["favorites"]
            if pin.key in favorites:
                favorites.remove(pin.key)
            else:
                favorites.append(pin.key)
            self.core.save_state(state)
            return pin.key in favorites

    def ban(self, key: str) -> None:
        with self._state() as state:
            if key not in state["pins"]:
                raise TuiError("that wallpaper is no longer in your library")
            if key not in state["banned"]:
                state["banned"].append(key)
            state["deck"] = [item for item in state["deck"] if item != key]
            self.core.save_state(state)

    def bans(self) -> list[str]:
        try:
            return list(self.core._load_state_unlocked(repair=False)["banned"])
        except (OSError, ValueError) as error:
            raise TuiError(str(error)) from error

    def displayed_pin(self, display: int) -> str | None:
        if type(display) is not int or display < 1:
            raise TuiError("display must be a positive integer")
        try:
            state = self.core._load_state_unlocked(repair=False)
            return self.core.pin_on_display(state, display)
        except (OSError, ValueError) as error:
            raise TuiError(str(error)) from error

    def settings(self) -> list[tuple[str, str]]:
        try:
            with open(self.core.CONFIG_FILE, encoding="utf-8") as handle:
                config = json.load(handle)
            if not isinstance(config, dict):
                raise TuiError("configuration must be a JSON object")
            rules = self.core.CONFIG_RULES.keys() | self.core.OPTIONAL_CONFIG_RULES.keys()
            return [(key, wallpaper_tui_actions.compact(value)) for key, value in config.items() if key in rules]
        except (OSError, ValueError) as error:
            raise TuiError(str(error)) from error

    def add_board(self, text: str) -> tuple[str, str, str]:
        """The settings change that adds a board: key, new value, and the value it must replace."""
        url = text.strip()
        if "://" not in url:
            url = "https://" + url
        try:
            parts = urllib.parse.urlsplit(url)
            names = [name for name in parts.path.split("/") if name]
            valid = self.core._safe_board_url(url) and len(names) >= 2
        except ValueError:
            valid = False
        if not valid:
            raise TuiError("That isn't a board link. It should look like pinterest.com/your-name/board-name")
        url = f"https://{parts.hostname}/{'/'.join(names)}/"
        try:
            with open(self.core.CONFIG_FILE, encoding="utf-8") as handle:
                current = json.load(handle).get("boards")
        except (OSError, ValueError, AttributeError) as error:
            raise TuiError(f"config.json can't be read: {error}") from error
        if not isinstance(current, list):
            raise TuiError("config.json has no list of boards to add to")
        kept = [board for board in current if isinstance(board, str) and self.core.PLACEHOLDER_BOARD not in board]
        if any(board.rstrip("/") == url.rstrip("/") for board in kept):
            raise TuiError("That board is already in your list")
        return "boards", wallpaper_tui_actions.compact([*kept, url]), wallpaper_tui_actions.compact(current)

    def remove_board(self, url: str) -> tuple[str, str, str]:
        """The settings change that removes a board: key, new value, and the value it must replace."""
        try:
            with open(self.core.CONFIG_FILE, encoding="utf-8") as handle:
                current = json.load(handle).get("boards")
        except (OSError, ValueError, AttributeError) as error:
            raise TuiError(f"config.json can't be read: {error}") from error
        if not isinstance(current, list) or url not in current:
            raise TuiError("That board is no longer in your list")
        kept = [board for board in current if board != url]
        if not kept:
            raise TuiError("This is your only board. Add another one before removing it")
        return "boards", wallpaper_tui_actions.compact(kept), wallpaper_tui_actions.compact(current)

    def disabled_boards(self) -> set[str]:
        try:
            return set(self.core._load_state_unlocked(repair=False)["disabled_boards"])
        except (OSError, ValueError) as error:
            raise TuiError(str(error)) from error

    def toggle_board(self, url: str) -> bool:
        """Switch a board off or back on; returns whether it is now off."""
        with self._state() as state:
            disabled = state["disabled_boards"]
            if url in disabled:
                disabled.remove(url)
            elif url in self.config["boards"]:
                disabled.append(url)
            else:
                raise TuiError("That board is no longer in your list")
            self.core.save_state(state)
            return url in disabled

    def start_action(self, command: str, args: tuple[str, ...] = ()) -> None:
        try:
            self._actions.start(command, args)
        except wallpaper_tui_actions.ActionError as error:
            raise TuiError(str(error)) from error

    def take_action(self) -> tuple[bool, str] | None:
        result = self._actions.take()
        if result and result[0] and self._actions.command == "config":
            self.config, _ = self.core.load_config(create=False)
        return result

    @property
    def action_busy(self) -> bool:
        return self._actions.busy

    def close_actions(self) -> None:
        """Leave running actions to finish independently after the UI closes."""
        self._actions.close()

    def _draft(self, pin: Pin, draft: Draft, state, files, config) -> None:
        current = self._pin(pin.key, state, files, config)
        if draft.angle is not None:
            if type(draft.angle) is not int or draft.angle not in (0, 90, 180, 270):
                raise TuiError("rotation must be 0, 90, 180, or 270")
            if current.angle != pin.angle:
                raise TuiError("rotation changed since this pin was loaded")
            state["rotations"][pin.key] = draft.angle
        if draft.fill is not None:
            if not isinstance(draft.fill, bool):
                raise TuiError("fill must be true or false")
            if current.fill != pin.fill:
                raise TuiError("fill changed since this pin was loaded")
            state["fill_overrides"][pin.key] = draft.fill
        if draft.focus_changed:
            if current.focus != pin.focus:
                raise TuiError("focus changed since this pin was loaded")
            point = draft.focus
            if point is not None and (not isinstance(point, tuple) or len(point) != 2 or
                                      not all(type(value) in (int, float) and 0 <= value <= 1 for value in point)):
                raise TuiError("focus must be a pair of coordinates between 0 and 1")
            focuses = state.setdefault("crop_focus", {})
            if point is None:
                focuses.pop(pin.key, None)
            else:
                focuses[pin.key] = list(point)
        if draft.framing_changed:
            if current.framing != pin.framing:
                raise TuiError("framing changed since this pin was loaded")
            framing = draft.framing
            if framing is None:
                state.get("framing", {}).pop(pin.key, None)
            else:
                validated = parse_framing(framing._asdict()) if isinstance(framing, Framing) else None
                if validated is None:
                    raise TuiError("invalid framing")
                state.setdefault("framing", {})[pin.key] = validated._asdict()

    def preview_task(self, pin: Pin, draft: Draft, original: bool,
                     bounds: tuple[int, int], *, display: int = 1,
                     focus_editor: bool = False) -> Callable[[], Image.Image]:
        snapshot = Backend(self.core, copy.deepcopy(self.config))
        snapshot._snapshot = copy.deepcopy(self._snapshot)
        return partial(snapshot.preview, pin, draft, original, bounds, display=display, focus_editor=focus_editor)

    def preview(self, pin: Pin, draft: Draft, original: bool,
                bounds: tuple[int, int] | None = None, *, display: int = 1,
                focus_editor: bool = False) -> Image.Image:
        if self._snapshot is None:
            raise TuiError("load pins before previewing")
        state = copy.deepcopy(self._snapshot)
        try:
            files = self.core.source_files()
            self._draft(pin, draft, state, files, self.config)
            if original and not focus_editor:
                image, _, _ = self.core.decode(os.path.join(self.core.IMAGE_DIR, files[pin.key]))
                if bounds is not None:
                    image.thumbnail(bounds, Image.Resampling.LANCZOS)
                return image
            source, ai = self.core.source_for(pin.key, self.config, files)
            try:
                image, _, _ = self.core.decode(source)
            except (OSError, ValueError, Image.DecompressionBombError):
                if not ai:
                    raise
                image, _, _ = self.core.decode(os.path.join(self.core.IMAGE_DIR, files[pin.key]))
                ai = False
            screens = self.core.current_screens(self.config, state, detect=False)
            screen = screens[display - 1] if type(display) is int and 1 <= display <= len(screens) else screens[0]
            settings = self.core.render_settings(pin.key, self.config, state, ai, files)
            screen_size = (screen["width"], screen["height"])
            if focus_editor:
                angle = settings["rotate"]
                rotated = image.transpose(self.core.TRANSPOSE_CW[angle]) if angle else image
                left, top, right, bottom = self.core.cover_crop_box(rotated, screen_size, settings)
                point = settings.get("crop_focus", (0.5, 0.5))
                x, y = self.core.focus_point_rotated(point, angle)

                result = rotated.convert("RGB")
                if bounds is not None:
                    result.thumbnail(bounds, Image.Resampling.LANCZOS)
                draw = ImageDraw.Draw(result)
                sx, sy = (result.width - 1) / rotated.width, (result.height - 1) / rotated.height
                outline = (round(left * sx), round(top * sy), round(right * sx), round(bottom * sy))
                draw.rectangle(outline, outline="black", width=3)
                draw.rectangle(outline, outline="white")

                cx, cy = round(x * (result.width - 1)), round(y * (result.height - 1))
                radius = max(1, min(6, min(result.size) // 10))
                draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill="white", outline="black")
                return result
            color = state["pins"][pin.key].get("color")
            if bounds is None:
                return self.core.render_image(image, screen_size, settings, color)
            scale = min(1, bounds[0] / screen_size[0], bounds[1] / screen_size[1])
            size = (max(1, round(screen_size[0] * scale)), max(1, round(screen_size[1] * scale)))
            return self.core.render_image(image, size, settings, color, reference_size=screen_size)
        except (OSError, ValueError, Image.DecompressionBombError, self.core.BadSource) as error:
            raise TuiError(str(error)) from error

    def save(self, pin: Pin, draft: Draft) -> Pin:
        with self._state() as state:
            config, problems = self.core.load_config(create=False)
            if problems:
                raise TuiError("; ".join(problems))
            files = self.core.source_files()
            self._draft(pin, draft, state, files, config)
            result = self._pin(pin.key, state, files, config)
            self.core.save_state(state)
            self.config = config
            self._snapshot = copy.deepcopy(state)
            return result

    def apply(self, pin: Pin, draft: Draft) -> None:
        with self._state() as state:
            config, problems = self.core.load_config(create=False)
            if problems:
                raise TuiError("; ".join(problems))
            files = self.core.source_files()
            render_state = copy.deepcopy(state)
            self._draft(pin, draft, render_state, files, config)
            try:
                os.makedirs(self.core.WALLPAPERS_DIR, exist_ok=True)
                screens = self.core.current_screens(config, state)
                assignment = self.core.render_for_screens([pin.key], screens, config, render_state, files)
                self.core.show(assignment, state, [pin.key])
            except self.core.WallpaperTransactionError as error:
                self.core.save_state(state)
                raise TuiError(str(error)) from error
            except (OSError, ValueError, self.core.BadSource, RuntimeError) as error:
                raise TuiError(str(error)) from error
            self.core.push_history(state, [pin.key])
            self.core.save_state(state)
