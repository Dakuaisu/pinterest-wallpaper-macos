from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from wallpaper_framing import Framing

if TYPE_CHECKING:
    from wallpaper_tui import Session

ASPECTS = ("display", "original", "1:1", "4:3", "3:2", "16:9", "9:16", "21:9")
STEP = 5


def begin(session: Session) -> None:
    pin, draft = session.pin, session.draft
    if pin is None:
        return
    if not hasattr(pin, "framing") or not hasattr(draft, "framing_changed"):
        session.message = "Framing editing requires a framing-capable backend"
        return
    session.framing_pin, session.framing_draft = pin, draft
    session.controls.mode = "framing"
    session.message = ""


def finish(session: Session, accept: bool) -> None:
    pin, draft, baseline = session.pin, session.framing_draft, session.framing_pin
    if accept and pin is not None and draft is not None and baseline is not None and pin.key == baseline.key:
        session.set_draft(pin, draft)
    session.framing_pin = None
    session.framing_draft = None
    session.controls.close()
    if baseline is not None and not getattr(session.drafts.get(baseline.key), "framing_changed", False):
        latest = session.latest_pins.get(baseline.key)
        if latest is not None:
            session.all_pins = [replace(item, framing=latest.framing) if item.key == baseline.key else item
                                for item in session.all_pins]
            session.filter_pins()
    session.message = "Draft: s save, Enter apply" if accept else "Framing edit cancelled"


def current(session: Session) -> Framing | None:
    pin, draft = session.framing_pin, session.framing_draft
    if pin is None or draft is None:
        return None
    return draft.framing if draft.framing_changed else pin.framing


def key(session: Session, pressed: str) -> None:
    pin, draft = session.framing_pin, session.framing_draft
    if pin is None or draft is None:
        finish(session, False)
        return
    if pressed in ("enter", "esc"):
        finish(session, pressed == "enter")
        return
    if pressed == "]":
        session.display = session.display % session.displays + 1
        session.message = ""
        return
    if pressed == "0":
        framing = None
    elif pressed in ("m", "left", "right", "up", "down"):
        framing = current(session) or Framing("proportional")
        if pressed == "m":
            framing = framing._replace(mode="stretch" if framing.mode == "proportional" else "proportional")
        elif framing.mode == "proportional" and pressed in ("left", "right"):
            direction = 1 if pressed == "right" else -1
            framing = framing._replace(aspect=ASPECTS[(ASPECTS.index(framing.aspect) + direction) % len(ASPECTS)])
        else:
            field = "zoom" if framing.mode == "proportional" else "width" if pressed in ("left", "right") else "height"
            direction = 1 if pressed in ("up", "right") else -1
            value = max(25, min(400, getattr(framing, field) + direction * STEP))
            framing = framing._replace(**{field: value})
    else:
        return
    session.framing_draft = replace(draft, framing=framing, framing_changed=framing != pin.framing)
    session.message = ""


def status(session: Session) -> str:
    framing = current(session)
    if framing is None:
        return f"D{session.display} legacy"
    if framing.mode == "proportional":
        return f"D{session.display} proportional {framing.aspect} {framing.zoom}%"
    return f"D{session.display} stretch {framing.width}% x {framing.height}%"
