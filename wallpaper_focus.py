from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TypedDict

from PIL import Image

Point = tuple[float, float]
Size = tuple[int, int]
CropBox = tuple[float, float, float, float]


class CropSettings(TypedDict, total=False):
    fit: str
    crop: str
    max_upscale: float
    rotate: int
    crop_focus: Sequence[float] | None


def parse_crop_focus(value) -> Point | None:
    """Read a normalized pair from JSON state or an in-memory draft."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    x, y = value
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1 for v in (x, y)):
        return None
    return float(x or 0), float(y or 0)


def focus_point_rotated(point: Sequence[float], angle: int) -> Point:
    """Map an original-image (x, y), normalized from the top left, through a clockwise quarter turn."""
    x, y = point
    return {0: (x, y), 90: (1 - y, x), 180: (1 - x, 1 - y), 270: (y, 1 - x)}[angle % 360]


def cover_crop(
    img: Image.Image, target_size: Size, settings: CropSettings, *,
    smart_centering: Callable[[Image.Image, Size], Point], output_size: Size | None = None,
) -> CropBox | None:
    """Return None for fit; output_size preserves legacy framing when preview dimensions round."""
    width, height = img.size
    cover = max(target_size[0] / width, target_size[1] / height)
    if settings.get("fit", "cover") != "cover" or cover > (settings.get("max_upscale") or float("inf")):
        return None

    focus = settings.get("crop_focus")
    crop_size = target_size if focus is not None else (output_size or target_size)
    ratio = crop_size[0] / crop_size[1]
    source_ratio = width / height
    # Match Pillow's zero-bleed fit arithmetic, including the exact-aspect case.
    crop_width = ratio * height if source_ratio > ratio else width
    crop_height = width / ratio if source_ratio < ratio else height
    slack_x, slack_y = width - crop_width, height - crop_height

    if focus is not None:
        x, y = focus_point_rotated(focus, settings.get("rotate", 0))
        left = max(0.0, min(slack_x, x * width - crop_width / 2))
        top = max(0.0, min(slack_y, y * height - crop_height / 2))
        return left, top, min(width, left + crop_width), min(height, top + crop_height)

    x, y = smart_centering(img, target_size) if settings.get("crop") == "smart" else (0.5, 0.5)
    left, top = slack_x * x, slack_y * y
    return left, top, left + crop_width, top + crop_height
