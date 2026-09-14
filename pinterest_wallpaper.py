#!/usr/bin/env python3

import os
import re
import io
import sys
import html
import json
import time
import random
import hashlib
import functools
import subprocess
import warnings

warnings.filterwarnings(
    "ignore",
    message="urllib3 v2 only supports OpenSSL",
)

import requests
from requests.adapters import HTTPAdapter, Retry
from PIL import Image, ImageOps, ImageFilter


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGE_DIR = os.path.join(BASE_DIR, "images")
WALLPAPERS_DIR = os.path.join(BASE_DIR, "wallpapers")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
STATE_FILE = os.path.join(BASE_DIR, ".state.json")

os.makedirs(IMAGE_DIR, exist_ok=True)
os.makedirs(WALLPAPERS_DIR, exist_ok=True)

# Renders are cached by mtime, so they must also be invalidated by any edit to
# the code that produced them, not just by the source image changing.
SCRIPT_MTIME = os.path.getmtime(os.path.abspath(__file__))

FALLBACK_SIZE = (2560, 1600)
HISTORY_LIMIT = 50

DEFAULT_CONFIG = {
    "boards": [
        "https://www.pinterest.com/YOUR_USERNAME/YOUR_BOARD/"
    ],
    "refresh_hours": 24,
    "retry_hours": 1,
    "resolution": "auto",
    "fit_mode": "cover",
    "sharpen": True,
    "menubar_gradient": False,
    "notifications": True,
    "new_pins_first": True,
}

DEFAULT_STATE = {
    "last_refresh_ok": 0,
    "last_refresh_attempt": 0,
    "fail_streak": 0,
    "board_names": [],
    "deck": [],
    "history": [],
    "banned": [],
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

SESSION = requests.Session()
SESSION.mount(
    "https://",
    HTTPAdapter(
        max_retries=Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
        )
    ),
)


# =========================
# PLUMBING
# =========================

def log(message):

    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}")


def progress(message):

    if sys.stdout.isatty():
        print(message, end="\r")


def save_json(path, data):

    tmp = path + ".tmp"

    with open(tmp, "w") as handle:
        json.dump(data, handle, indent=2)

    os.replace(tmp, path)


def load_config():

    config = dict(DEFAULT_CONFIG)

    if not os.path.exists(CONFIG_FILE):
        save_json(CONFIG_FILE, config)
        return config

    try:
        with open(CONFIG_FILE) as handle:
            loaded = json.load(handle)

        if isinstance(loaded, dict):
            config.update(loaded)

    except Exception as e:
        log(f"config.json unreadable ({e}); using defaults.")

    return config


def load_state():

    state = dict(DEFAULT_STATE)

    try:
        with open(STATE_FILE) as handle:
            loaded = json.load(handle)

        if isinstance(loaded, dict):
            state.update(loaded)

    except Exception:
        pass

    return state


def save_state(state):

    save_json(STATE_FILE, state)


def applescript_string(value):

    escaped = value.replace("\\", "\\\\").replace('"', '\\"')

    return f'"{escaped}"'


def set_wallpaper(path):

    script = (
        "tell application \"System Events\" to tell every desktop"
        f" to set picture to {applescript_string(path)}"
    )

    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip() or "osascript failed"
        )


def notify(config, title, message):

    if not config.get("notifications"):
        return

    script = (
        f"display notification {applescript_string(message)}"
        f" with title {applescript_string(title)}"
    )

    subprocess.run(["osascript", "-e", script], check=False)


@functools.lru_cache(maxsize=1)
def detect_resolution():

    try:
        output = subprocess.run(
            ["system_profiler", "SPDisplaysDataType"],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout

        sizes = re.findall(r"Resolution:\s+(\d+)\s*x\s*(\d+)", output)

        if sizes:
            width, height = max(sizes, key=lambda s: int(s[0]) * int(s[1]))
            return int(width), int(height)

    except Exception:
        pass

    return FALLBACK_SIZE


def target_size(config):

    match = re.fullmatch(
        r"(\d+)\s*x\s*(\d+)",
        str(config.get("resolution", "auto")).strip().lower()
    )

    if match:
        return int(match.group(1)), int(match.group(2))

    return detect_resolution()


# =========================
# BOARD SCRAPING
# =========================

def scrape_board(board_url):

    response = SESSION.get(board_url, headers=PAGE_HEADERS, timeout=30)
    response.raise_for_status()

    pws_match = re.search(
        r'<script[^>]*id="__PWS_INITIAL_PROPS__"[^>]*>(.*?)</script>',
        response.text,
        re.DOTALL
    )

    if not pws_match:
        raise RuntimeError("no __PWS_INITIAL_PROPS__ block in page HTML")

    payload = pws_match.group(1).strip()

    # Pinterest serves this block entity-escaped on some responses and as raw
    # JSON on others. Unescaping raw JSON would turn a literal "&quot;" inside
    # a pin title into a bare quote, so only unescape once the raw parse fails.
    try:
        props = json.loads(payload)
    except json.JSONDecodeError:
        props = json.loads(html.unescape(payload))

    resources = props["initialReduxState"]["resources"]

    pin_list = []

    for resource_val in resources.get("BoardFeedResource", {}).values():

        data = resource_val.get("data", [])

        if isinstance(data, list) and data:
            pin_list = data
            break

    if not pin_list:
        raise RuntimeError("board feed contained no pins")

    urls = []

    for pin in pin_list:

        if not isinstance(pin, dict) or pin.get("type") != "pin":
            continue

        images = pin.get("images", {})

        if not isinstance(images, dict):
            continue

        image_url = None

        for quality in ["orig", "736x", "474x"]:

            entry = images.get(quality)

            if isinstance(entry, dict):
                image_url = entry.get("url")
            elif isinstance(entry, str):
                image_url = entry

            if image_url:
                break

        if image_url:
            urls.append(image_url)

    board_meta = {}

    for br_val in resources.get("BoardResource", {}).values():

        board = br_val.get("data", {})

        if isinstance(board, dict) and board:
            board_meta = board
            break

    return (
        board_meta.get("name", "Unknown"),
        board_meta.get("pin_count", "?"),
        list(dict.fromkeys(urls)),
    )


def scrape_board_rss(board_url):

    response = SESSION.get(
        board_url.rstrip("/") + ".rss",
        headers=PAGE_HEADERS,
        timeout=30
    )

    response.raise_for_status()

    text = html.unescape(response.text)

    thumbs = re.findall(
        r"https://i\.pinimg\.com/\d+x/[0-9a-f/]+\.(?:jpg|png)",
        text
    )

    urls = [
        re.sub(r"/\d+x/", "/originals/", thumb)
        for thumb in dict.fromkeys(thumbs)
    ]

    if not urls:
        raise RuntimeError("RSS feed contained no pin images")

    title = re.search(r"<title>(.*?)</title>", text, re.DOTALL)

    return (
        title.group(1).strip() if title else "Unknown",
        len(urls),
        urls,
    )


def fetch_board(board_url):

    try:
        name, pin_count, urls = scrape_board(board_url)
        return name, pin_count, urls, False

    except Exception as e:
        log(f"Page scrape failed ({e}); falling back to RSS.")
        name, pin_count, urls = scrape_board_rss(board_url)
        return name, pin_count, urls, True


# =========================
# DOWNLOAD
# =========================

def cache_name(url):

    return hashlib.md5(url.encode()).hexdigest() + ".jpg"


def download_images(urls):

    downloaded = []
    failures = 0
    total = len(urls)

    for i, url in enumerate(urls, 1):

        try:
            progress(f"Checking {i}/{total}...")

            if not url.startswith("https://"):
                continue

            path = os.path.join(IMAGE_DIR, cache_name(url))

            if os.path.exists(path):

                try:
                    with Image.open(path) as img:
                        img.verify()

                    downloaded.append(path)
                    continue

                except Exception:
                    # Drop the corrupt cache entry and fall through to re-download.
                    os.remove(path)

            r = SESSION.get(
                url,
                headers=IMAGE_HEADERS,
                timeout=20,
                allow_redirects=True
            )

            r.raise_for_status()

            content_type = r.headers.get("Content-Type", "").lower()

            if not content_type.startswith("image/"):
                continue

            if len(r.content) < 5000:
                continue

            image = Image.open(io.BytesIO(r.content))

            if image.width < 300 or image.height < 300:
                continue

            image.convert("RGB").save(path, "JPEG", quality=95)

            downloaded.append(path)

        except Exception as e:
            failures += 1
            print(f"Skipped {url[-48:]}: {e}")
            continue

    print(f"Downloaded {len(downloaded)}/{total} images.")

    return downloaded, failures


# =========================
# RENDER
# =========================

def render_name(src_path, size):

    stem = os.path.splitext(os.path.basename(src_path))[0]

    return f"{stem}.{size[0]}x{size[1]}.jpg"


def source_stem(render_filename):

    return render_filename.split(".")[0]


def add_menubar_gradient(img, strength=135):

    width, height = img.size
    band = max(48, height // 20)

    column = Image.new("L", (1, band))

    for y in range(band):
        column.putpixel((0, y), int(strength * (1 - y / band)))

    mask = column.resize((width, band), Image.Resampling.BILINEAR)

    shaded = img.copy()
    shaded.paste(Image.new("RGB", (width, band), "black"), (0, 0), mask)

    return shaded


def render_wallpaper(src_path, out_path, config, size):

    width, height = size

    img = Image.open(src_path).convert("RGB")

    if config.get("fit_mode") == "blur":

        canvas = ImageOps.fit(
            img, size, method=Image.Resampling.LANCZOS
        ).filter(ImageFilter.GaussianBlur(40))

        ratio = min(width / img.width, height / img.height)

        scaled = img.resize(
            (max(1, int(img.width * ratio)), max(1, int(img.height * ratio))),
            Image.Resampling.LANCZOS
        )

        canvas.paste(
            scaled,
            ((width - scaled.width) // 2, (height - scaled.height) // 2)
        )

        out = canvas

    else:
        out = ImageOps.fit(img, size, method=Image.Resampling.LANCZOS)

    if config.get("sharpen") and img.width < width:
        out = out.filter(
            ImageFilter.UnsharpMask(radius=1.6, percent=110, threshold=3)
        )

    if config.get("menubar_gradient"):
        out = add_menubar_gradient(out)

    out.save(out_path, "JPEG", quality=95)


def prune(directory, keep):

    for filename in os.listdir(directory):

        if not filename.endswith(".jpg") or filename in keep:
            continue

        os.remove(os.path.join(directory, filename))

        print(f"Pruned stale: {os.path.basename(directory)}/{filename}")


# =========================
# SYNC
# =========================

def cache_floor():

    stamps = [SCRIPT_MTIME]

    if os.path.exists(CONFIG_FILE):
        stamps.append(os.path.getmtime(CONFIG_FILE))

    return max(stamps)


def sync_boards(config, state):

    size = target_size(config)
    floor = cache_floor()

    all_urls = []
    names = []
    degraded = False

    for board_url in config.get("boards", []):

        name, pin_count, urls, fallback = fetch_board(board_url)

        degraded = degraded or fallback
        names.append(name)

        print(f"Board: {name} ({pin_count} pins, {len(urls)} usable)")

        all_urls.extend(urls)

    all_urls = list(dict.fromkeys(all_urls))

    if not all_urls:
        raise RuntimeError("no pins found across configured boards")

    state["board_names"] = names

    images, failures = download_images(all_urls)

    if not images:
        raise RuntimeError(f"none of {len(all_urls)} images could be downloaded")

    # Tracked by source pin, not by rendered filename: re-rendering at a new
    # resolution changes every filename without a single new pin being added.
    known = {source_stem(name) for name in os.listdir(WALLPAPERS_DIR)}
    fresh = []

    for path in images:

        out_name = render_name(path, size)
        out_path = os.path.join(WALLPAPERS_DIR, out_name)

        if (
            os.path.exists(out_path)
            and os.path.getmtime(out_path)
            >= max(os.path.getmtime(path), floor)
        ):
            continue

        render_wallpaper(path, out_path, config, size)

        print(f"Rendered: {out_name}")

        if source_stem(out_name) not in known:
            fresh.append(out_name)

    if failures:
        print(
            f"{failures}/{len(all_urls)} downloads failed"
            " (still on the board, keeping them)."
        )

    # Keyed off the board listing rather than off what downloaded, so a pin that
    # is still pinned survives a failed download instead of looking unpinned.
    on_board = {
        cache_name(url)
        for url in all_urls
        if url.startswith("https://")
    }

    if degraded:
        print("Board list came from RSS, which may omit older pins; skipping prune.")
    else:
        prune(WALLPAPERS_DIR, {render_name(name, size) for name in on_board})
        prune(IMAGE_DIR, on_board)

    return fresh


def do_refresh(config, state):

    now = int(time.time())

    state["last_refresh_attempt"] = now
    save_state(state)

    log("Refreshing from Pinterest...")

    try:
        fresh = sync_boards(config, state)

    except Exception as e:

        state["fail_streak"] = state.get("fail_streak", 0) + 1

        log(f"Refresh failed: {e}")

        if state["fail_streak"] == 3:
            notify(
                config,
                "Pinterest Wallpaper",
                "Refresh failed 3 times in a row."
                " The board may be private or gone."
            )

        return

    state["last_refresh_ok"] = now
    state["fail_streak"] = 0

    if not fresh:
        return

    if config.get("new_pins_first"):
        state["deck"] = fresh + [
            name for name in state.get("deck", []) if name not in fresh
        ]

    label = "pin" if len(fresh) == 1 else "pins"
    where = ", ".join(state.get("board_names") or ["your board"])

    notify(
        config,
        "Pinterest Wallpaper",
        f"{len(fresh)} new {label} on {where}"
    )


def refresh_if_due(config, state):

    now = int(time.time())

    fresh_enough = (
        now - state.get("last_refresh_ok", 0)
        < config.get("refresh_hours", 24) * 3600
    )

    if wallpaper_pool(state) and fresh_enough:
        return

    if (
        now - state.get("last_refresh_attempt", 0)
        < config.get("retry_hours", 1) * 3600
    ):
        log("Board refresh tried recently; skipping.")
        return

    do_refresh(config, state)


# =========================
# ROTATION
# =========================

def wallpaper_pool(state):

    banned = set(state.get("banned", []))

    return sorted(
        name for name in os.listdir(WALLPAPERS_DIR)
        if name.endswith(".jpg") and source_stem(name) not in banned
    )


def current_name(state):

    history = state.get("history", [])

    return history[-1] if history else None


def refill_deck(pool, avoid=None):

    deck = list(pool)
    random.shuffle(deck)

    if len(deck) > 1 and avoid and deck[0] == avoid:
        deck[0], deck[-1] = deck[-1], deck[0]

    return deck


def advance(config, state):

    pool = wallpaper_pool(state)

    if not pool:
        log("No wallpapers available yet.")
        return

    deck = [name for name in state.get("deck", []) if name in pool]

    if not deck:
        deck = refill_deck(pool, avoid=current_name(state))

    name = deck.pop(0)
    state["deck"] = deck

    try:
        set_wallpaper(os.path.join(WALLPAPERS_DIR, name))
    except Exception as e:
        state["deck"] = [name] + deck
        log(f"Failed to set wallpaper ({e}); check System Events permission.")
        return

    history = state.setdefault("history", [])
    history.append(name)
    del history[:-HISTORY_LIMIT]

    log(f"Wallpaper -> {name}")


def go_back(config, state):

    history = state.get("history", [])

    if len(history) < 2:
        log("No previous wallpaper to go back to.")
        return

    state["deck"] = [history.pop()] + state.get("deck", [])

    target = history[-1]
    path = os.path.join(WALLPAPERS_DIR, target)

    if not os.path.exists(path):
        log(f"Previous wallpaper is gone: {target}")
        return

    try:
        set_wallpaper(path)
    except Exception as e:
        log(f"Failed to set wallpaper ({e}).")
        return

    log(f"Wallpaper -> {target}  (back)")


# =========================
# COMMANDS
# =========================

def fmt_time(stamp):

    if not stamp:
        return "never"

    delta = int(time.time()) - int(stamp)

    if delta < 3600:
        ago = f"{delta // 60}m ago"
    elif delta < 86400:
        ago = f"{delta // 3600}h ago"
    else:
        ago = f"{delta // 86400}d ago"

    return f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(stamp))}  ({ago})"


def cmd_status(config, state):

    size = target_size(config)
    pool = wallpaper_pool(state)
    deck = [name for name in state.get("deck", []) if name in pool]

    due_in = (
        state.get("last_refresh_ok", 0)
        + config.get("refresh_hours", 24) * 3600
        - int(time.time())
    )

    boards = state.get("board_names") or ["(not synced yet)"]

    print(f"Boards        : {', '.join(boards)}")

    for board in config.get("boards", []):
        print(f"                {board}")

    print(f"Resolution    : {size[0]}x{size[1]}  (config: {config.get('resolution')})")
    print(
        f"Render        : fit={config.get('fit_mode')}"
        f"  sharpen={config.get('sharpen')}"
        f"  menubar={config.get('menubar_gradient')}"
    )
    print(f"Wallpapers    : {len(pool)} in rotation, {len(state.get('banned', []))} banned")
    print(f"Current       : {current_name(state) or '-'}")
    print(f"Up next       : {deck[0] if deck else '(reshuffle)'}")
    print(f"Deck left     : {len(deck)} before reshuffle")
    print(f"Last refresh  : {fmt_time(state.get('last_refresh_ok'))}")

    if due_in <= 0:
        print("Next refresh  : due now")
    else:
        print(f"Next refresh  : in {due_in // 3600}h {due_in % 3600 // 60}m")

    if state.get("fail_streak"):
        print(f"Fail streak   : {state['fail_streak']} consecutive failures")


def cmd_ban(config, state):

    name = current_name(state)

    if not name:
        log("Nothing is showing right now, so there is nothing to ban.")
        return

    stem = source_stem(name)

    banned = state.setdefault("banned", [])

    if stem not in banned:
        banned.append(stem)

    state["deck"] = [n for n in state.get("deck", []) if source_stem(n) != stem]
    state["history"] = [n for n in state.get("history", []) if source_stem(n) != stem]

    log(f"Banned {name}")

    advance(config, state)


def cmd_unban(config, state):

    count = len(state.get("banned", []))

    state["banned"] = []

    log(f"Cleared {count} banned image(s).")


def cmd_shuffle(config, state):

    state["deck"] = []

    advance(config, state)


def cmd_tick(config, state):

    refresh_if_due(config, state)

    advance(config, state)


USAGE = """usage: pinterest_wallpaper.py <command>

  tick      refresh if due, then show the next wallpaper (what launchd runs)
  next      show the next wallpaper now
  prev      go back to the previous wallpaper
  shuffle   reshuffle the deck and show a new wallpaper
  refresh   re-read the boards from Pinterest right now
  status    show what the tool is doing
  ban       drop the current wallpaper from rotation, then advance
  unban     clear the ban list
"""

COMMANDS = {
    "tick": cmd_tick,
    "next": advance,
    "prev": go_back,
    "shuffle": cmd_shuffle,
    "refresh": do_refresh,
    "status": cmd_status,
    "ban": cmd_ban,
    "unban": cmd_unban,
}


if __name__ == "__main__":

    command = sys.argv[1] if len(sys.argv) > 1 else ""

    if command not in COMMANDS:
        sys.exit(
            USAGE if not command
            else f"unknown command: {command}\n\n{USAGE}"
        )

    config = load_config()
    state = load_state()

    try:
        COMMANDS[command](config, state)
    finally:
        save_state(state)
