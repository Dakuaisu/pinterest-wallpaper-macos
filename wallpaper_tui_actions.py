"""Non-interactive, single-flight control-center actions.

Closing the browser does not terminate a running action; it may finish after quit.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import unicodedata

COMMANDS = frozenset({"next", "prev", "shuffle", "pause", "resume", "ban", "bans",
                      "unban", "fav", "rotate", "open", "refresh", "status", "orient",
                      "install", "uninstall", "doctor", "tick"})
OUTPUT_LIMIT = 8192
LAUNCHER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pw")


class ActionError(ValueError):
    """An action cannot be started or a config edit was rejected."""


def compact(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def sanitize(text: str) -> str:
    return "".join(c if c in "\n\t" or not unicodedata.category(c).startswith("C") else " "
                   for c in text)[:OUTPUT_LIMIT]


def edit_config(core, args: tuple[str, ...]) -> str:
    if len(args) != 3:
        raise ActionError("config requires key, value and expected value")
    key, raw, expected = args
    rule = core.CONFIG_RULES.get(key) or core.OPTIONAL_CONFIG_RULES.get(key)
    if rule is None:
        raise ActionError("unknown configuration key")
    value, old = json.loads(raw), json.loads(expected)
    if not rule[0](value):
        raise ActionError(f"{key} should be {rule[1]}")
    with core.file_lock(core.REFRESH_LOCK, wait=False) as acquired:
        if not acquired:
            raise ActionError("a refresh or configuration edit is running")
        with open(core.CONFIG_FILE, encoding="utf-8") as handle:
            config = json.load(handle)
        if not isinstance(config, dict) or key not in config:
            raise ActionError("configuration key is not present")
        if compact(config[key]) != compact(old):
            raise ActionError("configuration changed since it was loaded")
        config[key] = value
        core.save_json(core.CONFIG_FILE, config)
    return f"Updated {key}; refresh is not automatic."


def run_action(core, command: str, args: tuple[str, ...],
               launched: threading.Event | None = None) -> tuple[bool, str]:
    launched = launched or threading.Event()
    try:
        if command == "config":
            return True, edit_config(core, args)
        # A regular temporary file lets the child continue writing after the UI exits.
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen([LAUNCHER, command, *args], stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT, close_fds=True)
            launched.set()
            code = process.wait()
            output.seek(0)
            data = output.read(OUTPUT_LIMIT + 1)
        text = sanitize(data[:OUTPUT_LIMIT].decode("utf-8", errors="replace"))
        if len(data) > OUTPUT_LIMIT:
            text = text[:OUTPUT_LIMIT - 15] + "\n[truncated]"
        return code == 0, text or f"{command} exited with status {code}"
    except (OSError, ValueError, OverflowError) as error:
        return False, sanitize(str(error))
    finally:
        launched.set()


class Actions:
    """Own the mutable state of one background action and its unclaimed result."""

    def __init__(self, core):
        self.core = core
        self.thread: threading.Thread | None = None
        self.result: tuple[bool, str] | None = None
        self.command = ""
        self.launched = threading.Event()

    @property
    def busy(self) -> bool:
        return self.thread is not None

    def start(self, command: str, args: tuple[str, ...]) -> None:
        if self.busy:
            raise ActionError("an action is already pending")
        if command not in COMMANDS and command != "config":
            raise ActionError("command is not allowed")
        if not isinstance(args, tuple) or any(not isinstance(arg, str) or "\x00" in arg for arg in args):
            raise ActionError("arguments must be strings")
        self.command = command
        self.launched = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(command, args), daemon=True)
        self.thread.start()

    def _run(self, command: str, args: tuple[str, ...]) -> None:
        self.result = run_action(self.core, command, args, self.launched)

    def take(self) -> tuple[bool, str] | None:
        if self.thread is None or self.thread.is_alive():
            return None
        result = self.result
        self.thread = None
        self.result = None
        return result

    def close(self) -> None:
        """Wait until the job is a child process or a finished config edit; the child can outlive the browser."""
        if self.thread is not None:
            self.launched.wait(10)
