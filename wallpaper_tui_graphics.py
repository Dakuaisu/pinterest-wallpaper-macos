from __future__ import annotations

import base64
import io
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from PIL import Image

ImageMode = Literal["blocks", "kitty", "iterm"]
PREVIEW_PIXEL_CAP: Final = 1024
CELL_QUERY: Final = "\x1b]1337;ReportCellSize\x07"
_CELL_REPLY: Final = b"\x1b]1337;ReportCellSize="
_CELL_FIELDS: Final = re.compile(rb"([0-9]{1,4}(?:\.[0-9]{1,6})?);([0-9]{1,4}(?:\.[0-9]{1,6})?)(?:;([0-9]{1,4}(?:\.[0-9]{1,6})?))?")
_KITTY_REPLY: Final = b"\x1b_G"
_CUBE = tuple(sum(value >= boundary for boundary in (48, 115, 155, 195, 235)) for value in range(256))


@dataclass(frozen=True)
class Box:
    row: int
    column: int
    columns: int
    rows: int

    def clipped_to(self, columns: int, rows: int) -> Box | None:
        row, column = max(1, self.row), max(1, self.column)
        width = min(columns + 1, self.column + self.columns) - column
        height = min(rows + 1, self.row + self.rows) - row
        return Box(row, column, width, height) if width > 0 and height > 0 else None


def direct_graphics_allowed(environment: Mapping[str, str]) -> bool:
    term = environment.get("TERM", "")
    return (not environment.get("TMUX") and not environment.get("STY")
            and term not in ("dumb", "screen", "tmux") and not term.startswith(("screen-", "tmux-"))
            and environment.get("PINTEREST_WALLPAPER_IMAGE") != "blocks")


def kitty_supported(environment: Mapping[str, str]) -> bool:
    window = environment.get("KITTY_WINDOW_ID", "")
    return (direct_graphics_allowed(environment) and environment.get("TERM") == "xterm-kitty"
            and window.isascii() and window.isdecimal() and bool(window.strip("0"))
            and environment.get("TERM_PROGRAM") not in ("vscode", "Apple_Terminal"))


def native_graphics(environment: Mapping[str, str]) -> ImageMode | None:
    """The real-image protocol of a terminal that identifies itself, without asking it."""
    program = environment.get("TERM_PROGRAM", "")
    if not direct_graphics_allowed(environment) or program in ("vscode", "Apple_Terminal"):
        return None
    if kitty_supported(environment) or program == "ghostty" or environment.get("TERM") == "xterm-ghostty":
        return "kitty"
    if program in ("iTerm.app", "WezTerm") or environment.get("LC_TERMINAL") == "iTerm2":
        return "iterm"
    return None


def vscode_probe_allowed(environment: Mapping[str, str]) -> bool:
    return direct_graphics_allowed(environment) and environment.get("TERM_PROGRAM") == "vscode"


def opaque_rgb(image: Image.Image) -> Image.Image:
    """Composite transparency on black without copying already-opaque RGB images."""
    if image.mode == "RGB" and "transparency" not in image.info:
        return image
    rgba = image.convert("RGBA")
    background = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
    background.alpha_composite(rgba)
    return background.convert("RGB")


def half_blocks(image: Image.Image, row: int, column: int, truecolor: bool = True) -> str:
    """Encode an RGB image, two vertical pixels per terminal cell."""
    pixels = image.load()
    parts = []
    for y in range(0, image.height, 2):
        parts.append(f"\x1b[{row + y // 2};{column}H")
        previous = None
        for x in range(image.width):
            top = pixels[x, y]
            bottom = pixels[x, y + 1]
            if truecolor:
                colors = (top, bottom)
            else:
                colors = tuple(16 + 36 * _CUBE[color[0]] + 6 * _CUBE[color[1]] + _CUBE[color[2]]
                               for color in (top, bottom))
            if colors != previous:
                if truecolor:
                    parts.append(f"\x1b[38;2;{top[0]};{top[1]};{top[2]}m"
                                 f"\x1b[48;2;{bottom[0]};{bottom[1]};{bottom[2]}m")
                else:
                    parts.append(f"\x1b[38;5;{colors[0]}m\x1b[48;5;{colors[1]}m")
                previous = colors
            parts.append("▀")
        parts.append("\x1b[0m")
    return "".join(parts)


_QUARTERS = " ▘▝▀▖▌▞▛▗▚▐▜▄▙▟█"
_FLAT = 24


def quarter_blocks(image: Image.Image, row: int, column: int, truecolor: bool = True) -> str:
    """Encode an RGB image, two by two pixels per terminal cell, with the best two colours for each cell.

    A cell can only show two colours, so its four pixels are split into a lighter and a darker
    group; a cell with little contrast keeps exact top and bottom colours instead.
    """
    pixels = image.load()
    parts = []
    for y in range(0, image.height - 1, 2):
        parts.append(f"\x1b[{row + y // 2};{column}H")
        previous = None
        for x in range(0, image.width - 1, 2):
            cell = (pixels[x, y], pixels[x + 1, y], pixels[x, y + 1], pixels[x + 1, y + 1])
            spans = [max(pixel[channel] for pixel in cell) - min(pixel[channel] for pixel in cell) for channel in range(3)]
            channel = spans.index(max(spans))
            if spans[channel] < _FLAT:
                mask = 3
            else:
                middle = (max(pixel[channel] for pixel in cell) + min(pixel[channel] for pixel in cell)) / 2
                mask = sum(1 << index for index, pixel in enumerate(cell) if pixel[channel] > middle)
            front = [pixel for index, pixel in enumerate(cell) if mask >> index & 1]
            back = [pixel for index, pixel in enumerate(cell) if not mask >> index & 1]
            colors = tuple(tuple(sum(pixel[part] for pixel in group) // len(group) for part in range(3))
                           for group in (front, back))
            if not truecolor:
                colors = tuple(16 + 36 * _CUBE[color[0]] + 6 * _CUBE[color[1]] + _CUBE[color[2]] for color in colors)
            if colors != previous:
                if truecolor:
                    (red, green, blue), (back_red, back_green, back_blue) = colors
                    parts.append(f"\x1b[38;2;{red};{green};{blue}m\x1b[48;2;{back_red};{back_green};{back_blue}m")
                else:
                    parts.append(f"\x1b[38;5;{colors[0]}m\x1b[48;5;{colors[1]}m")
                previous = colors
            parts.append(_QUARTERS[mask])
        parts.append("\x1b[0m")
    return "".join(parts)


class PngCache:
    """Cache one encoded frame, comparing pixels so in-place edits remain visible."""

    def __init__(self) -> None:
        self.signature: tuple[tuple[int, int], bytes] | None = None
        self.encoded = ""

    def encode(self, image: Image.Image) -> str:
        rgb = opaque_rgb(image)
        if max(rgb.size) > PREVIEW_PIXEL_CAP:
            rgb = rgb.copy()
            rgb.thumbnail((PREVIEW_PIXEL_CAP, PREVIEW_PIXEL_CAP), Image.Resampling.LANCZOS)
        signature = (rgb.size, rgb.tobytes())
        if signature != self.signature:
            with io.BytesIO() as buffer:
                rgb.save(buffer, format="PNG", compress_level=1)
                self.encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
            self.signature = signature
        return self.encoded


def kitty_image(image: Image.Image, box: Box, image_id: int, cache: PngCache | None = None) -> str:
    """Transmit a bounded PNG with a private image ID and no reply."""
    payload = (cache if cache is not None else PngCache()).encode(image)
    chunks = [payload[index:index + 4096] for index in range(0, len(payload), 4096)]
    parts = [f"\x1b[{box.row};{box.column}H"]
    for index, chunk in enumerate(chunks):
        more = int(index < len(chunks) - 1)
        control = f"a=T,f=100,t=d,i={image_id},c={box.columns},r={box.rows},C=1,q=2," if index == 0 else ""
        parts.append(f"\x1b_G{control}m={more};{chunk}\x1b\\")
    return "".join(parts)


def iterm_image(payload: str, box: Box) -> str:
    decoded_size = len(payload) // 4 * 3 - (len(payload) - len(payload.rstrip("=")))
    return (f"\x1b[{box.row};{box.column}H\x1b]1337;File=size={decoded_size};inline=1;"
            f"width={box.columns};height={box.rows};preserveAspectRatio=0:{payload}\x07")


def _cell_pixels(fields: bytes) -> tuple[float, float] | None:
    match = _CELL_FIELDS.fullmatch(fields)
    if match is None:
        return None
    height, width = float(match[1]), float(match[2])
    scale = float(match[3]) if match[3] is not None else 1.0
    if 1 <= width <= 512 and 1 <= height <= 512 and 0.5 <= scale <= 8:
        return width * scale, height * scale
    return None


class GraphicsReplies:
    """Remove this query's replies while buffering fragmented terminal sequences."""

    def __init__(self, image_id: int) -> None:
        self.image_id = str(image_id).encode("ascii")
        self.buffer = bytearray()
        self.cell_pixels: tuple[float, float] | None = None

    def feed(self, data: bytes) -> tuple[bytes, bool | None]:
        self.buffer.extend(data)
        ordinary = bytearray()
        result = None
        while self.buffer:
            start = self.buffer.find(b"\x1b")
            if start < 0:
                ordinary.extend(self.buffer)
                self.buffer.clear()
                break
            ordinary.extend(self.buffer[:start])
            del self.buffer[:start]
            if _KITTY_REPLY.startswith(self.buffer) or _CELL_REPLY.startswith(self.buffer):
                break
            is_cell = self.buffer.startswith(_CELL_REPLY)
            if not is_cell and not self.buffer.startswith(_KITTY_REPLY):
                ordinary.append(self.buffer[0])
                del self.buffer[0]
                continue
            prefix = len(_CELL_REPLY if is_cell else _KITTY_REPLY)
            terminator = re.search(rb"[\x07\x1b]" if is_cell else rb"\x1b", self.buffer[prefix:])
            if terminator is None:
                if len(self.buffer) > 256:
                    self.buffer.clear()
                break
            end = prefix + terminator.start()
            if terminator.group() == b"\x1b" and end + 1 == len(self.buffer):
                break
            length = 1 if terminator.group() == b"\x07" else 2 if self.buffer[end + 1] == 92 else 0
            frame = bytes(self.buffer[prefix:end])
            del self.buffer[:end + length]
            if length and is_cell:
                self.cell_pixels = _cell_pixels(frame) or self.cell_pixels
            elif length:
                header, separator, status = frame.partition(b";")
                if separator and header == b"i=" + self.image_id:
                    result = bool(result or status == b"OK")
        return bytes(ordinary), result
