#!/usr/bin/env python3

import contextlib
import copy
import fcntl
import hashlib
import html
import io
import json
import math
import os
import plistlib
import random
import re
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import warnings
from concurrent.futures import ThreadPoolExecutor

warnings.filterwarnings(
    "ignore",
    message="urllib3 v2 only supports OpenSSL",
)

import requests
from PIL import Image, ImageFilter, ImageOps, ImageStat
from requests.adapters import HTTPAdapter, Retry

import wallpaper_focus
import wallpaper_framing
import wallpaper_refresh
from wallpaper_http import DeadlineExceeded, get_response, is_board_host

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGE_DIR = os.path.join(BASE_DIR, "images")
UPSCALED_DIR = os.path.join(BASE_DIR, "upscaled")
WALLPAPERS_DIR = os.path.join(BASE_DIR, "wallpapers")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
STATE_FILE = os.path.join(BASE_DIR, ".state.json")
STATE_LOCK = os.path.join(BASE_DIR, ".state.lock")
REFRESH_LOCK = os.path.join(BASE_DIR, ".refresh.lock")
PW = os.path.join(BASE_DIR, "pw")

LABEL = "io.github.dakuaisu.pinterest-wallpaper"
LEGACY_LABEL = "com.pinterest.wallpaper"
LAUNCH_AGENTS = os.path.expanduser("~/Library/LaunchAgents")

STATE_VERSION = 2
# Baked into every render filename; bump it when render output changes for the same settings.
RENDER_VERSION = 2
HISTORY_LIMIT = 50
FALLBACK_SIZE = (2560, 1600)
MIN_SOURCE_SIDE = 300
DOWNLOAD_WORKERS = 6
BOARD_HTTP_BUDGET = 60
BOARD_PAGE_MAX_BYTES = 4 * 1024 * 1024
FEED_MAX_BYTES = 2 * 1024 * 1024
IMAGE_MAX_BYTES = 20 * 1024 * 1024
MAX_DOWNLOAD_PIXELS = 24_000_000
REFRESH_TIMEOUT = 120
MAX_LAUNCH_INTERVAL_SECONDS = (1 << 63) - 1
ALERT_AFTER_FAILURES = 3
PLACEHOLDER_BOARD = "YOUR_USERNAME"
# Pin IDs become filenames: plain digits, or the newer opaque "e1_..." IDs.
# No dots or slashes, and no leading dash, so a key is always a safe filename.
PIN_ID = r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}"
# Version 1 keyed banned pins by md5(url); those aren't pins Pinterest knows.
LEGACY_KEY = r"[0-9a-f]{32}"
UPSCALES_PER_TICK = 2
ORIENTATION_TIMEOUT = 30
AI_UPSCALE_ABOVE = 1.5
RENDER_LOOKAHEAD = 5
DOCTOR_ENLARGEMENT_WARNING = 2

DEFAULT_CONFIG = {
    "boards": [
        "https://www.pinterest.com/YOUR_USERNAME/YOUR_BOARD/"
    ],
    "interval_minutes": 15,
    "refresh_hours": 24,
    "retry_hours": 1,
    "max_pins": 250,
    "skip_video": True,
    "skip_upscale_over": 0,
    "resolution": "auto",
    "fit_mode": "cover",
    "fill_pins": [],
    "crop": "smart",
    "max_upscale": 0,
    "backdrop": "blur",
    "sharpen": True,
    "menubar_gradient": False,
    "upscaler": "",
    "upscaler_model": "realesrgan-x4plus",
    "orientation_model": "",
    "different_per_display": False,
    "favorite_weight": 2,
    "mood": "off",
    "night_hours": [19, 7],
    "skip_when_locked": True,
    "notifications": True,
    "new_pins_first": True,
}

DEFAULT_STATE = {
    "version": STATE_VERSION,
    "last_refresh_ok": 0,
    "last_refresh_attempt": 0,
    "refresh_pending": False,
    "board_fails": {},
    "board_names": [],
    "pins": {},
    "seen": [],
    "deck": [],
    "history": [],
    "banned": [],
    "favorites": [],
    "rotations": {},
    "fill_overrides": {},
    "crop_focus": {},
    "framing": {},
    "orientation_cache": {},
    "orientation_retry_after": 0,
    "screens": [],
    "last_change": 0,
    "paused_until": 0,
    "on_screen": [],
    "current": [],
    "current_screens": [],
    "listings": {},
    "disabled_boards": [],
}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/139.0 Safari/537.36"
)

PAGE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

IMAGE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.pinterest.com/",
}

# Pinterest rejects feed requests without these ("Invalid Resource Request").
API_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "X-Pinterest-AppState": "active",
    "X-Pinterest-PWS-Handler": "www/[username]/[slug].js",
}

FEED_API = "https://www.pinterest.com/resource/BoardFeedResource/get/"

NETWORK_ERRORS = (requests.ConnectionError, requests.Timeout)

FORMAT_EXT = {"JPEG": ".jpg", "MPO": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif"}

ROTATE_STEPS = {"cw": 90, "ccw": 270, "180": 180}

TRANSPOSE_CW = {
    90: Image.Transpose.ROTATE_270,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90,
}
focus_point_rotated = wallpaper_focus.focus_point_rotated


def make_session():

    session = requests.Session()
    session.mount(
        "https://",
        HTTPAdapter(
            max_retries=Retry(
                total=3,
                connect=3,
                read=0,
                status=0,
                redirect=0,
                backoff_factor=0.5,
                status_forcelist=(),
                respect_retry_after_header=False,
            )
        ),
    )
    return session


SESSION = make_session()
_thread_local = threading.local()


def worker_session():

    if not hasattr(_thread_local, "session"):
        _thread_local.session = make_session()

    return _thread_local.session


class Offline(Exception):
    pass


class StaleBan(ValueError):
    pass


class BadSource(Exception):

    def __init__(self, key, reason):
        super().__init__(reason)
        self.key = key


class WallpaperTransactionError(RuntimeError):

    def __init__(self, errors, screens):
        super().__init__("; ".join(str(error) for error in errors) or "wallpaper transaction failed")
        self.screens = screens if isinstance(screens, list) else []


# =========================
# PLUMBING
# =========================

def _stamp():

    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(message):

    print(f"{_stamp()}  {message}", flush=True)


def log_error(message):

    print(f"{_stamp()}  {message}", file=sys.stderr, flush=True)


def short(error, limit=200):

    text = str(error).strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "..."


def clean_text(text):

    return " ".join("".join(ch if ch.isprintable() else " " for ch in str(text)).split())


def menu_text(text):

    # SwiftBar/xbar read "|" as the start of item options, and leading dashes as separators or submenus.
    return clean_text(text).replace("|", "/").lstrip("-").strip() or "(untitled)"


def atomic_write(path, write):

    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")

    try:
        with os.fdopen(fd, "wb") as handle:
            write(handle)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)

    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def save_json(path, data):

    atomic_write(path, lambda handle: handle.write(json.dumps(data, indent=2).encode()))


def save_jpeg(img, path, icc=None):

    extra = {"icc_profile": icc} if icc else {}
    atomic_write(path, lambda handle: img.save(handle, "JPEG", quality=95, **extra))


def remove_quietly(path):

    with contextlib.suppress(FileNotFoundError):
        os.remove(path)


def fmt_time(stamp):

    if not stamp:
        return "never"

    try:
        local = time.localtime(stamp)
        delta = int(time.time()) - int(stamp)
    except (OverflowError, OSError, TypeError, ValueError):
        return "unknown time"

    if delta < 3600:
        ago = f"{delta // 60}m ago"
    elif delta < 86400:
        ago = f"{delta // 3600}h ago"
    else:
        ago = f"{delta // 86400}d ago"

    return f"{time.strftime('%Y-%m-%d %H:%M', local)}  ({ago})"


def pin_url(key):

    is_pin = isinstance(key, str) and re.fullmatch(PIN_ID, key) and not re.fullmatch(LEGACY_KEY, key)

    return f"https://www.pinterest.com/pin/{key}/" if is_pin else None


# =========================
# CONFIG
# =========================

def _number(minimum):

    return lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and _finite_number(v, minimum)


def _hours(minimum):

    def valid(value):
        if not _number(minimum)(value):
            return False
        return _finite_number(value * 3600, minimum * 3600)

    return valid


def launch_interval_seconds(minutes):

    if not _finite_number(minutes, 1):
        return None
    try:
        seconds = round(minutes * 60)
    except OverflowError:
        return None
    return seconds if 1 <= seconds <= MAX_LAUNCH_INTERVAL_SECONDS else None


def _integer(minimum, maximum=None):

    return lambda v: (
        isinstance(v, int)
        and not isinstance(v, bool)
        and v >= minimum
        and (maximum is None or v <= maximum)
    )


def _choice(*options):

    return lambda v: v in options


def _boolean(v):

    return isinstance(v, bool)


CONFIG_RULES = {
    "boards": (
        lambda v: isinstance(v, list) and bool(v) and all(isinstance(b, str) and b.strip() for b in v),
        "a list of board URLs",
    ),
    "interval_minutes": (lambda v: launch_interval_seconds(v) is not None, "a number of minutes that fits launchd's interval range"),
    "refresh_hours": (_hours(0.1), "a number of hours representable in seconds"),
    "retry_hours": (_hours(0.1), "a number of hours representable in seconds"),
    "max_pins": (_integer(1), "a whole number, 1 or more"),
    "skip_video": (_boolean, "true or false"),
    "skip_upscale_over": (lambda v: _number(0)(v) and (v == 0 or v >= 1), "0 (off) or a number from 1 up"),
    "resolution": (
        lambda v: isinstance(v, str) and bool(re.fullmatch(r"auto|0*[1-9]\d*\s*x\s*0*[1-9]\d*", v.strip().lower())),
        '"auto" or "WIDTHxHEIGHT"',
    ),
    "fit_mode": (_choice("cover", "blur"), '"cover" or "blur"'),
    "crop": (_choice("center", "smart"), '"center" or "smart"'),
    "max_upscale": (lambda v: _number(0)(v) and (v == 0 or v >= 1), "0 (no limit) or a number from 1 up"),
    "backdrop": (_choice("blur", "color"), '"blur" or "color"'),
    "sharpen": (_boolean, "true or false"),
    "menubar_gradient": (_boolean, "true or false"),
    "upscaler": (lambda v: isinstance(v, str), 'a path to realesrgan-ncnn-vulkan, or ""'),
    "upscaler_model": (lambda v: isinstance(v, str) and bool(v.strip()), "a model name"),
    "different_per_display": (_boolean, "true or false"),
    "favorite_weight": (_integer(1, 10), "a whole number from 1 to 10"),
    "mood": (_choice("off", "appearance", "night"), '"off", "appearance" or "night"'),
    "night_hours": (
        lambda v: isinstance(v, list) and len(v) == 2 and all(_integer(0, 23)(h) for h in v),
        "[start_hour, end_hour], e.g. [19, 7]",
    ),
    "skip_when_locked": (_boolean, "true or false"),
    "notifications": (_boolean, "true or false"),
    "new_pins_first": (_boolean, "true or false"),
}

OPTIONAL_CONFIG_RULES = {
    "orientation_model": (lambda v: isinstance(v, str), 'a local model path, or ""'),
    "fill_pins": (
        lambda v: isinstance(v, list) and all(isinstance(pin, str) and re.fullmatch(PIN_ID, pin) for pin in v),
        "a list of safe pin IDs",
    ),
}


def load_config(create=True):

    config = copy.deepcopy(DEFAULT_CONFIG)
    problems = []

    if not os.path.exists(CONFIG_FILE):
        if create:
            save_json(CONFIG_FILE, config)
        return config, problems

    try:
        with open(CONFIG_FILE) as handle:
            loaded = json.load(handle)
    except (OSError, ValueError) as e:
        return config, [f"config.json is unreadable ({short(e)}); using defaults."]

    if not isinstance(loaded, dict):
        return config, ["config.json must hold a JSON object; using defaults."]

    for key, value in loaded.items():

        rule = CONFIG_RULES.get(key) or OPTIONAL_CONFIG_RULES.get(key)

        if rule is None:
            problems.append(f"config.json: unknown key {key!r} ignored.")
            continue

        if key == "boards" and isinstance(value, str):
            value = [value]

        check, expected = rule

        if check(value):
            config[key] = value
        else:
            problems.append(
                f"config.json: {key} should be {expected}; using {json.dumps(DEFAULT_CONFIG[key])}."
            )

    return config, problems


def forced_size(config):

    match = re.fullmatch(r"(\d+)\s*x\s*(\d+)", config["resolution"].strip().lower())

    return (int(match.group(1)), int(match.group(2))) if match else None


# =========================
# STATE
# =========================

def migrate_state(old):

    # v1 stored render filenames in deck/history and md5(url) stems in banned.
    return {
        "version": STATE_VERSION,
        "banned": [b for b in old.get("banned", []) if isinstance(b, str)] if isinstance(old.get("banned", []), list) else [],
        "board_names": old.get("board_names", []),
    }


def load_state():

    with file_lock(STATE_LOCK):
        return _load_state_unlocked()


def _load_state_unlocked(repair=True):

    state = copy.deepcopy(DEFAULT_STATE)

    try:
        with open(STATE_FILE) as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict):
            raise ValueError("not a JSON object")  # noqa: TRY004 - caught below with the other corrupt-state errors

    except FileNotFoundError:
        return state

    except (OSError, ValueError) as e:
        if not repair:
            raise ValueError(f".state.json is unreadable ({short(e)})") from e
        backup = f"{STATE_FILE}.corrupt-{int(time.time())}"
        suffix = 0
        while os.path.lexists(backup):
            suffix += 1
            backup = f"{STATE_FILE}.corrupt-{int(time.time())}-{suffix}"
        os.replace(STATE_FILE, backup)
        log_error(
            f".state.json is unreadable ({short(e)}); moved it to"
            f" {os.path.basename(backup)} and started fresh."
        )
        return state

    version = loaded.get("version", 1)
    if type(version) is not int or version < 1 or version > STATE_VERSION:
        raise ValueError(f"unsupported state version {version!r}; supported versions are 1 and {STATE_VERSION}")

    if version == 1:
        loaded = migrate_state(loaded)

    for key, value in loaded.items():
        if key in state:
            parsed = _valid_state_field(key, value)
            if parsed is not False:
                state[key] = value if parsed is True else parsed

    return state


def _finite_number(value, minimum=0):

    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value >= minimum
    except OverflowError:
        return False


def _valid_timestamp(value):

    if not _finite_number(value):
        return False
    try:
        time.localtime(value)
    except (OverflowError, OSError, ValueError):
        return False
    return True


def _valid_state_field(key, value):

    strings = {"board_names", "seen", "deck", "banned", "favorites", "on_screen"}
    if key in strings:
        if not isinstance(value, list):
            return False
        if key in {"seen", "deck", "favorites"}:
            return [item for item in value if isinstance(item, str) and re.fullmatch(PIN_ID, item)]
        if key == "banned":
            return [item for item in value if isinstance(item, str) and (re.fullmatch(PIN_ID, item) or re.fullmatch(LEGACY_KEY, item))]
        return [item for item in value if isinstance(item, str) and (key != "on_screen" or os.path.basename(item) == item)]
    if key in {"last_refresh_ok", "last_refresh_attempt", "last_change", "orientation_retry_after"}:
        return _valid_timestamp(value)
    if key == "paused_until":
        return _valid_timestamp(value) or value == -1
    if key == "refresh_pending":
        return isinstance(value, bool)
    if key == "history":
        if not isinstance(value, list):
            return False
        rows = [[item for item in row if isinstance(item, str) and re.fullmatch(PIN_ID, item)]
                for row in value if isinstance(row, list)]
        return [row for row in rows if row]
    if key == "current":
        return [pin if pin is None or isinstance(pin, str) and re.fullmatch(PIN_ID, pin) else None
                for pin in value] if isinstance(value, list) else False
    if key == "current_screens":
        return [screen for screen in value if screen is None or isinstance(screen, (str, int)) and not isinstance(screen, bool)] if isinstance(value, list) else False
    if key == "rotations":
        return {pin: angle for pin, angle in value.items() if isinstance(pin, str) and re.fullmatch(PIN_ID, pin)
                and isinstance(angle, int) and not isinstance(angle, bool) and angle in (0, 90, 180, 270)} if isinstance(value, dict) else False
    if key == "fill_overrides":
        return {pin: fill for pin, fill in value.items() if isinstance(pin, str) and re.fullmatch(PIN_ID, pin)
                and isinstance(fill, bool)} if isinstance(value, dict) else False
    if key == "crop_focus":
        if not isinstance(value, dict):
            return False
        return {pin: list(point) for pin, raw in value.items()
                if isinstance(pin, str) and re.fullmatch(PIN_ID, pin)
                and (point := wallpaper_focus.parse_crop_focus(raw)) is not None}
    if key == "framing":
        if not isinstance(value, dict):
            return False
        return {pin: framing._asdict() for pin, raw in value.items()
                if isinstance(pin, str) and re.fullmatch(PIN_ID, pin)
                and (framing := wallpaper_framing.parse_framing(raw)) is not None}
    if key == "orientation_cache":
        return {pin: record for pin, record in value.items() if isinstance(pin, str) and re.fullmatch(PIN_ID, pin)
                and isinstance(record, dict) and "angle" in record and isinstance(record.get("identity"), str)
                and (record.get("angle") is None or type(record.get("angle")) is int and record["angle"] in (0, 90, 180, 270))
                and _finite_number(record.get("score"))} if isinstance(value, dict) else False
    if key == "pins":
        if not isinstance(value, dict):
            return False
        return {pin: _valid_pin_record(record) for pin, record in value.items()
                if isinstance(pin, str) and re.fullmatch(PIN_ID, pin) and _valid_pin_record(record) is not False}
    if key == "screens":
        if not isinstance(value, list):
            return False
        return [screen for screen in value if isinstance(screen, dict) and isinstance(screen.get("name"), str)
                and isinstance(screen.get("width"), int) and not isinstance(screen.get("width"), bool) and screen["width"] > 0
                and isinstance(screen.get("height"), int) and not isinstance(screen.get("height"), bool) and screen["height"] > 0
                and (screen.get("id") is None or isinstance(screen.get("id"), (str, int)) and not isinstance(screen.get("id"), bool))]
    if key == "board_fails":
        return isinstance(value, dict) and {url: fail for url, fail in value.items() if isinstance(url, str)
                and isinstance(fail, dict) and _finite_number(fail.get("count"), 1) and isinstance(fail.get("error"), str)}
    if key == "disabled_boards":
        return list(dict.fromkeys(url for url in value if _safe_board_url(url))) if isinstance(value, list) else False
    if key == "listings":
        return {url: record for url, record in value.items() if _safe_board_url(url) and _valid_listing_record(record)} if isinstance(value, dict) else False
    return True


def _valid_pin_record(record):

    if (not isinstance(record, dict) or not isinstance(record.get("width"), int) or isinstance(record.get("width"), bool)
            or not isinstance(record.get("height"), int) or isinstance(record.get("height"), bool)
            or record["width"] < 1 or record["height"] < 1):
        return False
    normalized = {"width": record["width"], "height": record["height"]}
    if _finite_number(record.get("luma")):
        normalized["luma"] = record["luma"]
    if isinstance(record.get("avg"), str) and re.fullmatch(r"#[0-9a-fA-F]{6}", record["avg"]):
        normalized["avg"] = record["avg"]
    if isinstance(record.get("board"), str) and _safe_board_url(record["board"]):
        normalized["board"] = record["board"]
    if isinstance(record.get("boards"), list):
        normalized["boards"] = list(dict.fromkeys(board for board in record["boards"] if _safe_board_url(board)))
    if isinstance(record.get("url"), str) and is_pinimg(record["url"]):
        normalized["url"] = record["url"]
    color = record.get("color")
    if isinstance(color, str) and (not color or re.fullmatch(r"#[0-9a-fA-F]{6}", color)):
        normalized["color"] = color
    if isinstance(record.get("video"), bool):
        normalized["video"] = record["video"]
    return normalized


def _safe_board_url(url):

    if not isinstance(url, str):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return False
    return (parts.scheme == "https" and port in (None, 443) and parts.username is None
            and parts.password is None and is_board_host(host))


def _valid_listing_pin(pin):

    if not isinstance(pin, dict) or not isinstance(pin.get("key"), str) or not re.fullmatch(PIN_ID, pin["key"]):
        return False
    if not isinstance(pin.get("url"), str) or not is_pinimg(pin["url"]):
        return False
    if not _safe_board_url(pin.get("board")) or not isinstance(pin.get("color"), str) or not isinstance(pin.get("video"), bool):
        return False
    for field in ("width", "height"):
        if not isinstance(pin.get(field), int) or isinstance(pin[field], bool) or pin[field] < 0:
            return False
    fallbacks = pin.get("fallbacks", [])
    return isinstance(fallbacks, list) and all(isinstance(url, str) and is_pinimg(url) for url in fallbacks)


def _valid_listing_record(record):

    if not isinstance(record, dict) or not isinstance(record.get("pins"), list) or not all(
        _valid_listing_pin(pin) for pin in record["pins"]
    ):
        return False
    stamp = record.get("stamp")
    if not isinstance(stamp, dict):
        return False
    if not isinstance(stamp.get("max_pins"), int) or isinstance(stamp.get("max_pins"), bool) or stamp["max_pins"] < 1:
        return False
    if not isinstance(stamp.get("first_page"), list) or not all(
        isinstance(pin, str) and re.fullmatch(PIN_ID, pin) for pin in stamp["first_page"]
    ):
        return False
    count = stamp.get("pin_count")
    modified = stamp.get("modified")
    if count is not None and (not isinstance(count, int) or isinstance(count, bool) or count < 0):
        return False
    return modified is None or isinstance(modified, str) or _finite_number(modified)


def save_state(state):

    save_json(STATE_FILE, state)


@contextlib.contextmanager
def file_lock(path, wait=True):

    with open(path, "a") as handle:

        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

        except BlockingIOError:

            if not wait:
                yield False
                return

            if sys.stdout.isatty():
                print("Waiting for another pw command to finish...", flush=True)

            fcntl.flock(handle, fcntl.LOCK_EX)

        yield True


@contextlib.contextmanager
def locked_state():

    with file_lock(STATE_LOCK):

        state = _load_state_unlocked()

        save = True
        try:
            yield state
        except StaleBan:
            save = False
            raise
        finally:
            if save:
                save_state(state)


def current_pins(state):

    if state["current"]:
        return list(state["current"])
    previous = list(state["history"][-1]) if state["history"] else []
    return previous * len(state["screens"]) if len(previous) == 1 and len(state["screens"]) > 1 else previous


def pin_on_display(state, display):

    pins = current_pins(state)

    if not pins:
        return None

    if display > max(len(pins), len(state["screens"])):
        raise ValueError(f"there's no display {display} (see ./pw status)")

    if display > len(pins):
        return None
    return pins[display - 1]


def is_paused(state):

    until = state["paused_until"]

    return until == -1 or until > time.time()


# =========================
# MACOS
# =========================

JXA_SCREENS = """
ObjC.import("AppKit");
var screens = $.NSScreen.screens, out = [];
for (var i = 0; i < screens.count; i++) {
  var s = screens.objectAtIndex(i), f = s.frame, k = s.backingScaleFactor;
  out.push({
    id: ObjC.unwrap(s.deviceDescription.objectForKey("NSScreenNumber")),
    name: ObjC.unwrap(s.localizedName),
    width: Math.round(f.size.width * k),
    height: Math.round(f.size.height * k)
  });
}
JSON.stringify(out);
"""

JXA_SET_WALLPAPERS = """
ObjC.import("AppKit");
function run(argv) {
  var ws = $.NSWorkspace.sharedWorkspace, screens = $.NSScreen.screens, plan = [], errors = [], changed = [];
  function screenID(sc) { return String(ObjC.unwrap(sc.deviceDescription.objectForKey("NSScreenNumber"))); }
  function imagePath(url) { return url ? String(ObjC.unwrap(url.path)) : ""; }
  for (var j = 0; j + 1 < argv.length; j += 2) {
    var target = argv[j], hit = false;
    for (var i = 0; i < screens.count; i++) {
      var sc = screens.objectAtIndex(i), id = screenID(sc);
      if (target !== "*" && id !== target) continue;
      hit = true;
      var path = argv[j + 1];
      if (!$.NSFileManager.defaultManager.fileExistsAtPath($(path))) errors.push("missing render " + path);
      plan.push({screen: sc, id: id, path: path, url: $.NSURL.fileURLWithPath(path),
                 oldURL: ws.desktopImageURLForScreen(sc), oldOptions: ws.desktopImageOptionsForScreen(sc)});
    }
    if (!hit) errors.push("no display with id " + target);
  }
  if (errors.length) return JSON.stringify({ok:false, errors:errors, screens:[]});
  for (var p = 0; p < plan.length; p++) {
    var item = plan[p], err = Ref();
    changed.push(item);
    if (!ws.setDesktopImageURLForScreenOptionsError(item.url, item.screen, item.oldOptions, err)) {
      errors.push(item.id + ": " + (err[0] ? ObjC.unwrap(err[0].localizedDescription) : "failed"));
      break;
    }
  }
  if (errors.length) {
    for (var r = changed.length - 1; r >= 0; r--) {
      var prior = changed[r], rollbackError = Ref();
      if (!ws.setDesktopImageURLForScreenOptionsError(prior.oldURL, prior.screen, prior.oldOptions, rollbackError))
        errors.push("rollback " + prior.id + ": " + (rollbackError[0] ? ObjC.unwrap(rollbackError[0].localizedDescription) : "failed"));
    }
  }
  var outcomes = [];
  for (var q = 0; q < plan.length; q++) {
    var targetItem = plan[q], actual = ws.desktopImageURLForScreen(targetItem.screen);
    outcomes.push({id:targetItem.id, path:imagePath(actual)});
  }
  return JSON.stringify({ok:errors.length === 0, errors:errors, screens:outcomes});
}
"""

JXA_CURRENT_WALLPAPER = """
ObjC.import("AppKit");
ObjC.unwrap($.NSWorkspace.sharedWorkspace.desktopImageURLForScreen($.NSScreen.mainScreen).path);
"""


def osascript(args, timeout=30):

    result = subprocess.run(
        ["osascript", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"osascript exited with status {result.returncode}")

    return result.stdout.strip()


def detect_screens():

    try:
        found = json.loads(osascript(["-l", "JavaScript", "-e", JXA_SCREENS], timeout=15))
        screens = [
            {
                "id": int(s["id"]),
                "name": str(s.get("name") or "Display"),
                "width": int(s["width"]),
                "height": int(s["height"]),
            }
            for s in found
        ]
        return screens or None

    except Exception as e:
        log_error(f"Display detection failed ({short(e)}).")
        return None


def current_screens(config, state, detect=True):

    screens = detect_screens() if detect else None

    if screens:
        state["screens"] = screens
    else:
        screens = state["screens"] or [
            {"id": None, "name": "Display", "width": FALLBACK_SIZE[0], "height": FALLBACK_SIZE[1]}
        ]

    size = forced_size(config)

    if size:
        screens = [dict(s, width=size[0], height=size[1]) for s in screens]

    return screens


def set_wallpapers(assignment):

    args = []

    for screen, path in assignment:

        # NSWorkspace crashes osascript outright when handed a missing file.
        if not os.path.isfile(path):
            raise RuntimeError(f"missing render {path}")

        args += ["*" if screen.get("id") is None else str(screen["id"]), path]

    result = osascript(["-l", "JavaScript", "-e", JXA_SET_WALLPAPERS, *args])
    if isinstance(result, str) and result:
        try:
            transaction = json.loads(result)
        except ValueError as error:
            raise RuntimeError("osascript returned an invalid wallpaper transaction result") from error
        if isinstance(transaction, dict) and transaction.get("ok") is False:
            raise WallpaperTransactionError(transaction.get("errors", []), transaction.get("screens", []))


def screen_locked():

    try:
        raw = subprocess.run(
            ["ioreg", "-n", "Root", "-d1", "-a"],
            check=False,
            capture_output=True,
            timeout=10,
        ).stdout
        root = plistlib.loads(raw)

    except Exception:
        return False

    for node in root if isinstance(root, list) else [root]:
        for user in node.get("IOConsoleUsers") or []:
            if user.get("kCGSSessionOnConsoleKey") and user.get("CGSSessionScreenIsLocked"):
                return True

    return False


def dark_mode():

    result = subprocess.run(
        ["defaults", "read", "-g", "AppleInterfaceStyle"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    return result.stdout.strip() == "Dark"


def notify(config, title, message):

    if not config["notifications"]:
        return

    with contextlib.suppress(Exception):
        osascript(
            [
                "-e", "on run argv",
                "-e", "display notification (item 1 of argv) with title (item 2 of argv)",
                "-e", "end run",
                message,
                title,
            ],
            timeout=15,
        )


def launchctl_interval(label):

    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"launchctl status probe failed for {label}: {short(error)}") from error

    if result.returncode != 0:
        return None

    match = re.search(r"run interval = (\d+) seconds", result.stdout)

    return int(match.group(1)) if match else 0


# =========================
# BOARD SCRAPING
# =========================

def parse_initial_props(page):

    match = re.search(
        r'<script[^>]*id="__PWS_INITIAL_PROPS__"[^>]*>(.*?)</script>',
        page,
        re.DOTALL,
    )

    if not match:
        raise RuntimeError("no __PWS_INITIAL_PROPS__ block in page HTML")

    payload = match.group(1).strip()

    # Pinterest serves this block entity-escaped on some responses and as raw
    # JSON on others. Unescaping raw JSON would turn a literal "&quot;" inside
    # a pin title into a bare quote, so only unescape once the raw parse fails.
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return json.loads(html.unescape(payload))


def is_pinimg(url):

    if not isinstance(url, str) or not url.startswith("https://"):
        return False

    host = urllib.parse.urlparse(url).hostname or ""

    return host == "pinimg.com" or host.endswith(".pinimg.com")


def pin_from_feed(item, board_url):

    if not isinstance(item, dict) or item.get("type") != "pin":
        return None

    pin_id = str(item.get("id") or "")
    images = item.get("images")

    if not re.fullmatch(PIN_ID, pin_id) or not isinstance(images, dict):
        return None

    for quality in ("orig", "736x", "474x"):

        entry = images.get(quality)
        url = entry.get("url") if isinstance(entry, dict) else entry

        if is_pinimg(url):
            size = entry if isinstance(entry, dict) else {}
            return {
                "key": pin_id,
                "url": url,
                "board": board_url,
                "color": item.get("dominant_color") or "",
                "width": _dimension(size.get("width")),
                "height": _dimension(size.get("height")),
                "video": bool(item.get("videos")),
            }

    return None


def _dimension(value):

    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def dedupe(pins):

    keys, urls, out = set(), set(), []

    for pin in pins:

        if pin["key"] in keys or pin["url"] in urls:
            continue

        keys.add(pin["key"])
        urls.add(pin["url"])
        out.append(pin)

    return out


def more_pages(bookmark):

    return _bookmark_state(bookmark) is True


def _bookmark_state(bookmark):

    if bookmark is None:
        return False
    if not isinstance(bookmark, str):
        return None
    return bookmark not in ("", "-end-")


def paginate(board_url, feed_key, bookmark, pins, max_pins, deadline=None):

    path = urllib.parse.urlparse(board_url).path

    try:
        options = dict(json.loads(feed_key))
    except (TypeError, ValueError):
        log_error(f"{board_url}: unrecognised feed options, so only the first page was read.")
        return False

    headers = dict(API_HEADERS, **{"X-Pinterest-Source-Url": path})

    state = _bookmark_state(bookmark)
    if state is None:
        return False
    bookmarks = set()
    while state and len(pins) < max_pins:

        if not isinstance(bookmark, str) or bookmark in bookmarks or len(bookmarks) >= 100:
            log_error(f"{board_url}: stopped paging after {len(pins)} pins (Pinterest repeated a page).")
            return False
        bookmarks.add(bookmark)

        data = json.dumps(
            {"options": dict(options, bookmarks=[bookmark]), "context": {}},
            separators=(",", ":"),
        )

        try:
            response = get_response(
                SESSION,
                FEED_API,
                params={"source_url": path, "data": data},
                headers=headers,
                kind="feed",
                max_bytes=FEED_MAX_BYTES,
                deadline=deadline,
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            body = payload["resource_response"]
            if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                raise TypeError("unexpected feed response")
            # The last page has no bookmark of its own; Pinterest marks the end in the echoed request options.
            following = body["bookmark"] if "bookmark" in body else payload["resource"]["options"]["bookmarks"][0]
            next_state = _bookmark_state(following)
            if next_state is None:
                raise ValueError("unexpected feed bookmark")

        except Exception as e:
            log_error(f"{board_url}: stopped paging after {len(pins)} pins ({short(e)}).")
            return False

        for item in body["data"]:
            pin = pin_from_feed(item, board_url)
            if not isinstance(item, dict) or item.get("type") == "pin" and not pin:
                log_error(f"{board_url}: stopped paging after {len(pins)} pins (a pin couldn't be read).")
                return False
            if pin:
                pins.append(pin)
        pins[:] = dedupe(pins)
        bookmark = following
        state = next_state

    return True


def scrape_board(board_url, max_pins, previous=None, deadline=None):

    deadline = time.monotonic() + BOARD_HTTP_BUDGET if deadline is None else deadline
    response = get_response(SESSION, board_url, headers=PAGE_HEADERS, kind="board",
                            max_bytes=BOARD_PAGE_MAX_BYTES, deadline=deadline, timeout=30)
    response.raise_for_status()

    resources = parse_initial_props(response.text)["initialReduxState"]["resources"]

    feed_key, feed = None, None

    for key, value in resources.get("BoardFeedResource", {}).items():
        if isinstance(value, dict) and isinstance(value.get("data"), list):
            feed_key, feed = key, value
            break

    if feed is None:
        raise RuntimeError("board feed contained no pins")

    board = {}

    for value in resources.get("BoardResource", {}).values():
        if isinstance(value, dict) and isinstance(value.get("data"), dict) and value["data"]:
            board = value["data"]
            break

    bookmark_status = _bookmark_state(feed.get("nextBookmark")) if "nextBookmark" in feed else None
    if not feed["data"] and not (
        type(board.get("pin_count")) is int and board["pin_count"] == 0 and bookmark_status is False
    ):
        raise RuntimeError("board feed contained no pins")

    parsed = [(item, pin_from_feed(item, board_url)) for item in feed["data"]]
    pins = dedupe([pin for _, pin in parsed if pin])
    # Enough to tell that a board hasn't changed without paging through all of it again.
    stamp = {
        "pin_count": board.get("pin_count"),
        "modified": board.get("board_order_modified_at"),
        "max_pins": max_pins,
        "first_page": [p["key"] for p in pins],
    }
    paged = bookmark_status is True and len(pins) < max_pins
    complete = bookmark_status is not None and not any(
        not isinstance(item, dict) or item.get("type") == "pin" and not pin for item, pin in parsed
    )
    reused = False

    if paged and previous and previous.get("stamp") == stamp:
        pins, reused = [dict(p) for p in previous["pins"]], True
    elif paged:
        complete = paginate(board_url, feed_key, feed["nextBookmark"], pins, max_pins, deadline) and complete

    return {
        "url": board_url,
        "name": clean_text(board.get("name") or "") or "Unknown",
        "pin_count": board.get("pin_count"),
        "pins": dedupe(pins)[:max_pins],
        "complete": complete,
        "paged": paged,
        "reused": reused,
        "stamp": stamp,
    }


def scrape_board_rss(board_url, max_pins, deadline=None):

    deadline = time.monotonic() + BOARD_HTTP_BUDGET if deadline is None else deadline
    response = get_response(SESSION, board_url.rstrip("/") + ".rss", headers=PAGE_HEADERS, kind="rss",
                            max_bytes=BOARD_PAGE_MAX_BYTES, deadline=deadline, timeout=30)
    response.raise_for_status()

    pins = []

    for item in re.findall(r"<item>(.*?)</item>", response.text, re.DOTALL):

        item = html.unescape(item)
        pin_id = re.search(rf"/pin/({PIN_ID})(?![A-Za-z0-9_-])", item)
        thumb = re.search(r"https://i\.pinimg\.com/\d+x/[0-9a-f/]+\.(?:jpg|png)", item)

        if pin_id and thumb:
            original = re.sub(r"/\d+x/", "/originals/", thumb.group(0), count=1)
            stem = original.rsplit(".", 1)[0]
            pins.append({
                "key": pin_id.group(1),
                "url": original,
                # Thumbnails are always .jpg; the original may be PNG or GIF, and 736x always exists.
                "fallbacks": [f"{stem}.{ext}" for ext in ("png", "gif", "jpg") if not original.endswith("." + ext)]
                + [re.sub(r"/\d+x/", "/736x/", thumb.group(0), count=1)],
                "board": board_url,
                "color": "",
                "width": 0,
                "height": 0,
                "video": False,
            })

    if not pins:
        raise RuntimeError("RSS feed contained no pins")

    title = re.search(r"<title>(.*?)</title>", response.text, re.DOTALL)

    return {
        "url": board_url,
        "name": (clean_text(html.unescape(title.group(1))) if title else "") or "Unknown",
        "pin_count": None,
        # The feed only carries recent pins, so it never counts as a full listing.
        "pins": dedupe(pins)[:max_pins],
        "complete": False,
        "paged": False,
        "reused": False,
        "stamp": None,
    }


def fetch_board(board_url, max_pins, previous=None):

    deadline = time.monotonic() + BOARD_HTTP_BUDGET

    try:
        return scrape_board(board_url, max_pins, previous, deadline)
    except DeadlineExceeded as e:
        # A server that answers too slowly is reachable, so this isn't the laptop being offline.
        raise requests.exceptions.RetryError(short(e)) from e
    except NETWORK_ERRORS as e:
        raise Offline(short(e)) from e
    except requests.exceptions.RetryError:
        # Rate limiting or an outage would hit the RSS feed just the same.
        raise
    except Exception as e:
        log_error(f"{board_url}: page scrape failed ({short(e)}); falling back to RSS.")

    try:
        return scrape_board_rss(board_url, max_pins, deadline)
    except DeadlineExceeded as e:
        raise requests.exceptions.RetryError(short(e)) from e
    except NETWORK_ERRORS as e:
        raise Offline(short(e)) from e


# =========================
# IMAGES
# =========================

def decode(source):

    with Image.open(source) as img:

        fmt = img.format
        # A CMYK or greyscale profile would be wrong once the pixels are RGB.
        icc = img.info.get("icc_profile") if img.mode in ("RGB", "RGBA") else None
        img = ImageOps.exif_transpose(img)

        if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            flat = Image.new("RGB", rgba.size, "black")
            flat.paste(rgba, mask=rgba.getchannel("A"))
            img = flat
        else:
            img = img.convert("RGB")

    return img, icc, fmt


def image_meta(img):

    small = img.resize((64, 64), Image.Resampling.BILINEAR)
    avg = small.resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))

    return {
        "width": img.width,
        "height": img.height,
        "luma": round(ImageStat.Stat(small.convert("L")).mean[0], 1),
        "avg": "#{:02x}{:02x}{:02x}".format(*avg[:3]),
    }


def source_files():

    return {
        name.split(".")[0]: name
        for name in os.listdir(IMAGE_DIR)
        if not name.startswith(".") and not name.endswith(".tmp")
    }


def download_source(pin):

    urls = [pin["url"], *pin.get("fallbacks", [])]
    deadline = time.monotonic() + 45

    for number, url in enumerate(urls, 1):
        response = get_response(worker_session(), url, headers=IMAGE_HEADERS, kind="image",
                                max_bytes=IMAGE_MAX_BYTES, deadline=deadline, timeout=30)
        if response.status_code not in (403, 404) or number == len(urls):
            break

    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "").lower()

    if not content_type.startswith("image/"):
        raise ValueError(f"not an image ({content_type or 'no content type'})")

    with Image.open(io.BytesIO(response.content)) as header:
        if header.width * header.height > MAX_DOWNLOAD_PIXELS:
            raise ValueError(f"image exceeds pixel limit ({header.width}x{header.height})")

    # A full decode, so a truncated body never makes it into the cache.
    img, _, fmt = decode(io.BytesIO(response.content))

    if min(img.size) < MIN_SOURCE_SIDE:
        raise ValueError(f"too small ({img.width}x{img.height})")

    filename = pin["key"] + FORMAT_EXT.get(fmt, "." + (fmt or "img").lower())
    atomic_write(os.path.join(IMAGE_DIR, filename), lambda handle: handle.write(response.content))

    return filename, image_meta(img)


def try_download(pin):

    try:
        return download_source(pin), None
    except Exception as e:
        return None, e


def oriented_size(meta, rotation):

    w, h = meta.get("width", 0), meta.get("height", 0)

    return (h, w) if rotation in (90, 270) else (w, h)


def enlargement(meta, rotation, size):

    w, h = oriented_size(meta, rotation)

    return max(size[0] / w, size[1] / h) if w and h else 0


def largest_screen(config, state):

    screens = current_screens(config, state, detect=False)

    return max(s["width"] for s in screens), max(s["height"] for s in screens)


def run_upscaler(binary, model, src, out):

    with tempfile.TemporaryDirectory() as tmp:

        img, icc, _ = decode(src)
        png_in = os.path.join(tmp, "in.png")
        png_out = os.path.join(tmp, "out.png")
        img.save(png_in)

        result = subprocess.run(
            [binary, "-i", png_in, "-o", png_out, "-n", model, "-s", "4"],
            check=False,
            cwd=os.path.dirname(os.path.abspath(binary)),
            capture_output=True,
            text=True,
            timeout=300,
        )

        if result.returncode != 0 or not os.path.exists(png_out):
            raise RuntimeError(short(result.stderr or f"exit status {result.returncode}"))

        with Image.open(png_out) as upscaled:
            save_jpeg(upscaled.convert("RGB"), out, icc)


_digest_cache = {}


def _file_digest(path):

    stat = os.stat(path)
    identity = (os.path.realpath(path), stat.st_ino, stat.st_size, stat.st_mtime_ns)
    if identity not in _digest_cache:
        _digest_cache[identity] = _hash_file(path)
    return _digest_cache[identity]


def _hash_file(path):

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def orientation_backend(config):
    model = config.get("orientation_model", "").strip()
    if not model:
        raise ValueError("orientation_model is empty; configure a local model first")
    model = os.path.abspath(os.path.join(os.path.dirname(CONFIG_FILE), os.path.expanduser(model)))
    try:
        import wallpaper_orientation
    except ImportError as error:
        raise ValueError(f"orientation backend unavailable: {short(error)}") from error
    try:
        stat = os.stat(model)
    except OSError as error:
        raise ValueError(f"orientation model unavailable: {short(error)}") from error
    token = [os.path.realpath(model), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
             wallpaper_orientation.CACHE_VERSION, wallpaper_orientation.MODEL_SHA256]
    return wallpaper_orientation, model, token


def orientation_identity(source, token):
    return json.dumps([_file_digest(source), token], separators=(",", ":"))


def effective_angle(key, config, state, files=None):
    if key in state["rotations"]:
        return state["rotations"][key]
    if not config.get("orientation_model"):
        return 0
    record = state.get("orientation_cache", {}).get(key)
    if not record:
        return 0
    try:
        _, _, token = orientation_backend(config)
        files = source_files() if files is None else files
        source = os.path.join(IMAGE_DIR, files[key])
        if record["identity"] == orientation_identity(source, token):
            return record["angle"] or 0
    except (ValueError, OSError, KeyError):
        return 0
    return 0


def orient_batch(config, snapshot, explicit=False):
    backend, model, token = orientation_backend(config)
    files = source_files()
    keys = list(dict.fromkeys(snapshot["deck"] + list(snapshot["pins"])))
    work = []
    for key in keys:
        if key not in snapshot["pins"] or key not in files or key in snapshot["banned"]:
            continue
        source = os.path.join(IMAGE_DIR, files[key])
        try:
            identity = orientation_identity(source, token)
        except OSError:
            continue
        if snapshot.get("orientation_cache", {}).get(key, {}).get("identity") != identity:
            work.append((key, source, identity))
        if len(work) == 4:
            break
    if not work:
        return 0
    try:
        predictions = backend.predict(model, [item[1] for item in work], timeout=ORIENTATION_TIMEOUT)
        if len(predictions) != len(work):
            raise backend.OrientationError("orientation backend returned the wrong number of results")
    except (backend.OrientationError, OSError) as error:
        if explicit:
            raise ValueError(f"orientation failed: {short(error)}") from error
        with locked_state() as state:
            state["orientation_retry_after"] = time.time() + 3600
        log_error(f"Orientation failed; retrying after one hour ({short(error)}).")
        return 0
    failed = False
    count = 0
    with locked_state() as state:
        try:
            _, _, current_token = orientation_backend(config)
        except ValueError:
            return 0
        current_files = source_files()
        for (key, source, identity), prediction in zip(work, predictions):
            if prediction is None:
                failed = True
            if key not in state["pins"]:
                continue
            try:
                if key not in current_files or os.path.join(IMAGE_DIR, current_files[key]) != source:
                    continue
                if orientation_identity(source, current_token) != identity:
                    continue
            except OSError:
                continue
            # An unreadable image is remembered as an abstention, or four of them would be rescanned forever.
            state["orientation_cache"][key] = (
                {"identity": identity, "angle": None, "score": 0.0} if prediction is None
                else {"identity": identity, "angle": prediction.angle, "score": prediction.score}
            )
            count += prediction is not None
        state["orientation_retry_after"] = 0
    if failed and explicit:
        raise ValueError("orientation could not read one or more images; successful results were saved")
    return count


def upscale_cache_paths(key, binary, model, source):

    executable = os.path.realpath(binary)
    stat = os.stat(executable)
    identity = json.dumps([executable, stat.st_size, stat.st_mtime_ns, _file_digest(executable), model,
                           os.path.realpath(source), _file_digest(source)], separators=(",", ":"))
    tag = hashlib.sha256(identity.encode()).hexdigest()[:20]
    prefix = os.path.join(UPSCALED_DIR, key + ".")
    return prefix + tag + ".jpg", prefix + tag + ".failed", prefix


def upscale_sources(config, state, files, limit):

    binary = config["upscaler"]

    if not binary:
        return

    if not os.access(binary, os.X_OK):
        log_error(f"Upscaler {binary} is not an executable file; skipping AI upscaling.")
        return

    target = largest_screen(config, state)
    banned = set(state["banned"])
    attempted = 0
    cached = {}

    for name in os.listdir(UPSCALED_DIR):
        cached.setdefault(name.split(".")[0], []).append(name)

    for key, meta in state["pins"].items():

        if attempted >= limit:
            break

        if key not in files or key in banned:
            continue

        source = os.path.join(IMAGE_DIR, files[key])
        out, failed, _ = upscale_cache_paths(key, binary, config["upscaler_model"], source)

        for name in cached.get(key, ()):
            if name not in (os.path.basename(out), os.path.basename(failed)):
                remove_quietly(os.path.join(UPSCALED_DIR, name))

        if os.path.exists(out) or os.path.exists(failed):
            continue

        if enlargement(meta, effective_angle(key, config, state, files), target) <= AI_UPSCALE_ABOVE:
            continue

        attempted += 1

        try:
            run_upscaler(binary, config["upscaler_model"], source, out)
            log(f"Upscaled pin {key} with {config['upscaler_model']}.")
        except Exception as e:
            reason = short(e)
            # Remembered, so one image the upscaler can't handle doesn't cost every tick a timeout.
            atomic_write(failed, lambda handle, reason=reason: handle.write(reason.encode()))
            log_error(f"Upscaling pin {key} failed ({reason}); delete upscaled/ to retry.")


# =========================
# RENDER
# =========================

def source_for(key, config, files):

    upscaled = None
    if config["upscaler"] and key in files and os.access(config["upscaler"], os.X_OK):
        try:
            upscaled = upscale_cache_paths(key, config["upscaler"], config["upscaler_model"],
                                           os.path.join(IMAGE_DIR, files[key]))[0]
        except OSError:
            upscaled = None

    if upscaled and os.path.exists(upscaled):
        return upscaled, True

    if key not in files:
        raise BadSource(key, "its image is no longer cached (removed from its board?)")

    return os.path.join(IMAGE_DIR, files[key]), False


def render_settings(key, config, state, ai, files=None):

    fill = state.get("fill_overrides", {}).get(key, key in config.get("fill_pins", []))
    settings = {
        "v": RENDER_VERSION,
        "fit": "cover" if fill else config["fit_mode"],
        "crop": config["crop"],
        "backdrop": config["backdrop"],
        "color": state["pins"].get(key, {}).get("color") if config["backdrop"] == "color" else None,
        "sharpen": config["sharpen"],
        "menubar": config["menubar_gradient"],
        "max_upscale": 0 if fill else config["max_upscale"],
        "rotate": effective_angle(key, config, state, files),
        "ai": config["upscaler_model"] if ai else "",
    }
    focus = wallpaper_focus.parse_crop_focus(state.get("crop_focus", {}).get(key))
    if focus is not None:
        settings["crop_focus"] = focus
    framing = wallpaper_framing.parse_framing(state.get("framing", {}).get(key))
    if framing is not None:
        settings["framing"] = framing._asdict()
    return settings


def settings_tag(settings):

    return hashlib.sha1(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:8]


def render_name(key, size, tag):

    return f"{key}.{size[0]}x{size[1]}.{tag}.jpg"


def expected_render(key, size, config, state, files):

    src, ai = source_for(key, config, files)
    settings = render_settings(key, config, state, ai, files)
    settings["source_identity"] = [os.path.basename(src), _file_digest(src)]

    return src, settings, os.path.join(WALLPAPERS_DIR, render_name(key, size, settings_tag(settings)))


def sharpen(img, radius=1.6):

    return img.filter(ImageFilter.UnsharpMask(radius=radius, percent=110, threshold=3))


def add_menubar_gradient(img, strength=135, minimum_band=48):

    width, height = img.size
    band = max(minimum_band, height // 20)

    column = Image.new("L", (1, band))

    for y in range(band):
        column.putpixel((0, y), int(strength * (1 - y / band)))

    mask = column.resize((width, band), Image.Resampling.BILINEAR)

    shaded = img.copy()
    shaded.paste(Image.new("RGB", (width, band), "black"), (0, 0), mask)

    return shaded


def make_backdrop(img, size, style, color, blur_radius=40):

    if style == "color":

        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color or ""):
            color = "#{:02x}{:02x}{:02x}".format(*img.resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))[:3])

        return Image.new("RGB", size, color)

    return ImageOps.fit(img, size, method=Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(blur_radius))


def smart_centering(img, size):

    target = size[0] / size[1]
    ratio = img.width / img.height

    if abs(ratio - target) < 0.01:
        return (0.5, 0.5)

    small = img.convert("L")
    small.thumbnail((256, 256))
    # The blur keeps small high-contrast text (timestamps, captions) from outweighing the subject.
    small = small.filter(ImageFilter.GaussianBlur(2))
    # Pillow copies the 1-pixel border through its 3x3 filters unfiltered, so it holds raw brightness.
    edges = small.filter(ImageFilter.FIND_EDGES).crop((1, 1, small.width - 1, small.height - 1))
    wide = ratio > target

    # Slide a screen-shaped window along the per-column (or per-row) edge profile; keep the busiest spot.
    if wide:
        profile = list(edges.resize((edges.width, 1), Image.Resampling.BOX).getdata())
        window = max(1, round(edges.height * target))
    else:
        profile = list(edges.resize((1, edges.height), Image.Resampling.BOX).getdata())
        window = max(1, round(edges.width / target))

    slack = len(profile) - window

    if slack <= 0:
        return (0.5, 0.5)

    sums = [0]

    for value in profile:
        sums.append(sums[-1] + value)

    def detail(start):
        return sums[start + window] - sums[start]

    best = max(range(slack + 1), key=detail)

    if detail(best) <= detail(slack // 2) * 1.02:
        return (0.5, 0.5)

    return (best / slack, 0.5) if wide else (0.5, best / slack)


def cover_crop_box(
    img: Image.Image, target_size: tuple[int, int], settings: wallpaper_focus.CropSettings,
) -> tuple[float, float, float, float]:
    """Return source pixel edges on the already-rotated image, or full bounds for fit."""
    if wallpaper_framing.parse_framing(settings.get("framing")) is not None:
        return wallpaper_framing.geometry(img.size, target_size, settings).source
    box = wallpaper_focus.cover_crop(img, target_size, settings, smart_centering=smart_centering)
    return box if box is not None else (0, 0, img.width, img.height)


def render_image(img, size, settings, color=None, reference_size=None):

    width, height = size
    reference_width, reference_height = reference_size or size
    preview_scale = min(width / reference_width, height / reference_height)

    if settings["rotate"]:
        img = img.transpose(TRANSPOSE_CW[settings["rotate"]])

    if wallpaper_framing.parse_framing(settings.get("framing")) is not None:
        geometry = wallpaper_framing.geometry(img.size, (reference_width, reference_height), settings, size)
        left, top, right, bottom = geometry.destination
        photo = img.resize((right - left, bottom - top), Image.Resampling.LANCZOS, box=geometry.source)
        if settings["sharpen"] and (reference_width > img.width or reference_height > img.height):
            photo = sharpen(photo) if reference_size is None else sharpen(photo, 1.6 * preview_scale)
        if (left, top, right, bottom) == (0, 0, width, height):
            out = photo
        else:
            out = (make_backdrop(img, size, settings["backdrop"], color) if reference_size is None else
                   make_backdrop(img, size, settings["backdrop"], color, 40 * preview_scale))
            out.paste(photo, (left, top))
        if settings["menubar"]:
            out = (add_menubar_gradient(out) if reference_size is None else
                   add_menubar_gradient(out, minimum_band=max(1, round(48 * preview_scale))))
        return out

    limit = settings["max_upscale"] or float("inf")
    box = wallpaper_focus.cover_crop(img, (reference_width, reference_height), settings,
                                    smart_centering=smart_centering, output_size=size)

    if box is not None:

        out = img.resize(size, Image.Resampling.LANCZOS, box=box)

        if settings["sharpen"] and (reference_width > img.width or reference_height > img.height):
            out = sharpen(out) if reference_size is None else sharpen(out, 1.6 * preview_scale)

    else:

        scale = min(reference_width / img.width, reference_height / img.height, limit)

        photo = img.resize(
            (max(1, round(img.width * scale * preview_scale)), max(1, round(img.height * scale * preview_scale))),
            Image.Resampling.LANCZOS,
        )

        # Only the photo is sharpened; the backdrop is meant to stay soft.
        if settings["sharpen"] and scale > 1:
            photo = sharpen(photo) if reference_size is None else sharpen(photo, 1.6 * preview_scale)

        if reference_size is None:
            out = make_backdrop(img, size, settings["backdrop"], color)
        else:
            out = make_backdrop(img, size, settings["backdrop"], color, 40 * preview_scale)
        out.paste(photo, ((width - photo.width) // 2, (height - photo.height) // 2))

    if settings["menubar"]:
        out = (add_menubar_gradient(out) if reference_size is None else
               add_menubar_gradient(out, minimum_band=max(1, round(48 * preview_scale))))

    return out


def ensure_render(key, size, config, state, files):

    src, settings, out = expected_render(key, size, config, state, files)

    if os.path.exists(out):
        return out

    try:
        img, icc, _ = decode(src)
    except Exception as e:
        if src.startswith(os.path.join(UPSCALED_DIR, key + ".")):
            remove_quietly(src)
            return ensure_render(key, size, config, state, files)
        raise BadSource(key, short(e)) from e

    save_jpeg(render_image(img, size, settings, state["pins"].get(key, {}).get("color")), out, icc)

    prefix = f"{key}.{size[0]}x{size[1]}."
    keep = {os.path.basename(out), *state["on_screen"]}

    for name in os.listdir(WALLPAPERS_DIR):
        if name.startswith(prefix) and name.endswith(".jpg") and name not in keep:
            remove_quietly(os.path.join(WALLPAPERS_DIR, name))

    return out


def render_for_screens(pins, screens, config, state, files):

    return [
        (screen, ensure_render(pins[i % len(pins)], (screen["width"], screen["height"]), config, state, files))
        for i, screen in enumerate(screens)
    ]


def forget_source(key, files):

    if key in files:
        remove_quietly(os.path.join(IMAGE_DIR, files.pop(key)))

    for name in os.listdir(UPSCALED_DIR):
        if name.startswith(key + "."):
            remove_quietly(os.path.join(UPSCALED_DIR, name))


def prune_dir(directory, keep):

    removed = 0
    stale_tmp = time.time() - 600

    for name in os.listdir(directory):

        path = os.path.join(directory, name)

        if name.startswith("."):
            continue

        if name.endswith(".tmp"):
            if os.path.getmtime(path) < stale_tmp:
                remove_quietly(path)
            continue

        if not keep(name):
            remove_quietly(path)
            removed += 1

    return removed


# =========================
# SYNC
# =========================

def sync_boards(config, snapshot):

    listings, failures, offline = [], {}, []
    retry = False

    disabled = set(snapshot.get("disabled_boards", ()))

    if disabled & set(config["boards"]):
        log(f"Skipped {len(disabled & set(config['boards']))} board(s) that are switched off.")

    for board_url in config["boards"]:

        # Never listed, so a switched-off board keeps its pins and images, like one that failed to load.
        if board_url in disabled:
            continue

        try:
            listing = fetch_board(board_url, config["max_pins"], snapshot["listings"].get(board_url))

        except Offline as e:
            offline.append(board_url)
            log(f"{board_url}: offline ({e}).")
            continue

        except requests.exceptions.RetryError as e:
            retry = True
            log(f"{board_url}: Pinterest is rate-limiting or unavailable ({short(e)}); will retry later.")
            continue

        except Exception as e:
            failures[board_url] = short(e)
            log_error(f"{board_url}: refresh failed ({failures[board_url]}).")
            continue

        listings.append(listing)

        if listing["reused"]:
            note = ", unchanged since the last refresh"
        elif not listing["complete"]:
            note = ", partial listing"
        else:
            note = ""

        log(f"Board: {listing['name']} ({listing['pin_count'] or '?'} pins, {len(listing['pins'])} listed{note})")

    if offline and not listings and not failures and not retry:
        raise Offline("no board could be reached")

    pins = dedupe([pin for listing in listings for pin in listing["pins"]])

    if config["skip_video"]:
        videos = sum(1 for pin in pins if pin["video"])
        pins = [pin for pin in pins if not pin["video"]]
        if videos:
            log(f"Skipped {videos} video pin(s).")

    limit = config["skip_upscale_over"]
    target = largest_screen(config, snapshot)

    def too_small(key, size):
        if not limit:
            return False
        if config.get("orientation_model") and key not in snapshot["rotations"]:
            return min(enlargement(size, 0, target), enlargement(size, 90, target)) > limit
        return enlargement(size, snapshot["rotations"].get(key, 0), target) > limit

    skipped = {pin["key"] for pin in pins if too_small(pin["key"], pin)}
    files = source_files()
    known = snapshot["pins"]
    metas = {}

    todo = [pin for pin in pins if pin["key"] not in files and pin["key"] not in skipped]
    failed = 0
    retained = set()

    with ThreadPoolExecutor(DOWNLOAD_WORKERS) as pool:

        for pin, (result, error) in zip(todo, pool.map(try_download, todo)):

            if error is not None:
                failed += 1
                retained.add(pin["key"])
                log_error(f"Pin {pin['key']}: download failed ({short(error)}).")
                continue

            filename, metas[pin["key"]] = result
            files[pin["key"]] = filename

    if todo:
        log(f"Downloaded {len(todo) - failed}/{len(todo)} new image(s).")

    available = {}

    for pin in pins:

        key = pin["key"]

        if key not in files or key in skipped:
            continue

        meta = metas.get(key)

        if meta is None and all(k in known.get(key, {}) for k in ("width", "height", "luma", "avg")):
            meta = {k: known[key][k] for k in ("width", "height", "luma", "avg")}

        if meta is None:
            try:
                meta = image_meta(decode(os.path.join(IMAGE_DIR, files[key]))[0])
            except Exception as e:
                log_error(f"Pin {key}: cached image is unreadable ({short(e)}); re-downloading next time.")
                forget_source(key, files)
                retained.add(key)
                retry = True
                continue

        # RSS pins arrive without a size, so they can only be judged once downloaded.
        if too_small(key, meta):
            skipped.add(key)
            continue

        available[key] = dict(meta, url=pin["url"], board=pin["board"], color=pin["color"] or meta["avg"])

    if skipped:
        log(f"Skipped {len(skipped)} low-resolution pin(s) that would need more than {limit}x enlargement.")

    return {
        "listings": listings,
        "failures": failures,
        "offline": offline,
        "pins": available,
        "retained": sorted(retained),
        "retry": retry or bool(failed),
    }


def sync_with_deadline(config, snapshot):

    paths = {name: globals()[name] for name in ("BASE_DIR", "IMAGE_DIR", "UPSCALED_DIR", "WALLPAPERS_DIR")}
    try:
        return wallpaper_refresh.run_sync(config, snapshot, paths, timeout=REFRESH_TIMEOUT)
    except wallpaper_refresh.RefreshOffline as e:
        raise Offline(str(e)) from e


def apply_sync(state, result, config):

    now = int(time.time())
    listings = result["listings"]

    legacy = {
        hashlib.md5(pin["url"].encode()).hexdigest(): pin["key"]
        for listing in listings
        for pin in listing["pins"]
    }
    state["banned"] = list(dict.fromkeys(legacy.get(b, b) for b in state["banned"]))

    # Pins from a board that couldn't be fully listed this time stay put, so a
    # failed or partial read never looks like the pins were removed.
    configured = set(config["boards"])
    fully_listed = {listing["url"] for listing in listings if listing["complete"]}

    listed = {}
    for listing in listings:
        for pin in listing["pins"]:
            listed.setdefault(pin["key"], set()).add(listing["url"])

    merged = {}
    for key, meta in state["pins"].items():
        previous = set(meta.get("boards", [meta.get("board")])) & configured
        memberships = (previous - fully_listed) | listed.get(key, set())
        if memberships and ((previous - fully_listed) or key in result.get("retained", ())):
            merged[key] = dict(meta, boards=sorted(memberships))
    merged.update(result["pins"])
    for key, meta in merged.items():
        previous = set(state["pins"].get(key, {}).get("boards", [meta.get("board")])) & configured
        meta["boards"] = sorted((previous - fully_listed) | listed.get(key, set()))

    seen = set(state["seen"])
    fresh = [key for key in result["pins"] if key not in seen] if seen else []

    state["pins"] = merged
    state["seen"] = sorted((seen & set(merged)) | set(result["pins"]))

    if fresh and config["new_pins_first"]:
        state["deck"] = fresh + [key for key in state["deck"] if key not in fresh]

    alerts = []
    names = {listing["url"]: listing["name"] for listing in listings}

    for listing in listings:
        state["board_fails"].pop(listing["url"], None)

    for url, error in result["failures"].items():

        streak = state["board_fails"].get(url, {}).get("count", 0) + 1
        state["board_fails"][url] = {"count": streak, "error": error}

        if streak == ALERT_AFTER_FAILURES:
            alerts.append(
                f"Couldn't read {url} {streak} times in a row ({error})."
                " The board may be private or deleted."
            )

    for url in list(state["board_fails"]):
        if url not in configured:
            del state["board_fails"][url]

    state["disabled_boards"] = [url for url in state["disabled_boards"] if url in configured]

    for listing in listings:
        if listing["paged"] and listing["complete"]:
            state["listings"][listing["url"]] = {"stamp": listing["stamp"], "pins": listing["pins"]}
        else:
            state["listings"].pop(listing["url"], None)

    for url in list(state["listings"]):
        if url not in configured:
            del state["listings"][url]

    if listings:
        state["last_refresh_ok"] = now
        state["board_names"] = [names[url] for url in config["boards"] if url in names]

    state["refresh_pending"] = bool(result.get("retry") or result["failures"] or result["offline"] or any(not listing["complete"] for listing in listings))

    keys = set(merged)
    files = source_files()
    sizes = {(s["width"], s["height"]) for s in current_screens(config, state, detect=False)}
    # Renders are cheap to redo, so only recent and upcoming pins keep theirs.
    recent = {key for entry in state["history"] for key in entry} | set(state["deck"][:RENDER_LOOKAHEAD])
    keep_renders = set(state["on_screen"]) | {
        os.path.basename(expected_render(key, size, config, state, files)[2])
        for key in keys & recent
        if key in files
        for size in sizes
    }

    pruned = (
        prune_dir(IMAGE_DIR, lambda name: name.split(".")[0] in keys)
        + prune_dir(UPSCALED_DIR, lambda name: bool(config["upscaler"]) and name.split(".")[0] in keys)
        + prune_dir(WALLPAPERS_DIR, lambda name: name in keep_renders)
    )

    if pruned:
        log(f"Pruned {pruned} stale file(s).")

    return fresh, alerts


def do_refresh(config):

    if any(PLACEHOLDER_BOARD in board for board in config["boards"]):
        log_error("config.json still has the placeholder board URL; add your own board to \"boards\".")
        return False

    with file_lock(REFRESH_LOCK, wait=False) as acquired:

        if not acquired:
            log("A refresh is already running; skipping.")
            return False

        with locked_state() as state:
            previous_attempt = state["last_refresh_attempt"]
            state["last_refresh_attempt"] = int(time.time())
            snapshot = copy.deepcopy(state)

        log("Refreshing from Pinterest...")

        try:
            result = sync_with_deadline(config, snapshot)

        except Offline as e:
            # Being offline says nothing about the boards, so retry on the next tick.
            with locked_state() as state:
                state["last_refresh_attempt"] = previous_attempt
                state["refresh_pending"] = True
            log(f"Offline ({e}); will retry on the next tick.")
            return False

        except (wallpaper_refresh.RefreshTimeout, wallpaper_refresh.RefreshWorkerError) as e:
            with locked_state() as state:
                state["refresh_pending"] = True
            log_error(f"Refresh worker failed ({short(e)}); retrying after {config['retry_hours']}h.")
            return False

        with locked_state() as state:
            fresh, alerts = apply_sync(state, result, config)
            where = ", ".join(state["board_names"]) or "your boards"

    for alert in alerts:
        notify(config, "Pinterest Wallpaper", alert)

    if fresh:
        notify(
            config,
            "Pinterest Wallpaper",
            f"{len(fresh)} new {'pin' if len(fresh) == 1 else 'pins'} on {where}",
        )

    return bool(result["listings"])


def refresh_due(config, state):

    now = time.time()

    def within(stamp, hours):
        return 0 <= now - stamp < hours * 3600

    if not state.get("refresh_pending") and available_pins(state) and within(state["last_refresh_ok"], config["refresh_hours"]):
        return False

    return not within(state["last_refresh_attempt"], config["retry_hours"])


# =========================
# ROTATION
# =========================

def available_pins(state, files=None):

    files = source_files() if files is None else files
    banned = set(state["banned"])
    disabled = set(state.get("disabled_boards", ()))

    def switched_on(meta):
        return not disabled or any(board not in disabled for board in meta.get("boards") or [meta.get("board")])

    return [key for key, meta in state["pins"].items() if key in files and key not in banned and switched_on(meta)]


def build_deck(pool, favorites, weight, avoid=()):

    shuffled = random.sample(pool, len(pool))
    favs = [key for key in shuffled if key in favorites]
    rest = [key for key in shuffled if key not in favorites]

    # Each favourite appears once per segment, so repeats are spread across the deck.
    segments = weight if favs else 1
    deck = []

    for i in range(segments):

        part = rest[i::segments] + favs
        random.shuffle(part)

        if deck and len(part) > 1 and part[0] == deck[-1]:
            part[0], part[-1] = part[-1], part[0]

        deck += part

    for i, key in enumerate(deck):
        if key not in avoid:
            deck[0], deck[i] = key, deck[0]
            break

    return deck


def mood_preference(config):

    if config["mood"] == "appearance":
        return dark_mode()

    if config["mood"] == "night":
        start, end = config["night_hours"]
        hour = time.localtime().tm_hour
        return (hour >= start or hour < end) if start > end else (start <= hour < end)

    return None


def draw(state, pool, count, config, prefer_dark):

    pool_set = set(pool)
    deck = [key for key in state["deck"] if key in pool_set]
    favorites = set(state["favorites"]) & pool_set
    avoid = {key for key in current_pins(state) if key is not None}

    lumas = [state["pins"][key]["luma"] for key in pool if "luma" in state["pins"][key]]
    threshold = statistics.median(lumas) if lumas else None

    picks = []

    for _ in range(min(count, len(pool))):

        # Avoid what's on screen; when the pool is too small for that, at least keep the displays distinct.
        for blocked in (avoid | set(picks), set(picks)):

            if not deck:
                deck = build_deck(pool, favorites, config["favorite_weight"], blocked)

            candidates = [i for i, key in enumerate(deck) if key not in blocked]

            # The deck can hold nothing but blocked pins (a favorite's last copy): start the next pass early.
            if not candidates and pool_set - blocked:
                deck += build_deck(pool, favorites, config["favorite_weight"], blocked)
                candidates = [i for i, key in enumerate(deck) if key not in blocked]

            if candidates:
                break

        candidates = candidates or [0]
        index = candidates[0]

        if prefer_dark is not None and threshold is not None:
            for i in candidates:
                luma = state["pins"][deck[i]].get("luma")
                if luma is not None and (luma <= threshold) == prefer_dark:
                    index = i
                    break

        picks.append(deck.pop(index))

    while len(picks) < count:
        picks.append(picks[len(picks) % len(pool)])

    state["deck"] = deck

    return picks


def push_history(state, pins):

    history = state["history"]
    history.append(list(pins))
    del history[:-HISTORY_LIMIT]

    state["last_change"] = int(time.time())


def show(assignment, state, pins):

    try:
        set_wallpapers(assignment)
    except WallpaperTransactionError as error:
        reconcile_wallpaper_failure(error, assignment, pins, state)
        raise
    state["on_screen"] = [os.path.basename(path) for _, path in assignment]
    state["current"] = [pins[index % len(pins)] for index in range(len(assignment))]
    state["current_screens"] = [screen.get("id") for screen, _ in assignment]


def reconcile_wallpaper_failure(error, assignment, pins, state):

    previous_current = current_pins(state)
    previous_paths = list(state["on_screen"])
    previous_screens = list(state.get("current_screens", []))
    by_screen = {str(screen.get("id")): (index, path) for index, (screen, path) in enumerate(assignment)}
    wildcard = next(((index, path) for index, (screen, path) in enumerate(assignment) if screen.get("id") is None), None)
    slot_count = max(len(previous_current), len(previous_paths), len(previous_screens), len(assignment),
                     len(error.screens) if wildcard else 0)
    actual_current = list(previous_current) + [None] * (slot_count - len(previous_current))
    actual_paths = list(previous_paths) + [""] * (slot_count - len(previous_paths))
    actual_screens = list(previous_screens) + [None] * (slot_count - len(previous_screens))
    for result_position, result in enumerate(error.screens):
        if not isinstance(result, dict):
            continue
        screen_id = str(result.get("id"))
        match = by_screen.get(screen_id)
        wildcard_slot = match is None and wildcard is not None
        if wildcard_slot:
            match = (result_position, wildcard[1])
        if match is None:
            continue
        index, target_path = match
        path = result.get("path") if isinstance(result.get("path"), str) else ""
        assignment_index = wildcard[0] if wildcard_slot else index
        wanted = pins[assignment_index % len(pins)] if pins else None
        if path == target_path:
            actual_current[index] = wanted
        else:
            old_index = next((position for position, old_id in enumerate(previous_screens)
                              if str(old_id) == screen_id), None)
            if old_index is None and wildcard_slot:
                old_index = result_position if result_position < len(previous_current) else (0 if len(previous_current) == 1 else None)
            old_path = previous_paths[old_index] if old_index is not None and old_index < len(previous_paths) else None
            old_key = previous_current[old_index] if old_index is not None and old_index < len(previous_current) else None
            actual_current[index] = old_key if old_path and os.path.basename(path) == old_path else None
        actual_paths[index] = os.path.basename(path) if path else ""
        actual_screens[index] = result.get("id")
    state["current"] = actual_current
    state["current_screens"] = actual_screens
    state["on_screen"] = actual_paths


def advance(config, state):

    screens = current_screens(config, state)
    files = source_files()
    pool = available_pins(state, files)

    if not pool:
        log("No wallpapers available yet; run ./pw refresh.")
        return False

    count = len(screens) if config["different_per_display"] else 1
    prefer_dark = mood_preference(config)

    original_deck = list(state["deck"])
    bad_keys = set()
    while pool:

        picks = draw(state, pool, count, config, prefer_dark)

        try:
            assignment = render_for_screens(picks, screens, config, state, files)
            break

        except BadSource as e:
            log_error(f"Pin {e.key}: cached image is unreadable ({e}); it will be re-downloaded on the next refresh.")
            forget_source(e.key, files)
            bad_keys.add(e.key)
            pool = [key for key in pool if key != e.key]
            state["deck"] = [key for key in picks if key != e.key] + state["deck"]

        except Exception as e:
            state["deck"] = [key for key in original_deck if key not in bad_keys]
            log_error(f"Couldn't render the wallpaper ({short(e)}).")
            return False

    else:
        state["deck"] = [key for key in original_deck if key not in bad_keys]
        log_error("No readable wallpapers left; run ./pw refresh.")
        return False

    try:
        show(assignment, state, picks)

    except WallpaperTransactionError as e:
        state["deck"] = [key for key in original_deck if key not in bad_keys]
        log_error(f"Wallpaper transaction incomplete ({short(e)}); reconciled actual display state.")
        return False

    except Exception as e:
        state["deck"] = [key for key in original_deck if key not in bad_keys]
        log_error(f"Failed to set wallpaper ({short(e)}).")
        return False

    push_history(state, picks)
    log(f"Wallpaper -> {', '.join(picks)}")

    return True


def go_back(config, state):

    history = state["history"]

    if len(history) < 2:
        log("No previous wallpaper to go back to.")
        return False

    files = source_files()
    pool = set(available_pins(state, files))

    index = len(history) - 2

    while index >= 0 and not all(key in pool for key in history[index]):
        index -= 1

    if index < 0:
        log("None of the previous wallpapers are still available.")
        return False

    target = history[index]

    # Nothing in state changes until the desktop has actually changed.
    try:
        show(render_for_screens(target, current_screens(config, state), config, state, files), state, target)

    except Exception as e:
        log_error(f"Failed to go back ({short(e)}).")
        return False

    current = history[-1]
    del history[index + 1:]

    state["deck"] = current + [key for key in state["deck"] if key not in current]
    state["last_change"] = int(time.time())

    log(f"Wallpaper -> {', '.join(target)}  (back)")

    return True


def reapply_current(config, state):

    pins = current_pins(state)
    if any(pin is None for pin in pins):
        raise WallpaperTransactionError(["cannot reapply while a display identity is unknown"], [])
    files = source_files()
    screens = current_screens(config, state)

    # macOS can list displays in a different order after one is reconnected, so pins follow display IDs.
    saved = state["current_screens"]
    by_display = {str(display): pin for display, pin in zip(saved, pins)} if len(saved) == len(pins) else {}
    pins = [by_display.get(str(screen.get("id")), pins[index % len(pins)]) for index, screen in enumerate(screens)]

    show(render_for_screens(pins, screens, config, state, files), state, pins)


def tick_skip_reason(config, state):

    now = time.time()

    if state["paused_until"] and not is_paused(state):
        state["paused_until"] = 0

    if is_paused(state):
        return "paused"

    # A manual next/prev shortly before the tick shouldn't be overridden straight away.
    # A future stamp means the clock was set back; it counts as long ago rather than freezing rotation.
    if 0 <= now - state["last_change"] < config["interval_minutes"] * 30:
        return "changed recently"

    if config["skip_when_locked"] and screen_locked():
        return "screen locked"

    return None


# =========================
# COMMANDS
# =========================

def parse_display(args, allowed=()):

    display = 1

    for arg in args:

        if arg.lower() in allowed:
            continue

        if not arg.isdigit() or int(arg) < 1:
            raise ValueError(f"unexpected argument {arg!r}")

        display = int(arg)

    return display


def parse_duration(text):

    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([mhd])", text.strip().lower())

    if not match:
        raise ValueError(f"can't read duration {text!r}; use e.g. 30m, 2h or 1d")

    duration = float(match.group(1)) * {"m": 60, "h": 3600, "d": 86400}[match.group(2)]
    if not math.isfinite(duration):
        raise ValueError("duration is too large")
    if duration <= 0:
        raise ValueError("duration must be greater than zero")
    return duration


def cmd_tick(config, args):

    if refresh_due(config, load_state()):
        do_refresh(config)

    with locked_state() as state:
        skip = tick_skip_reason(config, state)
        ok = True if skip else advance(config, state)
        snapshot = copy.deepcopy(state)

    # After the change and a few pins at a time, so slow AI upscaling never delays the wallpaper.
    upscale_sources(config, snapshot, source_files(), UPSCALES_PER_TICK)

    if not skip and ok and config.get("orientation_model") and time.time() >= snapshot.get("orientation_retry_after", 0):
        try:
            orient_batch(config, snapshot)
        except ValueError as error:
            with locked_state() as state:
                state["orientation_retry_after"] = time.time() + 3600
            log_error(f"Orientation unavailable; retrying after one hour ({short(error)}).")

    return 0 if ok else 1


def cmd_next(config, args):

    with locked_state() as state:
        return 0 if advance(config, state) else 1


def cmd_prev(config, args):

    with locked_state() as state:
        return 0 if go_back(config, state) else 1


def cmd_shuffle(config, args):

    with locked_state() as state:
        original_deck = list(state["deck"])
        original_files = set(source_files())
        state["deck"] = []
        if advance(config, state):
            return 0
        removed = original_files - set(source_files())
        state["deck"] = [key for key in original_deck if key not in removed]
        return 1


def cmd_refresh(config, args):

    return 0 if do_refresh(config) else 1


def cmd_ban(config, args):

    expected = None
    display_args = args
    if "--expected-pin" in args:
        if len(args) != 3 or args[1] != "--expected-pin" or not re.fullmatch(PIN_ID, args[2]):
            raise ValueError("expected a display followed by --expected-pin and a valid pin ID")
        display_args = args[:1]
        expected = args[2]
    display = parse_display(display_args)

    with locked_state() as state:

        try:
            key = pin_on_display(state, display)
        except ValueError as error:
            if expected is not None:
                raise StaleBan("display changed since confirmation; ban cancelled") from error
            raise

        if expected is not None and key != expected:
            raise StaleBan("display pin changed since confirmation; ban cancelled")

        if not key:
            log("Nothing is showing yet, so there is nothing to ban.")
            return 1

        if key not in state["banned"]:
            state["banned"].append(key)

        state["deck"] = [k for k in state["deck"] if k != key]

        log(f"Banned pin {key}.")

        if not advance(config, state):
            return 1
        state["history"] = [
            entry for entry in state["history"] if key not in entry
        ]
        return 0


def cmd_bans(config, args):

    state = load_state()

    if not state["banned"]:
        print("No banned pins.")
        return 0

    print(f"Banned pins ({len(state['banned'])}):")

    for key in state["banned"]:
        print(f"  {key}  {pin_url(key) or '(from an older version; matched to its pin on the next refresh)'}")

    print("\nUnban one with ./pw unban <pin-id>, or all of them with ./pw unban all.")

    return 0


def cmd_unban(config, args):

    if len(args) != 1:
        print("usage: pw unban <pin-id>|all   (see ./pw bans)", file=sys.stderr)
        return 2

    with locked_state() as state:

        if args[0].lower() == "all":
            count = len(state["banned"])
            state["banned"] = []
            log(f"Cleared {count} banned pin(s).")
            return 0

        if args[0] not in state["banned"]:
            log(f"Pin {args[0]} isn't banned.")
            return 1

        state["banned"].remove(args[0])
        log(f"Unbanned pin {args[0]}.")

    return 0


def cmd_fav(config, args):

    display = parse_display(args)

    with locked_state() as state:

        key = pin_on_display(state, display)

        if not key:
            log("Nothing is showing yet.")
            return 1

        if key in state["favorites"]:
            state["favorites"].remove(key)
            log(f"Pin {key} is no longer a favorite.")
        else:
            state["favorites"].append(key)
            log(f"Pin {key} is a favorite; it shows {config['favorite_weight']}x per cycle from the next reshuffle.")

    return 0


def cmd_rotate(config, args):

    words = [a.lower() for a in args if a.lower() in (*ROTATE_STEPS, "reset", "auto")]

    if len(words) > 1:
        raise ValueError("give one direction: cw, ccw, 180, reset or auto")

    direction = words[0] if words else "cw"
    display = parse_display(args, allowed=(*ROTATE_STEPS, "reset", "auto"))

    with locked_state() as state:

        key = pin_on_display(state, display)

        if not key:
            log("Nothing is showing yet.")
            return 1

        angle = (effective_angle(key, config, state) + ROTATE_STEPS[direction]) % 360 if direction in ROTATE_STEPS else 0

        if direction == "auto":
            state["rotations"].pop(key, None)
            angle = effective_angle(key, config, state)
        elif angle or config.get("orientation_model"):
            state["rotations"][key] = angle
        else:
            state["rotations"].pop(key, None)

        try:
            reapply_current(config, state)
        except Exception as e:
            log_error(f"Saved the rotation, but couldn't redraw the wallpaper ({short(e)}).")
            return 1

        log(f"Pin {key} rotated to {angle} degrees.")

    return 0


def cmd_open(config, args):

    key = pin_on_display(load_state(), parse_display(args))
    url = pin_url(key) if key else None

    if not url:
        log("No Pinterest pin is showing right now.")
        return 1

    subprocess.run(["open", url], check=False)
    print(url)

    return 0


def cmd_pause(config, args):

    if len(args) > 1:
        raise ValueError("usage: pw pause [30m|2h|1d]")

    if not args:
        until = -1
    else:
        until = int(time.time() + parse_duration(args[0]))
        if not _valid_timestamp(until):
            raise ValueError("pause duration exceeds the supported calendar range")

    with locked_state() as state:
        state["paused_until"] = until

    if until == -1:
        log("Paused until you run ./pw resume.")
    else:
        log(f"Paused until {time.strftime('%Y-%m-%d %H:%M', time.localtime(until))}.")

    return 0


def cmd_resume(config, args):

    with locked_state() as state:
        state["paused_until"] = 0

    log("Resumed automatic changes.")

    return 0


def pause_label(state):

    if state["paused_until"] == -1:
        return "paused until resumed"

    try:
        until = time.localtime(state["paused_until"])
    except (OverflowError, OSError, TypeError, ValueError):
        return "paused until an unknown date"
    same_day = until[:3] == time.localtime()[:3]

    return f"paused until {time.strftime('%H:%M' if same_day else '%a %d %b %H:%M', until)}"


def agent_summary(config):

    try:
        interval = launchctl_interval(LABEL)
        legacy_interval = launchctl_interval(LEGACY_LABEL)
    except RuntimeError as error:
        return f"status unavailable ({short(error)})"
    wanted = round(config["interval_minutes"] * 60)

    if legacy_interval is not None:
        return "old com.pinterest.wallpaper agent is running; run ./pw install to migrate"

    if interval is None:
        return "not installed; run ./pw install"

    if interval == 0:
        return "installed (couldn't read its interval)"

    if interval != wanted:
        return f"runs every {interval // 60} min but config says {config['interval_minutes']}; run ./pw install"

    return f"installed, every {interval // 60} min"


def source_note(key, state, config, size):

    meta = dict(state["pins"].get(key, {}))
    ai = False
    files = source_files()
    if config["upscaler"] and key in files:
        try:
            cached = upscale_cache_paths(key, config["upscaler"], config["upscaler_model"],
                                         os.path.join(IMAGE_DIR, files[key]))[0]
            ai = os.path.exists(cached)
        except OSError:
            ai = False

    if ai:
        meta.update(width=meta.get("width", 0) * 4, height=meta.get("height", 0) * 4)

    angle = effective_angle(key, config, state, files)
    w, h = oriented_size(meta, angle)

    if not w or not h:
        return ""

    source = f"{w}x{h} {'AI-upscaled ' if ai else ''}source"
    cover = enlargement(meta, angle, size)
    settings = render_settings(key, config, state, ai, files)
    limit = settings["max_upscale"] or float("inf")

    if settings["fit"] == "cover" and cover <= limit:
        return f"{source}, scaled {cover:.1f}x to fill {size[0]}x{size[1]}"

    return f"{source}, scaled {min(size[0] / w, size[1] / h, limit):.1f}x on a backdrop"


def cmd_status(config, args):

    state = load_state()
    screens = current_screens(config, state)
    files = source_files()
    pool = available_pins(state, files)
    pool_set = set(pool)
    deck = [key for key in state["deck"] if key in pool_set]
    showing = current_pins(state)

    print(f"Boards        : {', '.join(state['board_names']) or '(not synced yet)'}")

    for board in config["boards"]:
        print(f"                {board}{'  (switched off)' if board in state['disabled_boards'] else ''}")

    for i, screen in enumerate(screens, 1):
        label = "Displays      :" if i == 1 else "               "
        print(f"{label} {i}. {screen['name']}  {screen['width']}x{screen['height']}")

    for i, key in enumerate(showing, 1):

        if key is None:
            label = "Showing       :" if i == 1 else "               "
            print(f"{label} unknown display state")
            continue

        notes = []

        angle = effective_angle(key, config, state, files)
        if angle:
            notes.append(f"rotated {angle}")
        if key in state["favorites"]:
            notes.append("favorite")

        label = "Showing       :" if i == 1 else "               "
        extra = f"  ({', '.join(notes)})" if notes else ""
        print(f"{label} {key}{extra}  {pin_url(key) or ''}")

        screen = screens[(i - 1) % len(screens)]
        note = source_note(key, state, config, (screen["width"], screen["height"]))

        if note:
            print(f"                {note}")

    if not showing:
        print("Showing       : -")

    upcoming = draw(copy.deepcopy(state), pool, 1, config, mood_preference(config))[0] if deck else "(reshuffle)"
    print(f"Up next       : {upcoming}")
    print(
        f"Collection    : {len(pool)} in rotation, {len(deck)} left before reshuffle,"
        f" {len(state['banned'])} banned, {len(state['favorites'])} favorite(s)"
    )

    changes = f"every {config['interval_minutes']} min"

    if is_paused(state):
        changes += f" ({pause_label(state)})"

    print(f"Changes       : {changes}; agent {agent_summary(config)}")
    print(
        f"Render        : fit={config['fit_mode']}  crop={config['crop']}  backdrop={config['backdrop']}"
        f"  sharpen={config['sharpen']}  menubar={config['menubar_gradient']}"
        f"  max_upscale={config['max_upscale'] or 'off'}  upscaler={config['upscaler'] or 'off'}"
        f"  per-display={'different' if config['different_per_display'] else 'same'}"
    )
    print(f"Mood          : {config['mood']}")
    print(f"Last refresh  : {fmt_time(state['last_refresh_ok'])}")

    due_in = state["last_refresh_ok"] + config["refresh_hours"] * 3600 - time.time()

    if not math.isfinite(due_in):
        print("Next refresh  : time outside supported range")
    elif due_in <= 0:
        print("Next refresh  : due now")
    else:
        print(f"Next refresh  : in {int(due_in) // 3600}h {int(due_in) % 3600 // 60}m")

    for url, fail in state["board_fails"].items():
        print(f"Board errors  : {url} failed {fail['count']}x: {fail['error']}")

    return 0


def cmd_menubar(config, args):

    state = load_state()
    showing = current_pins(state)
    paused = is_paused(state)
    screens = current_screens(config, state, detect=False)

    def note(key, number):
        screen = screens[(number - 1) % len(screens)]
        return source_note(key, state, config, (screen["width"], screen["height"]))

    def item(title, *params, shell=PW, refresh=True):
        parts = [f'{title} | shell="{shell}"']
        parts += [f'param{i}="{p}"' for i, p in enumerate(params, 1)]
        parts += ["terminal=false", f"refresh={'true' if refresh else 'false'}"]
        print(" ".join(parts))

    def pin_actions(key, prefix="", display=()):
        fav = "Unfavorite" if key in state["favorites"] else "Favorite"
        item(f"{prefix}{fav}", "fav", *display)
        item(f"{prefix}Rotate clockwise", "rotate", "cw", *display)
        item(f"{prefix}Rotate counter-clockwise", "rotate", "ccw", *display)
        if state["rotations"].get(key):
            item(f"{prefix}Reset rotation", "rotate", "reset", *display)
        item(f"{prefix}Ban this pin", "ban", *display)
        item(f"{prefix}Open on Pinterest", "open", *display, refresh=False)

    print(f"PW{' (paused)' if paused else ''} | sfimage=photo.on.rectangle.angled")
    print("---")

    if showing and showing[0] is not None:
        url = pin_url(showing[0])
        print(f"Pin {menu_text(showing[0])}" + (f" | href={url}" if url else ""))
        if note(showing[0], 1):
            print(menu_text(note(showing[0], 1)))
    print(menu_text(", ".join(state["board_names"])) if state["board_names"] else "Not synced yet")

    if paused:
        print(pause_label(state).capitalize())

    print("---")
    item("Next wallpaper", "next")
    item("Previous wallpaper", "prev")
    item("Shuffle", "shuffle")

    if showing and showing[0] is not None:
        print("---")
        pin_actions(showing[0])

    if len(showing) > 1:
        for number, key in enumerate(showing[1:], 2):
            if key is None:
                print(f"Display {number}: current wallpaper unknown")
                continue
            print(f"Display {number}: pin {menu_text(key)}")
            if note(key, number):
                print(f"--{menu_text(note(key, number))}")
            pin_actions(key, prefix="--", display=(str(number),))

    print("---")

    if paused:
        item("Resume", "resume")
    else:
        item("Pause for 1 hour", "pause", "1h")
        item("Pause until resumed", "pause")

    print("---")
    item("Refresh boards now", "refresh")
    item("Show wallpapers folder", WALLPAPERS_DIR, shell="/usr/bin/open", refresh=False)

    return 0


def plist_path(label=LABEL):

    return os.path.join(LAUNCH_AGENTS, f"{label}.plist")


def bootout(label):

    domain = f"gui/{os.getuid()}"

    if launchctl_interval(label) is None:
        return False

    try:
        result = subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True, check=False, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"launchctl bootout failed for {label}: {short(e)}") from e
    if result.returncode:
        raise RuntimeError(f"launchctl bootout failed for {label} (exit {result.returncode})")

    for _ in range(20):
        if launchctl_interval(label) is None:
            return True
        time.sleep(0.25)

    raise RuntimeError(f"launchctl job {label} did not stop")


def cmd_install(config, args):

    interval = launch_interval_seconds(config.get("interval_minutes"))
    if interval is None:
        log_error("interval_minutes cannot be represented as a valid launchd interval.")
        return 1

    os.makedirs(LAUNCH_AGENTS, exist_ok=True)

    try:
        if bootout(LEGACY_LABEL):
            log(f"Stopped the old {LEGACY_LABEL} agent.")
        bootout(LABEL)
    except RuntimeError as e:
        log_error(str(e))
        return 1
    remove_quietly(plist_path(LEGACY_LABEL))

    agent = {
        "Label": LABEL,
        "ProgramArguments": [PW, "tick"],
        "StartInterval": interval,
        "RunAtLoad": True,
        "StandardOutPath": os.path.join(BASE_DIR, "agent.log"),
        "StandardErrorPath": os.path.join(BASE_DIR, "agent.err"),
    }

    atomic_write(plist_path(), lambda handle: plistlib.dump(agent, handle))

    error = ""

    # launchd can still be tearing the old job down; bootstrap fails with EIO until it's gone.
    for _ in range(5):

        result = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", plist_path()],
            check=False,
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            log(
                f"Installed {LABEL}: the wallpaper changes every {config['interval_minutes']} min."
                " Logs go to agent.log and agent.err."
            )
            return 0

        error = result.stderr.strip()
        time.sleep(1)

    log_error(f"launchctl bootstrap failed: {error}")

    return 1


def cmd_uninstall(config, args):

    removed = False

    for label in (LABEL, LEGACY_LABEL):

        try:
            removed = bootout(label) or removed
        except RuntimeError as e:
            log_error(str(e))
            return 1

        if os.path.lexists(plist_path(label)):
            remove_quietly(plist_path(label))
            removed = True

    log("Removed the launchd agent." if removed else "No launchd agent was installed.")

    return 0


def cmd_doctor(config, args):

    failures = 0

    def report(level, message):
        nonlocal failures
        failures += level == "FAIL"
        print(f"{level:<5} {message}", flush=True)

    import PIL

    report(
        "warn" if sys.version_info < (3, 10) else "ok",
        f"Python {sys.version.split()[0]} ({sys.executable})"
        + ("; 3.9 is past end-of-life, see README for a venv" if sys.version_info < (3, 10) else ""),
    )

    pillow = tuple(int(p) for p in re.findall(r"\d+", PIL.__version__)[:2])
    report("ok" if pillow >= (9, 1) else "FAIL", f"Pillow {PIL.__version__}, requests {requests.__version__}")

    _, problems = load_config()

    for problem in problems:
        report("warn", problem)

    if any(PLACEHOLDER_BOARD in board for board in config["boards"]):
        report("FAIL", 'config.json "boards" still has the placeholder URL')
    elif not problems:
        report("ok", "config.json")

    screens = detect_screens()

    if screens:
        report("ok", "Displays: " + "; ".join(f"{s['name']} {s['width']}x{s['height']}" for s in screens))
    else:
        report("FAIL", "Couldn't list displays")

    try:
        report("ok", f"Wallpaper API reachable (current: {os.path.basename(osascript(['-l', 'JavaScript', '-e', JXA_CURRENT_WALLPAPER], timeout=15))})")
    except Exception as e:
        report("FAIL", f"Wallpaper API: {short(e)}")

    summary = agent_summary(config)
    report("ok" if summary.startswith("installed") else "warn", f"Agent {summary}")

    for board in config["boards"]:

        try:
            listing = fetch_board(board, config["max_pins"])
            level = "ok" if listing["complete"] else "warn"
            how = "" if listing["complete"] else " (partial: RSS fallback or paging stopped)"
            report(level, f"Board {listing['name']}: {len(listing['pins'])} pins listed{how}")

        except Offline as e:
            report("FAIL", f"Board {board}: offline ({e})")

        except Exception as e:
            report("FAIL", f"Board {board}: {short(e)}")

    if config["upscaler"]:

        binary = config["upscaler"]
        models = os.path.join(os.path.dirname(os.path.abspath(binary)), "models")

        if not os.access(binary, os.X_OK):
            report("FAIL", f"Upscaler {binary} isn't an executable file")
        elif not os.path.isdir(models):
            report("warn", f"Upscaler found, but no models/ folder next to it ({models})")
        else:
            report("ok", f"Upscaler {binary} ({config['upscaler_model']})")

    state = load_state()

    if screens:
        state["screens"] = screens

    size = sum(
        os.path.getsize(os.path.join(d, n))
        for d in (IMAGE_DIR, UPSCALED_DIR, WALLPAPERS_DIR)
        for n in os.listdir(d)
    )
    report(
        "ok",
        f"Cache: {len(source_files())} source image(s), {len(os.listdir(WALLPAPERS_DIR))} render(s),"
        f" {size / 1e6:.1f} MB; {len(available_pins(state))} pin(s) in rotation",
    )

    pool = available_pins(state)
    files = source_files()
    downloaded = [key for key in state["pins"] if key in files]
    banned = [key for key in downloaded if key in state["banned"]]
    switched_off = len(downloaded) - len(banned) - len(pool)
    left_out = ", ".join(reason for reason in (
        f"{switched_off} on boards that are switched off (./pw tui, then b, to switch one on)" if switched_off else "",
        f"{len(banned)} banned (./pw unban <pin-id>|all)" if banned else "",
    ) if reason)

    if not downloaded:
        hint = ("; skip_upscale_over may be leaving every pin out" if config["skip_upscale_over"] and state["last_refresh_ok"]
                else "; run ./pw refresh")
        report("warn", f"Nothing to rotate: no pins are downloaded{hint}")
    elif not pool:
        report("warn", f"Nothing to rotate: {len(downloaded)} pin(s) are downloaded but none are eligible: {left_out}")
    elif left_out:
        report("ok", f"Left out of rotation: {left_out}")

    target = largest_screen(config, state)
    blurry = [
        key for key in pool
        if enlargement(state["pins"][key], state["rotations"].get(key, 0), target) > DOCTOR_ENLARGEMENT_WARNING
    ]
    summary = (
        f"{len(blurry)} of {len(pool)} pin(s) need more than {DOCTOR_ENLARGEMENT_WARNING}x"
        f" enlargement to fill {target[0]}x{target[1]}"
    )

    if not blurry:
        report("ok", f"No pin needs more than {DOCTOR_ENLARGEMENT_WARNING}x enlargement")
    elif config["max_upscale"] or config["upscaler"]:
        report("ok", f"{summary}; handled by {'the upscaler' if config['upscaler'] else 'max_upscale'}")
    else:
        report("warn", f"{summary}; set max_upscale or an upscaler to keep them sharp (see README)")

    return 1 if failures else 0


def cmd_orient(config, args):
    if not args or args[0] not in ("preview", "scan") or len(args) > (2 if args[0] == "preview" else 1):
        raise ValueError("usage: orient preview [PIN_ID] | orient scan")
    if args[0] == "scan":
        count = orient_batch(config, load_state(), explicit=True)
        print(f"Orientation scan: cached {count} pin(s); at most 4 per invocation.")
        return 0

    backend, model, _ = orientation_backend(config)
    if len(args) == 2:
        key = args[1]
    else:
        try:
            with open(STATE_FILE) as handle:
                raw = json.load(handle)
            current = raw.get("current", [])
            history = raw.get("history", [])
            if not current and isinstance(history, list) and history and isinstance(history[-1], list):
                current = history[-1]
            key = current[0] if isinstance(current, list) and current else None
        except (OSError, ValueError, AttributeError) as error:
            raise ValueError(f"cannot read current pin: {short(error)}") from error
    if not isinstance(key, str) or not re.fullmatch(PIN_ID, key):
        raise ValueError("no current pin; provide a pin ID")
    try:
        files = source_files()
    except OSError as error:
        raise ValueError(f"cannot read local sources: {short(error)}") from error
    if key not in files:
        raise ValueError(f"no local source for pin {key}")
    try:
        predictions = backend.predict(model, [os.path.join(IMAGE_DIR, files[key])], timeout=ORIENTATION_TIMEOUT)
    except (backend.OrientationError, OSError) as error:
        raise ValueError(f"orientation failed: {short(error)}") from error
    if len(predictions) != 1 or predictions[0] is None:
        raise ValueError(f"orientation could not read pin {key}")
    prediction = predictions[0]
    print(f"Pin {key}: {'abstain' if prediction.angle is None else str(prediction.angle) + ' degrees clockwise'} (score {prediction.score:.3f})")
    return 0


def cmd_tui(config, args):
    import wallpaper_tui
    from wallpaper_tui_backend import Backend

    return wallpaper_tui.run(Backend(sys.modules[__name__], config))


COMMANDS = {
    "tick": cmd_tick,
    "next": cmd_next,
    "prev": cmd_prev,
    "shuffle": cmd_shuffle,
    "pause": cmd_pause,
    "resume": cmd_resume,
    "ban": cmd_ban,
    "fav": cmd_fav,
    "rotate": cmd_rotate,
    "orient": cmd_orient,
    "open": cmd_open,
    "refresh": cmd_refresh,
    "bans": cmd_bans,
    "unban": cmd_unban,
    "status": cmd_status,
    "install": cmd_install,
    "uninstall": cmd_uninstall,
    "doctor": cmd_doctor,
    "menubar": cmd_menubar,
    "tui": cmd_tui,
}

USAGE = """usage: pw <command> [arguments]

rotation
  next                          show the next wallpaper
  prev                          go back to the previous wallpaper
  shuffle                       reshuffle the deck and show a new wallpaper
  pause [30m|2h|1d]             stop automatic changes (until resumed without a duration)
  resume                        restart automatic changes

the pin on screen (add a display number, e.g. "ban 2", with different_per_display)
  ban [display]                 drop it from rotation, then advance
  fav [display]                 toggle favorite; favorites come up more often
  rotate [cw|ccw|180|reset|auto] [display]
                                turn it, e.g. so a vertical pin fills a landscape screen
  open [display]                open it on pinterest.com

library
  tui                           browse and preview wallpapers interactively
  refresh                       re-read the boards from Pinterest now
  bans                          list banned pins
  unban <pin-id>|all            put banned pins back into rotation
  status                        show what the tool is doing
  orient preview [PIN_ID]       predict locally without changing state or wallpaper
  orient scan                   cache up to 4 uncached pins per invocation

setup
  install                       install or update the launchd agent (uses interval_minutes)
  uninstall                     remove the launchd agent
  doctor                        check dependencies, displays, the agent and your boards
  menubar                       print the SwiftBar/xbar menu (used by the plugin)
  tick                          refresh if due, then change the wallpaper (what launchd runs)
"""


def main(argv):

    command, args = (argv[0], argv[1:]) if argv else ("", [])

    if command in ("-h", "--help", "help") or any(arg in ("-h", "--help") for arg in args):
        print(USAGE)
        return 0

    handler = COMMANDS.get(command)

    if handler is None:
        print(USAGE if not command else f"unknown command: {command}\n\n{USAGE}", file=sys.stderr)
        return 2

    if args and command in {"tick", "next", "prev", "shuffle", "resume", "refresh", "bans", "status", "install", "uninstall", "doctor", "menubar", "tui"}:
        print(f"pw {command}: unexpected argument {args[0]!r}", file=sys.stderr)
        return 2

    if command == "tui" and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        print("pw tui: an interactive terminal is required", file=sys.stderr)
        return 2

    preview = command == "tui" or command == "orient" and args[:1] == ["preview"]
    if not preview:
        for directory in (IMAGE_DIR, UPSCALED_DIR, WALLPAPERS_DIR):
            os.makedirs(directory, exist_ok=True)

    config, problems = load_config(create=not preview)

    if command not in ("menubar", "doctor"):
        for problem in problems:
            log_error(problem)

    try:
        return handler(config, args) or 0
    except ValueError as e:
        print(f"pw {command}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
