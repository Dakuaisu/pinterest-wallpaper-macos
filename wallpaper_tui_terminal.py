from __future__ import annotations

import fcntl
import os
import select
import signal
import struct
import sys
import termios
import time
import tty
import unicodedata
from dataclasses import dataclass
from typing import TypeVar

from PIL import Image

from wallpaper_tui_graphics import (
    CELL_QUERY,
    PREVIEW_PIXEL_CAP,
    Box,
    GraphicsReplies,
    ImageMode,
    PngCache,
    half_blocks,
    iterm_image,
    kitty_image,
    native_graphics,
    opaque_rgb,
    quarter_blocks,
    vscode_probe_allowed,
)
from wallpaper_tui_keys import text_key

ESC = "\x1b"
RESET = ESC + "[0m"
ALT_ENTER = ESC + "[?1049h"
ALT_EXIT = ESC + "[?1049l"
ERASE = ESC + "[2J"
CSI_LIMIT = 64
TerminalType = TypeVar("TerminalType", bound="Terminal")


@dataclass(frozen=True)
class Size:
    columns: int
    rows: int
    cell_aspect: float
    cell_width: float = 0.0
    cell_height: float = 0.0


class Terminated(BaseException):
    pass


def _terminate(signum: int, frame: object) -> None:
    raise Terminated()


def clip_text(text: str, width: int) -> str:
    """Remove terminal controls and clip by display cells, not code points."""
    result = []
    used = 0
    for char in text:
        category = unicodedata.category(char)
        if category.startswith("C"):
            continue
        cells = 0 if category in ("Mn", "Me") else (2 if unicodedata.east_asian_width(char) in "WF" else 1)
        if used + cells > width:
            break
        if cells or result:
            result.append(char)
        used += cells
    return "".join(result)


def _image_box(image: Image.Image, box: Box, cell_aspect: float) -> Box:
    ratio = image.width / image.height
    width = max(1, min(box.columns, round(box.rows * cell_aspect * ratio)))
    height = max(1, min(box.rows, round(width / (cell_aspect * ratio))))
    return Box(box.row + (box.rows - height) // 2, box.column + (box.columns - width) // 2, width, height)


def fit_image(image: Image.Image, columns: int, rows: int, cell_aspect: float, across: int = 1) -> Image.Image:
    """Fit image to cell bounds, with `across` pixels per cell horizontally and two vertically."""
    placed = _image_box(image, Box(0, 0, columns, rows), cell_aspect)
    return opaque_rgb(image).resize((placed.columns * across, placed.rows * 2), Image.Resampling.LANCZOS)


class Terminal:
    """Own terminal modes, buffered input, and this session's inline image."""

    def __init__(self) -> None:
        self.input_fd = sys.stdin.fileno()
        self.output_fd = sys.stdout.fileno()
        self.pending = bytearray()
        self.image_mode: ImageMode = native_graphics(os.environ) or "blocks"
        self.quarters = os.environ.get("PINTEREST_WALLPAPER_IMAGE") != "half"
        self.probe_allowed = vscode_probe_allowed(os.environ)
        if self.probe_allowed and os.environ.get("PINTEREST_WALLPAPER_IMAGE") == "iterm":
            self.image_mode = "iterm"
        self.reported_cell: tuple[float, float] | None = None
        self.image_id = int.from_bytes(os.urandom(4), "big") or 1
        self.replies: GraphicsReplies | None = None
        self.png_cache = PngCache()
        self.truecolor = (os.environ.get("TERM_PROGRAM") != "Apple_Terminal"
                          or os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit"))
        self.shown_mode: ImageMode = "blocks"
        self.screen_entered = False
        self.saved_termios = None
        self.saved_signals: dict[int, object] = {}

    @property
    def kitty(self) -> bool:
        return self.image_mode == "kitty"

    @kitty.setter
    def kitty(self, enabled: bool) -> None:
        self.image_mode = "kitty" if enabled else "blocks"

    def write(self, text: str) -> None:
        data = text.encode("utf-8")
        while data:
            written = os.write(self.output_fd, data)
            data = data[written:]

    def __enter__(self: TerminalType) -> TerminalType:  # noqa: PYI019 - Python 3.9 has no typing.Self
        self.saved_termios = termios.tcgetattr(self.input_fd)
        ready = False
        try:
            for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
                self.saved_signals[signum] = signal.signal(signum, _terminate)
            tty.setcbreak(self.input_fd, when=termios.TCSANOW)
            # Ctrl-Z would stop the process with the alternate screen and hidden cursor still active.
            attributes = termios.tcgetattr(self.input_fd)
            attributes[6][termios.VSUSP] = os.fpathconf(self.input_fd, "PC_VDISABLE")
            termios.tcsetattr(self.input_fd, termios.TCSANOW, attributes)
            if self.image_mode == "blocks" and self.replies is None and self.probe_allowed:
                self.image_mode = self._probe_graphics()
            self.screen_entered = True
            self.write(ALT_ENTER + ESC + "[?25l" + ERASE)
            ready = True
            return self
        finally:
            if not ready:
                self.restore()

    def restore(self) -> None:
        try:
            if self.screen_entered:
                self.write(self.delete_image(exiting=True) + RESET + ESC + "[?25h" + ALT_EXIT)
                self.screen_entered = False
        finally:
            try:
                if self.saved_termios is not None:
                    termios.tcsetattr(self.input_fd, termios.TCSADRAIN, self.saved_termios)
            finally:
                while self.saved_signals:
                    signum, handler = self.saved_signals.popitem()
                    signal.signal(signum, handler)

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.restore()

    def _probe_graphics(self) -> ImageMode:
        probe_id = self.image_id % 0xFFFFFFFF + 1
        replies = self.replies = GraphicsReplies(probe_id)
        deadline = time.monotonic() + 0.1
        self.write(f"{ESC}_Ga=q,t=d,f=24,s=1,v=1,i={probe_id};AAAA{ESC}\\" + CELL_QUERY)
        kitty_rejected = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([self.input_fd], [], [], remaining)
            if not ready:
                break
            data = os.read(self.input_fd, 4096)
            if not data:
                break
            ordinary, answer = replies.feed(data)
            self.pending.extend(ordinary)
            if time.monotonic() >= deadline:
                break
            self.reported_cell = replies.cell_pixels
            if answer:
                return "kitty"
            kitty_rejected |= answer is False
            if kitty_rejected and self.reported_cell is not None:
                break
        return "iterm" if self.reported_cell is not None else "blocks"

    def size(self) -> Size:
        data = fcntl.ioctl(self.input_fd, termios.TIOCGWINSZ, bytes(8))
        rows, columns, pixel_width, pixel_height = struct.unpack("HHHH", data)
        rows, columns = max(1, rows), max(1, columns)
        width, height = self.reported_cell or (
            (pixel_width / columns, pixel_height / rows) if pixel_width and pixel_height else (0.0, 0.0)
        )
        return Size(columns, rows, height / width if width else 2.0, width, height)

    def preview_bounds(self, box: Box, size: Size) -> tuple[int, int]:
        """Pixel budget for the drawable preview pane, not the full terminal."""
        pane = box.clipped_to(size.columns, size.rows)
        if pane is None:
            return 1, 1
        columns, rows = pane.columns, pane.rows
        if self.image_mode == "blocks":
            return columns * (2 if self.quarters else 1), rows * 2
        width = size.cell_width or 12.0
        height = size.cell_height or width * size.cell_aspect
        return (max(1, min(PREVIEW_PIXEL_CAP, round(columns * width))),
                max(1, min(PREVIEW_PIXEL_CAP, round(rows * height))))

    def delete_image(self, *, exiting: bool = False) -> str:
        shown_mode, self.shown_mode = self.shown_mode, "blocks"
        if shown_mode == "kitty":
            return f"{ESC}_Ga=d,d=I,i={self.image_id},q=2;{ESC}\\"
        if shown_mode == "iterm":
            # xterm's image addon purges alternate-buffer tiles on buffer exit, not ED alone.
            return "\x18" + ERASE if exiting else ALT_EXIT + ALT_ENTER
        return ""

    def draw(self, content: str, image: Image.Image | None, box: Box | None, size: Size) -> None:
        pane = box.clipped_to(size.columns, size.rows) if box is not None else None
        shown_mode: ImageMode = "blocks"
        frame = ""
        if image is not None and pane is not None:
            placed = _image_box(image, pane, size.cell_aspect)
            if self.kitty:
                frame = kitty_image(image, placed, self.image_id, self.png_cache)
                shown_mode = "kitty"
            elif self.image_mode == "iterm":
                frame = iterm_image(self.png_cache.encode(image), placed)
                shown_mode = "iterm"
            else:
                across = 2 if self.quarters else 1
                fitted = fit_image(image, pane.columns, pane.rows, size.cell_aspect, across)
                encode = quarter_blocks if self.quarters else half_blocks
                frame = encode(fitted, placed.row, placed.column, self.truecolor)
        clear = self.delete_image()
        self.shown_mode = shown_mode
        self.write(clear + ERASE + content + frame + RESET)

    def _next_key(self) -> str | None:
        arrows = {65: "up", 66: "down", 67: "right", 68: "left", 72: "home", 70: "end"}
        paging = {b"5": "pgup", b"6": "pgdn", b"1": "home", b"7": "home", b"4": "end", b"8": "end"}
        while self.pending:
            first = self.pending[0]
            if first == 27:
                if len(self.pending) < 2:
                    return None
                if self.pending[1] not in (ord("["), ord("O")):
                    del self.pending[0]
                    return "esc"
                end = 2
                while end < len(self.pending) and 32 <= self.pending[end] <= 63:
                    end += 1
                if end == len(self.pending):
                    if end <= CSI_LIMIT:
                        return None
                    del self.pending[:end]
                    continue
                if not 64 <= self.pending[end] <= 126:
                    del self.pending[:end]
                    continue
                final = self.pending[end]
                parameters = bytes(self.pending[2:end])
                del self.pending[:end + 1]
                if final in arrows:
                    return arrows[final]
                if final == ord("~") and parameters in paging:
                    return paging[parameters]
                continue
            key = text_key(self.pending)
            if key is not None:
                return key
            if self.pending and self.pending[0] == 27:
                continue
            return None
        return None

    def read_key(self, timeout: float = 0.15) -> str | None:
        key = self._next_key()
        if key is not None:
            return key
        ready, _, _ = select.select([self.input_fd], [], [], timeout)
        if not ready:
            if not self.pending and self.replies is not None and self.replies.buffer == b"\x1b":
                self.replies.buffer.clear()
                return "esc"
            if self.pending == bytearray(b"\x1b"):
                self.pending.clear()
                return "esc"
            return None
        data = os.read(self.input_fd, 4096)
        if not data:
            return "eof"
        if self.replies is not None:
            data, _ = self.replies.feed(data)
        self.pending.extend(data)
        return self._next_key()
