import json
import os
import sys
import types
from unittest import mock

from PIL import Image

from helpers import FakeResponse, Sandbox, board_page, feed_pin, pw


class OrientationIntegration(Sandbox):
    def setUp(self):
        super().setUp()
        self.model = os.path.join(self.root, "model.onnx")
        with open(self.model, "wb") as handle:
            handle.write(b"model")
        self.config["orientation_model"] = self.model
        self.calls = []
        self.backend = types.SimpleNamespace(
            MODEL_SHA256="a" * 64, CACHE_VERSION=1, OrientationError=RuntimeError,
            predict=self.predict,
        )
        patcher = mock.patch.dict(sys.modules, {"wallpaper_orientation": self.backend})
        patcher.start()
        self.addCleanup(patcher.stop)

    def predict(self, model, sources, *, timeout=8):
        self.calls.append(list(sources))
        return [types.SimpleNamespace(angle=90, score=0.99) for source in sources]

    def test_scan_caches_and_render_uses_prediction(self):
        self.add_pin("123")
        self.assertEqual(pw.cmd_orient(self.config, ["scan"]), 0)
        state = self.state()
        self.assertEqual(pw.expected_render("123", (160, 100), self.config, state, pw.source_files())[1]["rotate"], 90)
        self.assertEqual(pw.cmd_orient(self.config, ["scan"]), 0)
        self.assertEqual(len(self.calls), 1)

    def test_preview_does_not_write_state(self):
        self.add_pin("123")
        with open(pw.STATE_FILE, "rb") as handle:
            before = handle.read()
        self.assertEqual(pw.cmd_orient(self.config, ["preview", "123"]), 0)
        with open(pw.STATE_FILE, "rb") as handle:
            self.assertEqual(handle.read(), before)

    def test_preview_passes_thirty_second_timeout(self):
        self.add_pin("123")
        self.backend.predict = mock.Mock(wraps=self.predict)
        self.assertEqual(pw.cmd_orient(self.config, ["preview", "123"]), 0)
        self.assertEqual(self.backend.predict.call_args.kwargs["timeout"], 30)

    def test_scan_passes_thirty_second_timeout(self):
        self.add_pin("123")
        self.backend.predict = mock.Mock(wraps=self.predict)
        self.assertEqual(pw.cmd_orient(self.config, ["scan"]), 0)
        self.assertEqual(self.backend.predict.call_args.kwargs["timeout"], 30)

    def test_preview_reports_clockwise_rotation(self):
        self.add_pin("123")
        code, output = self.printed(pw.cmd_orient, "preview", "123")
        self.assertEqual(code, 0)
        self.assertIn("90 degrees clockwise", output)

    def test_reset_is_an_explicit_zero_override(self):
        self.add_pin("123")
        pw.cmd_orient(self.config, ["scan"])
        with pw.locked_state() as state:
            state["current"] = ["123"]
        with mock.patch.object(pw, "reapply_current"):
            self.assertEqual(pw.cmd_rotate(self.config, ["reset"]), 0)
        state = self.state()
        self.assertEqual(state["rotations"]["123"], 0)
        self.assertEqual(pw.expected_render("123", (160, 100), self.config, state, pw.source_files())[1]["rotate"], 0)

    def test_manual_rotation_starts_from_prediction_and_auto_clears_it(self):
        self.add_pin("123")
        pw.cmd_orient(self.config, ["scan"])
        with pw.locked_state() as state:
            state["current"] = ["123"]
        with mock.patch.object(pw, "reapply_current"):
            pw.cmd_rotate(self.config, ["cw"])
            self.assertEqual(self.state()["rotations"]["123"], 180)
            pw.cmd_rotate(self.config, ["auto"])
        self.assertNotIn("123", self.state()["rotations"])
        self.assertEqual(pw.effective_angle("123", self.config, self.state()), 90)

    def test_replaced_source_invalidates_cached_prediction(self):
        self.add_pin("123")
        pw.cmd_orient(self.config, ["scan"])
        Image.new("RGB", (400, 300), "blue").save(os.path.join(pw.IMAGE_DIR, "123.jpg"))
        self.assertEqual(pw.effective_angle("123", self.config, self.state()), 0)
        pw.cmd_orient(self.config, ["scan"])
        self.assertEqual(len(self.calls), 2)

    def test_replaced_model_invalidates_cached_prediction(self):
        self.add_pin("123")
        pw.cmd_orient(self.config, ["scan"])
        with open(self.model, "wb") as handle:
            handle.write(b"different model")
        self.assertEqual(pw.effective_angle("123", self.config, self.state()), 0)

    def test_abstention_and_image_failure_are_both_cached(self):
        self.add_pin("123")
        self.add_pin("456")
        self.backend.predict = lambda model, sources, **kw: [types.SimpleNamespace(angle=None, score=0.5), None]
        with self.assertRaises(ValueError):
            pw.cmd_orient(self.config, ["scan"])
        cache = self.state()["orientation_cache"]
        self.assertIsNone(cache["123"]["angle"])
        self.assertEqual((cache["456"]["angle"], cache["456"]["score"]), (None, 0.0))
        self.calls.clear()
        self.backend.predict = lambda model, sources, **kw: self.calls.append(sources) or []
        pw.cmd_orient(self.config, ["scan"])
        self.assertEqual(self.calls, [])

    def test_scan_prioritizes_deck_and_is_bounded(self):
        for key in ("1", "2", "3", "4", "5"):
            self.add_pin(key)
        with pw.locked_state() as state:
            state["deck"] = ["5", "4"]
        pw.cmd_orient(self.config, ["scan"])
        self.assertEqual([os.path.basename(path) for path in self.calls[0]], ["5.jpg", "4.jpg", "1.jpg", "2.jpg"])
        self.assertEqual(len(self.state()["orientation_cache"]), 4)

    def test_removed_pin_is_not_resurrected_during_inference(self):
        self.add_pin("123")
        def remove_pin(model, sources, **kwargs):
            with pw.locked_state() as state:
                del state["pins"]["123"]
            return [types.SimpleNamespace(angle=90, score=1.0)]
        self.backend.predict = remove_pin
        pw.cmd_orient(self.config, ["scan"])
        self.assertNotIn("123", self.state()["orientation_cache"])

    def test_tick_skips_orientation_when_recent(self):
        self.add_pin("123")
        with mock.patch.object(pw, "refresh_due", return_value=False), mock.patch.object(pw, "tick_skip_reason", return_value="changed recently"):
            self.assertEqual(pw.cmd_tick(self.config, []), 0)
        self.assertEqual(self.calls, [])

    def test_tick_runs_orientation_after_wallpaper(self):
        self.add_pin("123")
        events = []
        self.backend.predict = lambda model, sources, **kwargs: events.append("orientation") or [types.SimpleNamespace(angle=90, score=1.0)]
        with mock.patch.object(pw, "refresh_due", return_value=False), mock.patch.object(pw, "tick_skip_reason", return_value=None), mock.patch.object(pw, "advance", side_effect=lambda config, state: events.append("wallpaper") or True):
            self.assertEqual(pw.cmd_tick(self.config, []), 0)
        self.assertEqual(events, ["wallpaper", "orientation"])

    def test_rotate_before_crop_uses_oriented_image(self):
        self.add_pin("123", size=(400, 300))
        pw.cmd_orient(self.config, ["scan"])
        source, settings, _ = pw.expected_render("123", (160, 100), self.config, self.state(), pw.source_files())
        with Image.open(source) as image, mock.patch.object(pw, "smart_centering", wraps=pw.smart_centering) as center:
            pw.render_image(image, (160, 100), settings)
        self.assertEqual(center.call_args.args[0].size, (300, 400))

    def test_preview_without_id_uses_saved_current_and_no_desktop_query(self):
        self.add_pin("123")
        with pw.locked_state() as state:
            state["current"] = ["123"]
        with mock.patch.object(pw, "detect_screens", side_effect=AssertionError("desktop queried")):
            self.assertEqual(pw.cmd_orient(self.config, ["preview"]), 0)

    def test_main_preview_does_not_create_state_or_wallpapers(self):
        self.add_pin("123")
        with open(pw.CONFIG_FILE, "w") as handle:
            json.dump({"orientation_model": self.model}, handle)
        with open(pw.STATE_FILE, "rb") as handle:
            before = handle.read()
        with mock.patch.object(pw, "detect_screens", side_effect=AssertionError("desktop queried")):
            self.assertEqual(pw.main(["orient", "preview", "123"]), 0)
        with open(pw.STATE_FILE, "rb") as handle:
            self.assertEqual(handle.read(), before)
        self.assertEqual(os.listdir(pw.WALLPAPERS_DIR), [])

    def test_relative_model_path_is_resolved_against_config(self):
        self.config["orientation_model"] = "model.onnx"
        self.add_pin("123")
        self.assertEqual(pw.cmd_orient(self.config, ["scan"]), 0)

    def test_invalid_model_config_is_rejected(self):
        with open(pw.CONFIG_FILE, "w") as handle:
            json.dump({"orientation_model": 12}, handle)
        config, problems = pw.load_config()
        self.assertEqual(config["orientation_model"], "")
        self.assertTrue(problems)

    def test_missing_backend_is_reported_for_explicit_command(self):
        self.add_pin("123")
        with mock.patch.dict(sys.modules, {"wallpaper_orientation": None}), self.assertRaisesRegex(ValueError, "backend unavailable"):
            pw.cmd_orient(self.config, ["scan"])

    def test_tick_worker_failure_sets_cooldown_without_breaking_wallpaper(self):
        self.add_pin("123")
        self.backend.predict = mock.Mock(side_effect=RuntimeError("worker failed"))
        with mock.patch.object(pw, "refresh_due", return_value=False), mock.patch.object(pw, "tick_skip_reason", return_value=None), mock.patch.object(pw, "advance", return_value=True):
            self.assertEqual(pw.cmd_tick(self.config, []), 0)
            self.assertEqual(pw.cmd_tick(self.config, []), 0)
        self.assertEqual(self.backend.predict.call_count, 1)
        self.assertGreater(self.state()["orientation_retry_after"], 0)

    def test_missing_model_still_allows_explicit_reset(self):
        self.add_pin("123")
        self.config["orientation_model"] = "missing.onnx"
        with pw.locked_state() as state:
            state["current"] = ["123"]
        with mock.patch.object(pw, "reapply_current"):
            self.assertEqual(pw.cmd_rotate(self.config, ["reset"]), 0)
        self.assertEqual(self.state()["rotations"]["123"], 0)

    def test_resolution_filter_keeps_recoverable_rotated_original(self):
        self.add_pin("123", size=(300, 600))
        self.config["skip_upscale_over"] = 1
        self.config["resolution"] = "600x300"
        item = feed_pin("123", "https://i.pinimg.com/originals/123.jpg", width=300, height=600)
        with mock.patch.object(pw.SESSION, "get", return_value=FakeResponse(board_page([item], pin_count=1))):
            result = pw.sync_boards(self.config, self.state())
        self.assertIn("123", result["pins"])
