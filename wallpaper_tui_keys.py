from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from wallpaper_focus import focus_point_rotated

if TYPE_CHECKING:
    from wallpaper_tui import Session


def focus_key(session: Session, key: str) -> None:
    """Edit a temporary focus in the displayed image's coordinate system."""
    pin, draft = session.focus_pin, session.focus_draft
    if pin is None or draft is None:
        session.finish_focus(False)
        return
    if key in ("enter", "esc"):
        session.finish_focus(key == "enter")
        return
    if key == "]":
        session.display = session.display % session.displays + 1
        session.message = ""
        return
    steps = {"left": (-0.05, 0), "right": (0.05, 0), "up": (0, -0.05), "down": (0, 0.05)}
    if key == "0":
        focus = None
    elif key in steps:
        point = draft.focus if draft.focus_changed else pin.focus
        angle = pin.angle if draft.angle is None else draft.angle
        x, y = focus_point_rotated(point if point is not None else (0.5, 0.5), angle)
        dx, dy = steps[key]
        point = (max(0.0, min(1.0, x + dx)), max(0.0, min(1.0, y + dy)))
        x, y = focus_point_rotated(point, -angle)
        focus = (round(x, 4), round(y, 4))
    else:
        return
    session.message = ""
    session.focus_draft = replace(draft, focus=focus, focus_changed=focus != pin.focus)


def text_key(pending: bytearray) -> str | None:
    special = {8: "backspace", 9: "tab", 10: "enter", 13: "enter", 16: "ctrl-p",
               21: "ctrl-u", 127: "backspace"}
    while pending and pending[0] != 27:
        first = pending[0]
        if first >= 128:
            length = 2 if 0xC2 <= first <= 0xDF else 3 if 0xE0 <= first <= 0xEF else 4 if 0xF0 <= first <= 0xF4 else 1
            if len(pending) < length:
                return None
            try:
                char = bytes(pending[:length]).decode("utf-8")
            except UnicodeDecodeError:
                del pending[0]
                continue
            del pending[:length]
            if char.isprintable():
                return char
        else:
            del pending[0]
            if first in special:
                return special[first]
            if 32 <= first < 127:
                return chr(first)
    return None
