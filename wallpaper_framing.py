"""Validated per-pin framing and bounded rendering geometry."""

from __future__ import annotations

from typing import NamedTuple

from wallpaper_focus import focus_point_rotated, parse_crop_focus


class Framing(NamedTuple):
    mode: str
    aspect: str = "display"
    zoom: int = 100
    width: int = 100
    height: int = 100


ASPECTS = {"1:1": 1, "4:3": 4 / 3, "3:2": 3 / 2, "16:9": 16 / 9,
           "9:16": 9 / 16, "21:9": 21 / 9}


def parse_framing(value) -> Framing | None:
    """Parse a saved JSON mapping or an already constructed framing value."""
    if isinstance(value, Framing):
        value = value._asdict()
    if not isinstance(value, dict) or set(value) - set(Framing._fields):
        return None
    mode = value.get("mode")
    aspect = value.get("aspect", "display")
    numbers = [value.get(name, 100) for name in ("zoom", "width", "height")]
    if (not isinstance(mode, str) or mode not in ("proportional", "stretch")
            or not isinstance(aspect, str) or aspect not in (*ASPECTS, "display", "original")
            or not all(type(number) is int and 25 <= number <= 400 for number in numbers)):
        return None
    return Framing(mode, aspect, *numbers)


class Geometry(NamedTuple):
    source: tuple[float, float, float, float]
    destination: tuple[int, int, int, int]


def geometry(source_size, target_size, settings, output_size=None) -> Geometry:
    """Map the visible source to output pixels without resizing the offscreen area."""
    framing = parse_framing(settings["framing"])
    if framing is None:
        raise ValueError("invalid framing")
    sw, sh = source_size
    tw, th = target_size
    ow, oh = output_size or target_size
    if framing.mode == "stretch":
        fw, fh = tw, th
        photo_w, photo_h = tw * framing.width / 100, th * framing.height / 100
    else:
        ratio = (tw / th if framing.aspect == "display" else sw / sh if framing.aspect == "original"
                 else ASPECTS[framing.aspect])
        fw, fh = min(tw, th * ratio), min(th, tw / ratio)
        scale = max(fw / sw, fh / sh) * framing.zoom / 100
        photo_w, photo_h = sw * scale, sh * scale

    focus = parse_crop_focus(settings.get("crop_focus"))
    x, y = focus_point_rotated(focus, settings.get("rotate", 0)) if focus else (0.5, 0.5)

    def axis(frame, target, photo, point, output):
        frame_start = (target - frame) / 2
        if photo >= frame:
            start = max(frame_start + frame - photo, min(frame_start, target / 2 - point * photo))
        else:
            start = (target - photo) / 2
        lo = max(frame_start, start, 0)
        hi = min(frame_start + frame, start + photo, target)
        first = max(0, min(output - 1, round(lo * output / target)))
        last = max(first + 1, min(output, round(hi * output / target)))
        return start, first, last

    left, x0, x1 = axis(fw, tw, photo_w, x, ow)
    top, y0, y1 = axis(fh, th, photo_h, y, oh)
    # Calculate the source intersection from unrounded reference-space boundaries.
    sx0 = max(0.0, (max((tw - fw) / 2, left, 0) - left) * sw / photo_w)
    sy0 = max(0.0, (max((th - fh) / 2, top, 0) - top) * sh / photo_h)
    sx1 = min(sw, (min((tw + fw) / 2, left + photo_w, tw) - left) * sw / photo_w)
    sy1 = min(sh, (min((th + fh) / 2, top + photo_h, th) - top) * sh / photo_h)
    return Geometry((sx0, sy0, sx1, sy1), (x0, y0, x1, y1))
