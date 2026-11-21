from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

from wallpaper_tui_commands import MENU
from wallpaper_tui_terminal import ESC, RESET, Box, Size, Terminal, clip_text

if TYPE_CHECKING:
    from wallpaper_tui import Session

COLOR = not os.environ.get("NO_COLOR")
BOLD = ESC + "[1m"
DIM = ESC + "[2m"
SELECTED = ESC + "[7m"
ACCENT = ESC + ("[1;36m" if COLOR else "[1m")
WARNING = ESC + ("[1;33m" if COLOR else "[1m")
LIVE = ESC + ("[32m" if COLOR else "[1m")
MINIMUM = (36, 10)

TITLES = {"menu": "More actions", "boards": "Boards", "settings": "Settings",
          "unban": "Banned wallpapers", "result": "Output", "help": "Keyboard help"}
PROMPTS = {"search": "Search", "duration": "Pause for how long? (30m, 2h, 1d)",
           "display": "Which display?", "edit": "", "board": "Paste the board's Pinterest link"}
HINTS = {
    "browse": (("f", "Frame"), ("↑↓", "Move"), ("c", "Focus"), ("Enter", "Set wallpaper"), ("Space", "Favorite"),
               ("r", "Rotate"), ("F", "Fill"), ("/", "Search"), ("n", "Next")),
    "focus": (),
    "menu": (("↑↓", "Move"), ("Enter", "Choose"), ("Esc", "Back")),
    "boards": (("↑↓", "Move"), ("Enter", "Show only this"), ("d", "Off/on"), ("x", "Remove"), ("+", "Add")),
    "settings": (("↑↓", "Move"), ("Enter", "Change"), ("Esc", "Back")),
    "unban": (("↑↓", "Move"), ("Enter", "Bring back"), ("A", "Bring back all"), ("Esc", "Back")),
    "result": (("↑↓", "Scroll"), ("Esc", "Back")),
    "help": (("↑↓", "Scroll"), ("Esc", "Back")),
    "confirm": (("y", "Yes"), ("Esc", "No")),
    "search": (("Enter", "Keep"), ("Esc", "Cancel")),
    "edit": (("Enter", "Save"), ("Ctrl-U", "Clear"), ("Esc", "Cancel")),
    "duration": (("Enter", "Pause"), ("Esc", "Cancel")),
    "display": (("Enter", "Continue"), ("Esc", "Cancel")),
    "board": (("Enter", "Add"), ("Esc", "Cancel")),
}
ALWAYS = {"browse": (("a", "More"), ("?", "Help"), ("q", "Quit")), "boards": (("Esc", "Back"),)}


def wrap(text: str, width: int) -> list[str]:
    clean = clip_text(text, max(1, len(text) * 2))
    lines = []
    while clean:
        part = clip_text(clean, width)
        if not part:
            break
        lines.append(part)
        clean = clean[len(part):]
    return lines or [""]


def wrap_prose(text: str, width: int) -> list[str]:
    clean = clip_text(text, max(1, len(text) * 2))
    lines = []
    while clean:
        part = clip_text(clean, width)
        if not part:
            break
        if len(part) < len(clean) and " " in part:
            boundary = part.rfind(" ")
            if boundary:
                part = part[:boundary + 1]
        lines.append(part)
        clean = clean[len(part):]
    return lines or [""]


def too_small(size: Size) -> bool:
    return size.columns < MINIMUM[0] or size.rows < MINIMUM[1]


def pin_label(pin: object) -> str:
    note = getattr(pin, "note", "")
    dimensions = re.match(r"(\d+)x(\d+)", note)
    if dimensions:
        return f"{dimensions.group(1)}×{dimensions.group(2)}"
    return note or f"…{pin.key[-8:]}"


def swatch(color: str) -> str:
    if not COLOR or not re.fullmatch(r"#[0-9a-fA-F]{6}", color or ""):
        return ""
    red, green, blue = (int(color[index:index + 2], 16) for index in (1, 3, 5))
    return f"{ESC}[38;2;{red};{green};{blue}m"


def fit_hints(hints: tuple[tuple[str, str], ...], always: tuple[tuple[str, str], ...], width: int) -> list[tuple[str, str]]:
    def length(items: list[tuple[str, str]]) -> int:
        return sum(len(key) + len(label) + 1 for key, label in items) + 3 * max(0, len(items) - 1)

    chosen: list[tuple[str, str]] = []
    for hint in hints:
        if length(chosen + [hint] + list(always)) > width:
            break
        chosen.append(hint)
    return chosen + list(always)


class Canvas:
    def __init__(self, size: Size) -> None:
        self.size = size
        self.parts: list[str] = []

    def text(self, row: int, column: int, text: str, width: int, style: str = "") -> None:
        if not 1 <= row <= self.size.rows or not 1 <= column <= self.size.columns:
            return
        usable = max(0, min(width, self.size.columns - column + 1))
        self.parts.append(f"{ESC}[{row};{column}H{style}{clip_text(text, usable)}{RESET}")

    def panel(self, box: Box, title: str, footer: str = "", active: bool = False) -> None:
        width, bottom = box.columns, box.row + box.rows - 1
        border = ACCENT if active else DIM
        self.text(box.row, box.column, "╭" + "─" * (width - 2) + "╮", width, border)
        for row in range(box.row + 1, bottom):
            self.text(row, box.column, "│", 1, border)
            self.text(row, box.column + width - 1, "│", 1, border)
        self.text(bottom, box.column, "╰" + "─" * (width - 2) + "╯", width, border)
        if title and width > 6:
            self.text(box.row, box.column + 2, f" {title} ", width - 4, BOLD)
        if footer and width > 6:
            self.text(bottom, box.column + 2, f" {footer} ", width - 4)

    def hints(self, row: int, hints: list[tuple[str, str]]) -> None:
        column = 2
        for key, label in hints:
            self.text(row, column, key, len(key), BOLD)
            self.text(row, column + len(key) + 1, label, len(label))
            column += len(key) + len(label) + 4


def window(cursor: int, count: int, capacity: int) -> int:
    return max(0, min(cursor - capacity // 2, count - capacity))


def draw_list(canvas: Canvas, session: Session, box: Box) -> None:
    pins = session.pins
    inner = box.columns - 2
    capacity = box.rows - 2
    digits = len(str(max(1, len(pins))))
    start = window(session.index, len(pins), capacity)
    for offset, pin in enumerate(pins[start:start + capacity]):
        index = start + offset
        row, selected = box.row + 1 + offset, index == session.index
        flags = ("~" if pin.key in session.drafts else "") + ("★" if getattr(pin, "favorite", False) else "") \
            + ("●" if getattr(pin, "showing", False) else "")
        paint = swatch(getattr(pin, "color", "")) if inner >= 20 else ""
        label_column = digits + (5 if paint else 2)
        text = f" {index + 1:>{digits}}".ljust(label_column) + pin_label(pin)
        text = clip_text(text, max(0, inner - len(flags) - 1)).ljust(inner - len(flags) - 1) + flags + " "
        canvas.text(row, box.column + 1, text, inner, SELECTED if selected else "")
        if paint:
            canvas.text(row, box.column + digits + 3, "██", 2, paint)
        if flags and not selected:
            canvas.text(row, box.column + inner - len(flags), flags, len(flags), LIVE if "●" in flags else ACCENT)


def list_footer(session: Session) -> str:
    controls = session.controls
    chips = []
    if controls.search:
        chips.append(f"“{controls.search}”")
    if controls.favorites:
        chips.append("★ only")
    if controls.board:
        chips.append(next((label for url, label, _ in controls.board_items if url == controls.board), "one board"))
    return " · ".join(chips)


def preview_footer(session: Session, width: int) -> str:
    pin = session.pin
    if pin is None:
        return ""
    draft = session.draft
    angle = pin.angle if draft.angle is None else draft.angle
    fill = pin.fill if draft.fill is None else draft.fill
    unsaved = (draft.angle is not None or draft.fill is not None or getattr(draft, "focus_changed", False)
               or getattr(draft, "framing_changed", False))
    for turned, filled, pending in ((f"turned {angle}°", "fills screen", "not saved"), (f"{angle}°", "fill", "unsaved")):
        parts = [pin_label(pin)] + [text for text, shown in ((turned, angle), (filled, fill), (pending, unsaved)) if shown]
        if len(" · ".join(parts)) <= width:
            break
    while len(parts) > 1 and len(" · ".join(parts)) > width:
        del parts[0]
    return " · ".join(parts)


def draw_overlay(canvas: Canvas, session: Session, box: Box) -> None:
    controls = session.controls
    mode = controls.view
    inner = box.columns - 4
    capacity = box.rows - 2
    rows = controls.rows()
    if mode == "result":
        rows = [part for row in controls.output_snapshot for part in wrap(row, inner)]
        controls.output_rows = rows
    scrolling = mode in ("result", "help")
    start = max(0, min(controls.cursor, len(rows) - capacity)) if scrolling else window(controls.cursor, len(rows), capacity)
    for offset, text in enumerate(rows[start:start + capacity]):
        index = start + offset
        selected = index == controls.cursor and not scrolling
        heading = (mode == "menu" and not controls.selectable(index)) or (mode == "help" and not text.startswith(" ") and text)
        style = SELECTED if selected else BOLD if heading else ""
        canvas.text(box.row + 1 + offset, box.column + 2, text.ljust(inner) if selected else text, inner, style)
        key = MENU[index].key if mode == "menu" and controls.selectable(index) else ""
        if key and inner > 50:
            canvas.text(box.row + 1 + offset, box.column + 46, key, len(key), SELECTED if selected else DIM)
    if not rows:
        canvas.text(box.row + 1, box.column + 2, "Nothing here.", inner)


def message_row(canvas: Canvas, session: Session, row: int) -> None:
    controls, width = session.controls, canvas.size.columns - 2
    if controls.mode == "framing":
        from wallpaper_tui_framing import status

        canvas.text(row, 2, session.message or status(session), width, ACCENT)
    elif controls.mode == "focus" and session.message:
        canvas.text(row, 2, session.message, width, WARNING)
    elif controls.mode == "focus" and session.focus_pin is not None:
        from wallpaper_focus import focus_point_rotated

        pin, draft = session.focus_pin, session.draft
        point = draft.focus if draft.focus_changed else pin.focus
        angle = pin.angle if draft.angle is None else draft.angle
        if point is None:
            position = "auto"
        else:
            x, y = focus_point_rotated(point, angle)
            position = f"{x:.0%},{y:.0%}"
        canvas.text(row, 2, f"D{session.display} {position} draft  ] display", width, ACCENT)
    elif controls.mode == "confirm":
        canvas.text(row, 2, wrap_prose(controls.prompt, width)[0], width, WARNING)
    elif controls.mode in PROMPTS:
        label = PROMPTS[controls.mode] or controls.setting_key.replace("_", " ")
        prefix = f"{label}: "
        shown = wrap(controls.text, max(1, width - len(prefix) - 1))[-1] if controls.text else ""
        canvas.text(row, 2, prefix, len(prefix), BOLD)
        canvas.text(row, 2 + len(prefix), shown + "▏", width - len(prefix), ACCENT)
    elif session.message:
        canvas.text(row, 2, session.message, width)
    elif getattr(session.backend, "action_busy", False):
        canvas.text(row, 2, f"{controls.running or 'Working'}…", width, ACCENT)


def render(session: Session, terminal: Terminal, size: Size) -> None:
    canvas = Canvas(size)
    controls = session.controls
    if too_small(size):
        canvas.text(1, 1, f"q quit | Resize {MINIMUM[0]}x{MINIMUM[1]}", size.columns)
        terminal.draw("".join(canvas.parts), None, None, size)
        return

    summary = " · ".join(part for part in (session.status, f"{len(session.all_pins)} wallpapers") if part)
    if controls.mode == "framing":
        canvas.text(1, 2, "m:swap-mode 0:reset-legacy ]:disp", size.columns - 2, ACCENT)
    else:
        canvas.text(1, 2, "Pinwall", 7, ACCENT)
        if size.columns - len(summary) - 1 > 10:
            canvas.text(1, size.columns - len(summary), summary, len(summary), DIM if not session.paused else WARNING)

    body = Box(2, 1, size.columns, size.rows - 3)
    mode = controls.view
    image = None
    image_box = None
    if mode in TITLES:
        canvas.panel(body, TITLES[mode], active=True)
        draw_overlay(canvas, session, body)
    else:
        left = max(16, min(38, size.columns * 36 // 100))
        list_box = Box(body.row, 1, left, body.rows)
        preview_box = Box(body.row, left + 1, size.columns - left, body.rows)
        position = f"{session.index + 1} of {len(session.pins)}" if session.pins else "0"
        canvas.panel(list_box, f"Wallpapers · {position}" if left >= 26 else position, list_footer(session), active=True)
        draw_list(canvas, session, list_box)
        pin = session.pin
        heading = (f"Frame D{session.display}" if controls.mode == "framing" else
                   f"Focus D{session.display}" if controls.mode == "focus" else
                   "Original" if session.original else "Preview")
        if controls.mode not in ("focus", "framing") and session.display != 1:
            heading += f" D{session.display}"
        if pin is not None and getattr(pin, "showing", False):
            heading += " · on screen now"
        canvas.panel(preview_box, heading, preview_footer(session, preview_box.columns - 6))
        if pin is None:
            filtered = controls.search or controls.board or controls.favorites
            advice = ("Nothing matches. Esc shows all." if filtered
                      else "No wallpapers yet. Press + to add a board, or R to fetch the ones you have.")
            for offset, text in enumerate(wrap_prose(advice, preview_box.columns - 4)[:preview_box.rows - 2]):
                canvas.text(preview_box.row + 1 + offset, preview_box.column + 2, text, preview_box.columns - 4)
        if pin is not None:
            image_box = Box(preview_box.row + 1, preview_box.column + 1,
                            max(1, preview_box.columns - 2), max(1, preview_box.rows - 2))
            preview_bounds = getattr(terminal, "preview_bounds", None)
            if preview_bounds is not None:
                bounds = preview_bounds(image_box, size)
            elif getattr(terminal, "kitty", False):
                bounds = (min(1024, image_box.columns * 8), min(1024, round(image_box.rows * size.cell_aspect * 8)))
            else:
                bounds = (image_box.columns, image_box.rows * 2)
            image = session.preview(bounds)
            if session.first_frame:
                session.first_frame = False
                if session.preview_pending and session.worker.ready.wait(0.01):
                    image = session.preview(bounds)
            if image is None:
                canvas.text(image_box.row + image_box.rows // 2, image_box.column + 1,
                            "Loading preview…" if session.preview_pending else "No preview for this one",
                            image_box.columns - 2, DIM)

    message_row(canvas, session, size.rows - 1)
    if controls.mode == "framing":
        from wallpaper_tui_framing import current

        framing = current(session)
        hints = ("←→ width ↑↓ height ↵ ok Esc cancel" if framing is not None and framing.mode == "stretch"
                 else "←→ ratio ↑↓ zoom ↵ ok Esc cancel")
        canvas.text(size.rows, 2, hints, size.columns - 2)
    elif controls.mode == "focus":
        canvas.text(size.rows, 2, "↑↓←→ move 0 reset ↵ ok Esc back", size.columns - 2)
    elif controls.mode in PROMPTS and session.message:
        canvas.text(size.rows, 2, session.message, size.columns - 2, WARNING)
    else:
        canvas.hints(size.rows, fit_hints(HINTS[controls.mode], ALWAYS.get(controls.mode, ()), size.columns - 2))
    terminal.draw("".join(canvas.parts), image, image_box if image is not None else None, size)
