"""Run board synchronization in a disposable, deadline-bound interpreter."""

import json
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Union

Json = Union[None, bool, int, float, str, list["Json"], dict[str, "Json"]]


class RefreshTimeout(TimeoutError):
    """The synchronization process exceeded its deadline."""


class RefreshOffline(Exception):
    """All boards were offline; the caller may preserve offline semantics."""


class RefreshWorkerError(RuntimeError):
    """The synchronization process failed or returned an invalid response."""


def supervise(command: list[str], payload: dict[str, Json], timeout: float) -> Json:
    """Run a worker with JSON input and a result file isolated from its logs."""
    with tempfile.TemporaryDirectory(prefix="wallpaper-refresh-") as directory:
        result_path = os.path.join(directory, "result.json")
        request = dict(payload, result_path=result_path)
        try:
            completed = subprocess.run(command, input=json.dumps(request), text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as error:
            raise RefreshTimeout(f"refresh exceeded {timeout} seconds") from error
        except OSError as error:
            raise RefreshWorkerError(f"could not start refresh worker: {error}") from error

        if completed.returncode:
            raise RefreshWorkerError(f"refresh worker exited with status {completed.returncode}")
        try:
            with open(result_path, encoding="utf-8") as result_file:
                response = json.load(result_file)
            if not isinstance(response, dict) or response.get("status") not in ("ok", "offline", "error"):
                raise ValueError("invalid response")
            if response["status"] == "ok":
                return response["result"]
            message = response["message"]
            if not isinstance(message, str):
                raise TypeError("invalid error message")
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise RefreshWorkerError(f"invalid refresh worker response: {error}") from error
        if response["status"] == "offline":
            raise RefreshOffline(message)
        raise RefreshWorkerError(message)


def run_sync(config: dict[str, Json], snapshot: dict[str, Json], paths: dict[str, str], timeout: float = 120) -> Json:
    """Return sync_boards' result, without loading or publishing application state."""
    names = ("BASE_DIR", "IMAGE_DIR", "UPSCALED_DIR", "WALLPAPERS_DIR")
    payload = {"config": config, "snapshot": snapshot, "paths": {name: os.fspath(paths[name]) for name in names}}
    return supervise([sys.executable, "-B", str(Path(__file__).resolve())], payload, timeout)


def _worker() -> None:
    request = json.load(sys.stdin)
    import pinterest_wallpaper

    try:
        for name in ("BASE_DIR", "IMAGE_DIR", "UPSCALED_DIR", "WALLPAPERS_DIR"):
            setattr(pinterest_wallpaper, name, request["paths"][name])
        result = pinterest_wallpaper.sync_boards(request["config"], request["snapshot"])
        response = {"status": "ok", "result": result}
    except pinterest_wallpaper.Offline as error:
        response = {"status": "offline", "message": str(error)}
    except Exception as error:
        traceback.print_exc()
        response = {"status": "error", "message": f"{type(error).__name__}: {error}"}
    with open(request["result_path"], "w", encoding="utf-8") as result_file:
        json.dump(response, result_file)


if __name__ == "__main__":
    _worker()
