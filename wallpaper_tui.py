from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from PIL import Image

from wallpaper_tui_preview import PreviewWorker
from wallpaper_tui_terminal import (
    Size,
    Terminal,
    Terminated,
)

if TYPE_CHECKING:
    from wallpaper_tui_backend import Backend, Draft, Pin

PAGE = 10


class Session:
    """Keep the selection, per-pin drafts and preview for one browser session."""

    def __init__(self, backend: Backend, draft_type: type[Draft]) -> None:
        from wallpaper_tui_controls import Controls

        self.backend = backend
        self.draft_type = draft_type
        self.drafts: dict[str, Draft] = {}
        self.draft_bases: dict[str, Pin] = {}
        self.focus_pin: Pin | None = None
        self.focus_draft: Draft | None = None
        self.framing_pin: Pin | None = None
        self.framing_draft: Draft | None = None
        self.index = 0
        self.original = False
        self.message = ""
        self.worker = PreviewWorker()
        self.preview_pending = False
        self.revision = 0
        self.first_frame = True
        self.paused = False
        self.status = ""
        self.displays = 1
        self.display = 1
        try:
            self.pins = backend.pins()
        except (ValueError, OSError) as exc:
            self.pins = []
            self.message = str(exc)
        self.all_pins = list(self.pins)
        self.latest_pins = {pin.key: pin for pin in self.pins}
        self.controls = Controls(self)
        self.refresh_overview()

    @property
    def pin(self) -> Pin | None:
        return self.pins[self.index] if self.pins else None

    @property
    def draft(self) -> Draft:
        pin = self.pin
        if (pin is not None and self.framing_pin is not None and pin.key == self.framing_pin.key
                and self.framing_draft is not None):
            return self.framing_draft
        if (pin is not None and self.focus_pin is not None and pin.key == self.focus_pin.key
                and self.focus_draft is not None):
            return self.focus_draft
        return self.drafts.get(pin.key, self.draft_type()) if pin is not None else self.draft_type()

    def refresh_overview(self) -> None:
        overview = getattr(self.backend, "overview", None)
        if overview is None:
            return
        try:
            self.paused, self.status, self.displays = overview()
            self.displays = max(1, self.displays)
            self.display = min(self.display, self.displays)
        except (ValueError, OSError) as exc:
            self.message = str(exc)

    def move_to(self, index: int) -> None:
        self.index = max(0, min(len(self.pins) - 1, index))
        self.message = ""

    def handle(self, key: str) -> bool:
        if key == "eof":
            return False
        if self.controls.handle(key):
            return True
        if key == "q":
            return False
        steps = {"up": -1, "k": -1, "left": -1, "down": 1, "j": 1, "right": 1, "pgup": -PAGE, "pgdn": PAGE}
        if key in steps:
            self.move_to(self.index + steps[key])
        elif key in ("home", "g"):
            self.move_to(0)
        elif key in ("end", "G"):
            self.move_to(len(self.pins) - 1)
        pin = self.pin
        if pin is None:
            return True
        draft = self.draft
        if key == "c":
            if not hasattr(pin, "focus") or not hasattr(draft, "focus_changed"):
                self.message = "Focus editing requires a focus-capable backend"
            else:
                self.focus_pin, self.focus_draft = pin, draft
                self.controls.mode = "focus"
                self.message = ""
        elif key == "f":
            from wallpaper_tui_framing import begin

            begin(self)
        elif key == "]":
            self.display = self.display % self.displays + 1
            self.message = f"Previewing display {self.display}"
        elif key == "v":
            self.original = not self.original
            self.message = "Showing the untouched original" if self.original else ""
        elif key == "r":
            angle = ((pin.angle if draft.angle is None else draft.angle) + 90) % 360
            from dataclasses import replace

            self.set_draft(pin, replace(draft, angle=None if angle == pin.angle else angle))
            self.message = ""
        elif key == "F":
            fill = not (pin.fill if draft.fill is None else draft.fill)
            from dataclasses import replace

            if hasattr(pin, "framing") and hasattr(draft, "framing_changed"):
                draft = replace(draft, framing=None, framing_changed=pin.framing is not None)
            self.set_draft(pin, replace(draft, fill=None if fill == pin.fill else fill))
            self.message = ""
        elif key in ("s", "enter", " "):
            if getattr(self.backend, "action_busy", False):
                self.message = f"Still busy: {self.controls.running or 'another action'}"
                return True
            try:
                if key == "s" and pin.key not in self.drafts:
                    self.message = "Nothing to save; f frames, r rotates, F changes fill"
                elif key == "s":
                    self.save(pin, draft)
                    self.message = "Saved for next time"
                elif key == "enter":
                    self.backend.apply(pin, draft)
                    if pin.key in self.drafts:
                        self.save(pin, draft)
                    self.reload()
                    self.message = "Wallpaper set"
                else:
                    self.favorite(pin)
            except (ValueError, OSError) as exc:
                self.message = str(exc)
        return True

    def save(self, pin: Pin, draft: Draft) -> None:
        saved = self.backend.save(pin, draft)
        self.pins[self.index] = saved
        self.all_pins = [saved if item.key == pin.key else item for item in self.all_pins]
        self.latest_pins[pin.key] = saved
        self.drafts.pop(pin.key, None)
        self.draft_bases.pop(pin.key, None)
        self.revision += 1

    def favorite(self, pin: Pin) -> None:
        toggle = getattr(self.backend, "toggle_favorite", None)
        if toggle is None:
            self.message = "Favorites aren't available here"
            return
        try:
            added = toggle(pin)
        finally:
            self.reload()
        self.message = "Added to favorites" if added else "Removed from favorites"

    def ban(self, key: str) -> None:
        try:
            self.backend.ban(key)
            self.reload()
            self.message = "Banned. Undo it from a (More) → Banned wallpapers"
        except (ValueError, OSError) as exc:
            self.message = str(exc)

    def set_draft(self, pin: Pin, draft: Draft) -> None:
        from dataclasses import replace

        previous = self.drafts.get(pin.key, self.draft_type())
        previous_focus = getattr(previous, "focus_changed", False)
        changed_focus = getattr(draft, "focus_changed", False)
        previous_framing = getattr(previous, "framing_changed", False)
        changed_framing = getattr(draft, "framing_changed", False)
        if draft.angle is None and draft.fill is None and not changed_focus and not changed_framing:
            self.drafts.pop(pin.key, None)
            self.draft_bases.pop(pin.key, None)
        else:
            base = self.draft_bases.get(pin.key, pin)
            base = replace(
                base, angle=pin.angle if previous.angle is None and draft.angle is not None else base.angle,
                fill=pin.fill if previous.fill is None and draft.fill is not None else base.fill)
            if not previous_focus and changed_focus:
                base = replace(base, focus=pin.focus)
            if not previous_framing and changed_framing:
                base = replace(base, framing=pin.framing)
            self.draft_bases[pin.key] = base
            self.drafts[pin.key] = draft
        latest = self.latest_pins.get(pin.key, pin)
        cleared_angle = previous.angle is not None and draft.angle is None
        cleared_fill = previous.fill is not None and draft.fill is None
        cleared_focus = previous_focus and not changed_focus
        cleared_framing = previous_framing and not changed_framing
        if cleared_angle or cleared_fill or cleared_focus or cleared_framing:
            self.all_pins = [replace(item, angle=latest.angle if cleared_angle else item.angle,
                                     fill=latest.fill if cleared_fill else item.fill)
                             if item.key == pin.key else item for item in self.all_pins]
            if cleared_focus:
                self.all_pins = [replace(item, focus=latest.focus) if item.key == pin.key else item
                                 for item in self.all_pins]
            if cleared_framing:
                self.all_pins = [replace(item, framing=latest.framing) if item.key == pin.key else item
                                 for item in self.all_pins]
            self.filter_pins()

    def finish_focus(self, accept: bool) -> None:
        from dataclasses import replace

        pin, draft, baseline = self.pin, self.focus_draft, self.focus_pin
        if accept and pin is not None and draft is not None and baseline is not None and pin.key == baseline.key:
            self.set_draft(pin, draft)
        self.focus_pin = None
        self.focus_draft = None
        self.controls.close()
        if baseline is not None and not getattr(self.drafts.get(baseline.key), "focus_changed", False):
            latest = self.latest_pins.get(baseline.key)
            if latest is not None:
                self.all_pins = [replace(item, focus=latest.focus) if item.key == baseline.key else item
                                 for item in self.all_pins]
                self.filter_pins()
        self.message = "Draft: s save, Enter apply" if accept else "Focus edit cancelled"

    def filter_pins(self) -> None:
        from wallpaper_tui_view import pin_label

        selected = self.pin.key if self.pin is not None else None
        controls = self.controls
        query = controls.search.casefold().strip()
        self.pins = [pin for pin in self.all_pins
                     if (not controls.board or controls.board in getattr(pin, "boards", ()))
                     and (not controls.favorites or getattr(pin, "favorite", False))
                     and (not query or query in f"{pin_label(pin)} {pin.note}".casefold())]
        self.index = next((index for index, pin in enumerate(self.pins) if pin.key == selected), 0)

    def reload(self) -> None:
        from dataclasses import replace

        updated = self.backend.pins()
        self.latest_pins = {pin.key: pin for pin in updated}
        self.all_pins = [replace(pin,
                                 angle=self.draft_bases[pin.key].angle if self.drafts[pin.key].angle is not None else pin.angle,
                                 fill=self.draft_bases[pin.key].fill if self.drafts[pin.key].fill is not None else pin.fill)
                         if pin.key in self.drafts else pin for pin in updated]
        self.all_pins = [replace(pin, focus=self.draft_bases[pin.key].focus)
                         if pin.key in self.drafts and getattr(self.drafts[pin.key], "focus_changed", False)
                         else pin for pin in self.all_pins]
        self.all_pins = [replace(pin, framing=self.draft_bases[pin.key].framing)
                         if pin.key in self.drafts and getattr(self.drafts[pin.key], "framing_changed", False)
                         else pin for pin in self.all_pins]
        if self.focus_pin is not None:
            self.all_pins = [replace(pin, focus=self.focus_pin.focus) if pin.key == self.focus_pin.key else pin
                             for pin in self.all_pins]
        if self.framing_pin is not None:
            self.all_pins = [replace(pin, framing=self.framing_pin.framing) if pin.key == self.framing_pin.key else pin
                             for pin in self.all_pins]
        self.filter_pins()
        if self.focus_pin is not None and (self.pin is None or self.pin.key != self.focus_pin.key):
            self.finish_focus(False)
        if self.framing_pin is not None and (self.pin is None or self.pin.key != self.framing_pin.key):
            from wallpaper_tui_framing import finish

            finish(self, False)
        self.refresh_overview()

    def poll_actions(self) -> bool:
        take_action = getattr(self.backend, "take_action", None)
        if take_action is None:
            return False
        result = take_action()
        if result is None:
            return False
        success, output = result
        controls = self.controls
        label, controls.running = controls.running or "Action", ""
        show_output, controls.show_output = controls.show_output, False
        then, controls.then = controls.then, None
        self.revision += 1
        controls.output = output.splitlines()
        try:
            self.reload()
        except (ValueError, OSError) as exc:
            self.message = f"{label} finished, but reloading failed: {exc}"
            return True
        controls.refresh()
        controls.drop_missing_board()
        if (show_output or (not success and len(controls.output) > 1)) and controls.mode in ("browse", "result"):
            controls.output_snapshot = list(controls.output)
            controls.output_rows = []
            controls.open("result")
        elif success:
            self.message = f"{label}: done"
            if then is not None:
                controls.start(*then)
        else:
            self.message = f"{label} failed: {controls.output[0]}" if controls.output else f"{label} failed"
        return True

    def preview(self, bounds: tuple[int, int] = (256, 256)) -> Image.Image | None:
        pin = self.pin
        if pin is None:
            self.preview_pending = False
            return None
        draft = self.draft
        focus_editor = self.focus_pin is not None
        framing_editor = self.framing_pin is not None
        original = self.original and not focus_editor and not framing_editor
        saved_focus = getattr(pin, "focus", None)
        draft_focus = getattr(draft, "focus", None)
        key = (pin.key, pin.angle, pin.fill, tuple(saved_focus) if saved_focus is not None else None,
               draft.angle, draft.fill, tuple(draft_focus) if draft_focus is not None else None,
               getattr(draft, "focus_changed", False), getattr(pin, "framing", None),
               getattr(draft, "framing", None), getattr(draft, "framing_changed", False),
               original, bounds, self.revision, self.display, focus_editor, framing_editor)
        display = self.display
        if display == 1 and not focus_editor:
            make_task = lambda: self.backend.preview_task(pin, draft, original, bounds)
        else:
            make_task = lambda: self.backend.preview_task(pin, draft, original, bounds,
                                                          display=display, focus_editor=focus_editor)
        result = self.worker.request(key, make_task)
        self.preview_pending = result.pending
        if result.error is not None:
            if not isinstance(result.error, (ValueError, OSError)):
                raise result.error
            self.message = str(result.error)
        return result.image

    def render(self, terminal: Terminal, size: Size) -> None:
        from wallpaper_tui_view import render

        render(self, terminal, size)


def run(backend: Backend) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        sys.stderr.write("Wallpaper browser requires an interactive terminal.\n")
        return 1
    from wallpaper_tui_backend import Draft
    from wallpaper_tui_view import too_small

    try:
        with Terminal() as terminal:
            session = Session(backend, Draft)
            previous_size = None
            previous_image_mode = None
            redraw = True
            try:
                while True:
                    size = terminal.size()
                    image_mode = getattr(terminal, "image_mode", terminal.kitty)
                    if session.worker.take_changed():
                        redraw = True
                    if session.poll_actions():
                        redraw = True
                    if redraw or size != previous_size or image_mode != previous_image_mode:
                        session.render(terminal, size)
                        previous_size = size
                        previous_image_mode = image_mode
                        redraw = False
                    key = terminal.read_key()
                    if key is not None:
                        if too_small(size):
                            # Prompts are hidden at this size, so nothing but quitting may act.
                            if key in ("q", "eof"):
                                return 0
                            continue
                        if not session.handle(key):
                            return 0
                        redraw = True
            finally:
                session.worker.close()
                close_actions = getattr(backend, "close_actions", None)
                if close_actions is not None:
                    close_actions()
    except KeyboardInterrupt:
        return 130
    except Terminated:
        return 143
