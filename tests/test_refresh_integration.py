import copy
from unittest import mock

import pinterest_wallpaper as pw
import wallpaper_refresh
from helpers import BOARD, REAL, Sandbox, make_listing


class RefreshIntegrationTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.config["boards"] = [BOARD]

    def test_captured_production_wrapper_passes_paths_and_120_second_limit(self):
        result = {"listings": [make_listing([], complete=True)], "failures": {}, "offline": [],
                  "pins": {}, "retained": [], "retry": False}
        with mock.patch.object(wallpaper_refresh, "run_sync", return_value=result) as run, mock.patch.object(pw, "sync_with_deadline", REAL["sync_with_deadline"]):
            self.assertTrue(pw.do_refresh(self.config))
        args, kwargs = run.call_args
        self.assertEqual(args[0], self.config)
        self.assertEqual(args[2], {"BASE_DIR": self.root, "IMAGE_DIR": pw.IMAGE_DIR,
                                   "UPSCALED_DIR": pw.UPSCALED_DIR, "WALLPAPERS_DIR": pw.WALLPAPERS_DIR})
        self.assertEqual(kwargs["timeout"], 120)
        self.assertGreater(pw.load_state()["last_refresh_ok"], 0)

    def test_offline_from_supervisor_preserves_next_tick_retry(self):
        with pw.locked_state() as state:
            state["last_refresh_attempt"] = 17
            state["last_refresh_ok"] = 11
        with mock.patch.object(wallpaper_refresh, "run_sync", side_effect=wallpaper_refresh.RefreshOffline("disconnected")), mock.patch.object(pw, "sync_with_deadline", REAL["sync_with_deadline"]):
            self.assertFalse(pw.do_refresh(self.config))
        state = pw.load_state()
        self.assertEqual(state["last_refresh_attempt"], 17)
        self.assertEqual(state["last_refresh_ok"], 11)
        self.assertTrue(state["refresh_pending"])

    def test_timeout_and_worker_error_never_apply_partial_result(self):
        for failure in (wallpaper_refresh.RefreshTimeout("expired"), wallpaper_refresh.RefreshWorkerError("worker failed")):
            with self.subTest(error=type(failure).__name__):
                with pw.locked_state() as state:
                    state["last_refresh_ok"] = 11
                    state["last_refresh_attempt"] = 0
                    state["refresh_pending"] = False
                    state["pins"] = {"preserve": {"width": 400, "height": 300}}
                with mock.patch.object(pw, "apply_sync", side_effect=AssertionError("partial sync applied")) as apply, mock.patch.object(wallpaper_refresh, "run_sync", side_effect=failure), mock.patch.object(pw, "sync_with_deadline", REAL["sync_with_deadline"]):
                    self.assertFalse(pw.do_refresh(self.config))
                apply.assert_not_called()
                state = pw.load_state()
                self.assertEqual(state["last_refresh_ok"], 11)
                self.assertGreater(state["last_refresh_attempt"], 0)
                self.assertTrue(state["refresh_pending"])
                self.assertIn("preserve", state["pins"])

    def test_unsafe_launch_interval_fails_before_agent_teardown_or_file_write(self):
        config = copy.deepcopy(self.config)
        config["interval_minutes"] = 1e308
        with mock.patch.object(pw, "bootout") as bootout:
            self.assertEqual(pw.cmd_install(config, []), 1)
        bootout.assert_not_called()
        self.assertFalse(pw.os.path.exists(pw.plist_path()))
