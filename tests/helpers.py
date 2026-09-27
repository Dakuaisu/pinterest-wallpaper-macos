import copy
import html
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import requests
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pinterest_wallpaper as pw  # noqa: E402

BOARD = "https://www.pinterest.com/someone/cats/"
SCREEN = {"id": 1, "name": "Built-in", "width": 160, "height": 100}

REAL = {
    name: getattr(pw, name)
    for name in ("set_wallpapers", "detect_screens", "notify", "screen_locked", "log", "log_error")
}


def feed_pin(pin_id, url, color="#112233", width=None, height=None, videos=None):

    orig = {"url": url}

    if width:
        orig.update(width=width, height=height)

    return {"type": "pin", "id": pin_id, "dominant_color": color, "videos": videos, "images": {"orig": orig}}


def board_page(items, bookmark="-end-", escape=False, pin_count=99, modified="t1"):

    feed_key = json.dumps([["board_id", "42"], ["page_size", 25]])
    props = {
        "initialReduxState": {
            "resources": {
                "BoardFeedResource": {feed_key: {"data": items, "nextBookmark": bookmark}},
                "BoardResource": {"x": {"data": {
                    "name": "Cats", "pin_count": pin_count, "board_order_modified_at": modified,
                }}},
            }
        }
    }
    payload = json.dumps(props)

    if escape:
        payload = html.escape(payload)

    return f'<html><script id="__PWS_INITIAL_PROPS__" type="application/json">{payload}</script></html>'


def rss_item(pin_id, thumb):

    return (
        f"<item><title> </title><link>https://www.pinterest.com/pin/{pin_id}/</link>"
        f"<description>&lt;img src=&quot;{thumb}&quot;&gt;</description></item>"
    )


class FakeResponse:

    def __init__(self, text="", status=200, body=None, content=b"", content_type="text/html"):
        self.text = text
        self.status_code = status
        self._body = body
        self.content = content
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")

    def json(self):
        return self._body


def image_bytes(size=(400, 300), color="red", fmt="JPEG", mode="RGB", **save):

    buffer = io.BytesIO()
    Image.new(mode, size, color).save(buffer, fmt, **save)
    return buffer.getvalue()


def image_session(content=None, content_type="image/jpeg"):

    body = image_bytes() if content is None else content

    return mock.Mock(get=lambda url, **kwargs: FakeResponse(content=body, content_type=content_type))


def make_pin(key, board=BOARD, width=0, height=0, video=False, color=""):

    return {
        "key": key,
        "url": f"https://i.pinimg.com/originals/{key}.jpg",
        "board": board,
        "color": color,
        "width": width,
        "height": height,
        "video": video,
    }


def make_listing(keys, url=BOARD, complete=True, **pin_fields):

    return {
        "url": url,
        "name": "Cats",
        "pin_count": len(keys),
        "complete": complete,
        "paged": False,
        "reused": False,
        "stamp": None,
        "pins": [make_pin(key, url, **pin_fields) for key in keys],
    }


class Sandbox(unittest.TestCase):

    def setUp(self):

        self.root = tempfile.mkdtemp()
        patches = {
            "BASE_DIR": self.root,
            "IMAGE_DIR": os.path.join(self.root, "images"),
            "UPSCALED_DIR": os.path.join(self.root, "upscaled"),
            "WALLPAPERS_DIR": os.path.join(self.root, "wallpapers"),
            "CONFIG_FILE": os.path.join(self.root, "config.json"),
            "STATE_FILE": os.path.join(self.root, ".state.json"),
            "STATE_LOCK": os.path.join(self.root, ".state.lock"),
            "REFRESH_LOCK": os.path.join(self.root, ".refresh.lock"),
            "LAUNCH_AGENTS": os.path.join(self.root, "LaunchAgents"),
            "PW": os.path.join(self.root, "pw"),
        }

        for name, value in patches.items():
            patcher = mock.patch.object(pw, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        for directory in ("images", "upscaled", "wallpapers"):
            os.makedirs(os.path.join(self.root, directory))

        self.shown = []
        self.screens = [dict(SCREEN)]

        for name, replacement in {
            "set_wallpapers": lambda assignment: self.shown.append([os.path.basename(p) for _, p in assignment]),
            "detect_screens": lambda: [dict(s) for s in self.screens],
            "notify": mock.Mock(),
            "screen_locked": lambda: False,
            "log": mock.Mock(),
            "log_error": mock.Mock(),
        }.items():
            patcher = mock.patch.object(pw, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.addCleanup(shutil.rmtree, self.root)
        self.config = copy.deepcopy(pw.DEFAULT_CONFIG)
        self.config["boards"] = [BOARD]

    def add_pin(self, key, size=(400, 300), color="red", luma=None):

        Image.new("RGB", size, color).save(os.path.join(pw.IMAGE_DIR, f"{key}.jpg"))

        with pw.locked_state() as state:
            meta = pw.image_meta(Image.new("RGB", size, color))
            if luma is not None:
                meta["luma"] = luma
            state["pins"][key] = dict(meta, url=f"https://i.pinimg.com/originals/{key}.jpg", board=BOARD, color="")
            state["seen"].append(key)

    def state(self):

        return pw.load_state()

    def make_upscaler(self):

        stub = os.path.join(self.root, "fake-upscaler")

        with open(stub, "w") as handle:
            handle.write(
                f"#!{sys.executable}\n"
                "import sys\nfrom PIL import Image\n"
                "a = sys.argv\n"
                "img = Image.open(a[a.index('-i') + 1])\n"
                "img.resize((img.width * 4, img.height * 4)).save(a[a.index('-o') + 1])\n"
            )

        os.chmod(stub, 0o755)
        return stub

    def printed(self, command, *args):

        with mock.patch("builtins.print") as fake_print:
            code = command(self.config, list(args))

        return code, "\n".join(" ".join(str(a) for a in call.args) for call in fake_print.call_args_list)
