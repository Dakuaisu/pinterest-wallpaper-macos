"""One daemon worker for bounded, latest-only terminal previews."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Hashable
from dataclasses import dataclass
from threading import Condition, Event, Thread
from typing import Callable

from PIL import Image


@dataclass(frozen=True)
class PreviewResult:
    image: Image.Image | None = None
    error: Exception | None = None
    pending: bool = False


class PreviewWorker:
    """Own the mutable request slot, completion signal, and small image cache."""

    def __init__(self, capacity: int = 8) -> None:
        self._capacity = capacity
        self._condition = Condition()
        self._cache: OrderedDict[Hashable, Image.Image] = OrderedDict()
        self._desired: Hashable | None = None
        self._active: Hashable | None = None
        self._pending: tuple[Hashable, Callable[[], Image.Image]] | None = None
        self._result: PreviewResult | None = None
        self._changed = False
        self._closed = False
        self._thread: Thread | None = None
        self.ready = Event()

    def request(self, key: Hashable, make_task: Callable[[], Callable[[], Image.Image]]) -> PreviewResult:
        with self._condition:
            if key != self._desired:
                self._desired = key
                self._pending = None
                self._result = None
                self._changed = False
                self.ready.clear()
            if key in self._cache:
                self._cache.move_to_end(key)
                return PreviewResult(image=self._cache[key])
            if self._result is not None:
                return self._result
            if self._active != key and self._pending is None:
                self._pending = (key, make_task())
                if self._thread is None:
                    self._thread = Thread(target=self._run, name="wallpaper-preview", daemon=True)
                    self._thread.start()
                self._condition.notify()
            return PreviewResult(pending=True)

    def take_changed(self) -> bool:
        with self._condition:
            changed = self._changed
            self._changed = False
            return changed

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._pending = None
            self._condition.notify()

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._closed:
                    self._condition.wait()
                if self._closed:
                    return
                key, task = self._pending
                self._pending = None
                self._active = key
            try:
                result = PreviewResult(image=task())
            except Exception as error:  # Worker boundary: propagate unexpected failures to the UI thread.
                result = PreviewResult(error=error)
            with self._condition:
                self._active = None
                if not self._closed and key == self._desired:
                    if result.image is not None:
                        self._cache[key] = result.image
                        self._cache.move_to_end(key)
                        while len(self._cache) > self._capacity:
                            self._cache.popitem(last=False)
                    self._result = result
                    self._changed = True
                    self.ready.set()
