import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import wallpaper_refresh as refresh


def fixture_worker():
    payload = json.load(sys.stdin)
    mode = payload["mode"]
    if mode == "block":
        Path(payload["pidfile"]).write_text(str(os.getpid()))
        threading.Event().wait()
    if mode == "crash":
        sys.exit(7)
    print("worker log", flush=True)
    with open(payload["result_path"], "w") as result:
        json.dump(payload["response"], result)


class RefreshSupervisorTests(unittest.TestCase):
    def worker(self):
        return [sys.executable, "-B", __file__, "worker"]

    def test_success_is_separate_from_logs(self):
        response = {"status": "ok", "result": {"pins": {"123": {"width": 12}}}}
        self.assertEqual(refresh.supervise(self.worker(), {"mode": "reply", "response": response}, 3), response["result"])

    def test_structured_offline_and_worker_failure(self):
        for status, error in [("offline", refresh.RefreshOffline), ("error", refresh.RefreshWorkerError)]:
            with self.subTest(status=status), self.assertRaisesRegex(error, "fixture failure"):
                refresh.supervise(self.worker(), {"mode": "reply", "response": {"status": status, "message": "fixture failure"}}, 3)

    def test_crash_raises_worker_error(self):
        with self.assertRaises(refresh.RefreshWorkerError):
            refresh.supervise(self.worker(), {"mode": "crash"}, 3)

    def test_missing_or_malformed_result_is_worker_error(self):
        command = [sys.executable, "-B", "-c", "import sys; sys.stdin.read()"]
        with self.assertRaises(refresh.RefreshWorkerError):
            refresh.supervise(command, {}, 3)

    def test_temporary_result_directory_is_removed_after_success(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(refresh.tempfile, "tempdir", root):
                refresh.supervise(self.worker(), {"mode": "reply", "response": {"status": "ok", "result": 1}}, 3)
            self.assertEqual(os.listdir(root), [])

    def test_timeout_kills_and_reaps_child(self):
        with tempfile.TemporaryDirectory() as directory:
            pidfile = Path(directory) / "pid"
            with mock.patch.object(refresh.subprocess, "run", wraps=subprocess.run) as run, self.assertRaises(refresh.RefreshTimeout):
                refresh.supervise(self.worker(), {"mode": "block", "pidfile": str(pidfile)}, 3)
            pid = int(pidfile.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            with self.assertRaises(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)
            self.assertEqual(run.call_count, 1)

    def test_real_worker_syncs_empty_boards_without_state(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = {name: directory for name in ("BASE_DIR", "IMAGE_DIR", "UPSCALED_DIR", "WALLPAPERS_DIR")}
            config = {"boards": [], "skip_video": False, "skip_upscale_over": 0, "resolution": "auto"}
            snapshot = {"listings": {}, "rotations": {}, "pins": {}, "screens": []}
            result = refresh.run_sync(config, snapshot, paths, timeout=5)
            self.assertEqual(result, {"listings": [], "failures": {}, "offline": [], "pins": {}, "retained": [], "retry": False})
            self.assertEqual(os.listdir(directory), [])

    def test_worker_uses_passed_values_and_translates_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "wallpaper_refresh.py"
            shutil.copyfile(refresh.__file__, script)
            (Path(directory) / "pinterest_wallpaper.py").write_text(
                "class Offline(Exception): pass\n"
                "def sync_boards(config, snapshot):\n"
                "    if config.get('offline'): raise Offline('disconnected')\n"
                "    return {'config': config, 'snapshot': snapshot, 'paths': "
                "[BASE_DIR, IMAGE_DIR, UPSCALED_DIR, WALLPAPERS_DIR]}\n"
            )
            paths = {name: str(Path(directory) / name) for name in ("BASE_DIR", "IMAGE_DIR", "UPSCALED_DIR", "WALLPAPERS_DIR")}
            with mock.patch.object(refresh, "__file__", str(script)):
                result = refresh.run_sync({"value": 2}, {"value": 3}, paths, 3)
                self.assertEqual(result, {"config": {"value": 2}, "snapshot": {"value": 3}, "paths": list(paths.values())})
                with self.assertRaisesRegex(refresh.RefreshOffline, "disconnected"):
                    refresh.run_sync({"offline": True}, {}, paths, 3)


if __name__ == "__main__":
    if sys.argv[1:] == ["worker"]:
        fixture_worker()
    else:
        unittest.main()
