import json
import os
import shutil
import sys
import types
from unittest import mock

import pinterest_wallpaper as pw
from helpers import Sandbox


class TuiBackendTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.add_pin("123", size=(80, 40))
        self.config.update(resolution="60x40", fit_mode="blur", fill_pins=["123"])

    def backend(self):
        import wallpaper_tui_backend
        if not os.path.exists(pw.CONFIG_FILE):
            pw.save_json(pw.CONFIG_FILE, self.config)
        return wallpaper_tui_backend.Backend(pw, self.config)

    def test_preview_matches_renderer_without_writing(self):
        from wallpaper_tui_backend import Draft
        backend = self.backend()
        pin = backend.pins()[0]
        state = self.state()
        files = pw.source_files()
        source, ai = pw.source_for(pin.key, self.config, files)
        image, _, _ = pw.decode(source)
        state["rotations"][pin.key] = 90
        state["fill_overrides"][pin.key] = False
        expected = pw.render_image(image, (60, 40), pw.render_settings(pin.key, self.config, state, ai), state["pins"][pin.key].get("color"))
        before = {name: os.stat(os.path.join(self.root, name)).st_mtime_ns for name in os.listdir(self.root)}
        actual = backend.preview(pin, Draft(angle=90, fill=False), False)
        self.assertEqual(actual.tobytes(), expected.tobytes())
        self.assertEqual(before, {name: os.stat(os.path.join(self.root, name)).st_mtime_ns for name in os.listdir(self.root)})
        self.assertEqual(self.shown, [])

    def test_save_explicit_false_overrides_config(self):
        from wallpaper_tui_backend import Draft
        backend = self.backend()
        pin = backend.pins()[0]
        result = backend.save(pin, Draft(angle=90, fill=False))
        state = self.state()
        self.assertEqual((result.angle, result.fill), (90, False))
        self.assertEqual(state["fill_overrides"][pin.key], False)
        self.assertEqual(pw.render_settings(pin.key, self.config, state, False)["fit"], "blur")

    def test_save_rejects_conflict_on_changed_field(self):
        from wallpaper_tui_backend import Draft, TuiError
        backend = self.backend()
        pin = backend.pins()[0]
        with pw.locked_state() as state:
            state["rotations"][pin.key] = 180
        with self.assertRaises(TuiError):
            backend.save(pin, Draft(angle=90))
        self.assertEqual(self.state()["rotations"][pin.key], 180)

    def test_apply_uses_draft_on_all_displays_without_saving_it(self):
        from wallpaper_tui_backend import Draft
        self.screens.append(dict(self.screens[0], id=2))
        backend = self.backend()
        pin = backend.pins()[0]
        backend.apply(pin, Draft(angle=90, fill=False))
        state = self.state()
        self.assertEqual(state["current"], [pin.key, pin.key])
        self.assertEqual(state["history"][-1], [pin.key])
        self.assertNotIn(pin.key, state["rotations"])
        self.assertNotIn(pin.key, state["fill_overrides"])
        self.assertEqual(len(self.shown[-1]), 2)

    def test_apply_persists_reconciliation_on_desktop_failure(self):
        from wallpaper_tui_backend import Draft, TuiError
        backend = self.backend()
        pin = backend.pins()[0]
        def fail(assignment):
            raise pw.WallpaperTransactionError(["failed"], [{"id": "1", "path": assignment[0][1]}])
        with mock.patch.object(pw, "set_wallpapers", side_effect=fail), self.assertRaises(TuiError):
            backend.apply(pin, Draft(angle=90))
        self.assertEqual(self.state()["current"], [pin.key])
        self.assertEqual(self.state()["history"], [])

    def test_non_tty_cli_does_not_create_files(self):
        with mock.patch.object(pw.sys.stdin, "isatty", return_value=False), mock.patch.object(pw.sys.stderr, "write"):
            self.assertEqual(pw.main(["tui"]), 2)
        self.assertFalse(os.path.exists(pw.CONFIG_FILE))

    def test_save_rejects_missing_source(self):
        from wallpaper_tui_backend import Draft, TuiError
        backend = self.backend()
        pin = backend.pins()[0]
        os.unlink(os.path.join(pw.IMAGE_DIR, pin.key + ".jpg"))
        with self.assertRaises(TuiError):
            backend.save(pin, Draft(angle=90))
        self.assertNotIn(pin.key, self.state()["rotations"])

    def test_save_rejects_busy_lock_without_waiting(self):
        from wallpaper_tui_backend import Draft, TuiError
        backend = self.backend()
        pin = backend.pins()[0]
        with pw.file_lock(pw.STATE_LOCK), self.assertRaises(TuiError):
            backend.save(pin, Draft(angle=90))

    def test_original_preview_ignores_draft_rotation_and_cached_ai(self):
        from wallpaper_tui_backend import Draft
        backend = self.backend()
        pin = backend.pins()[0]
        image = backend.preview(pin, Draft(angle=90, fill=False), True)
        self.assertEqual(image.size, (80, 40))

    def test_unmodified_field_does_not_conflict(self):
        from wallpaper_tui_backend import Draft
        backend = self.backend()
        pin = backend.pins()[0]
        with pw.locked_state() as state:
            state["rotations"][pin.key] = 180
        result = backend.save(pin, Draft(fill=False))
        self.assertEqual((result.angle, result.fill), (180, False))

    def test_corrupt_ai_preview_falls_back_without_deleting_it(self):
        from wallpaper_tui_backend import Draft
        self.config["upscaler"] = sys.executable
        files = pw.source_files()
        ai_path = pw.upscale_cache_paths("123", self.config["upscaler"], self.config["upscaler_model"],
                                         os.path.join(pw.IMAGE_DIR, files["123"]))[0]
        with open(ai_path, "wb") as handle:
            handle.write(b"not an image")
        backend = self.backend()
        pin = backend.pins()[0]
        image = backend.preview(pin, Draft(), False)
        self.assertEqual(image.size, (60, 40))
        self.assertTrue(os.path.exists(ai_path))

    def test_tty_command_passes_core_to_lazy_ui(self):
        run = mock.Mock(return_value=0)
        module = types.ModuleType("wallpaper_tui")
        module.run = run
        with mock.patch.dict(sys.modules, {"wallpaper_tui": module}), mock.patch.object(
                pw.sys.stdin, "isatty", return_value=True), mock.patch.object(pw.sys.stdout, "isatty", return_value=True):
            self.assertEqual(pw.main(["tui"]), 0)
        self.assertIs(run.call_args.args[0].core, pw)

    def test_save_conflicts_when_fill_config_changes(self):
        from wallpaper_tui_backend import Draft, TuiError
        pw.save_json(pw.CONFIG_FILE, self.config)
        backend = self.backend()
        pin = backend.pins()[0]
        changed = dict(self.config, fill_pins=[])
        pw.save_json(pw.CONFIG_FILE, changed)
        with self.assertRaises(TuiError):
            backend.save(pin, Draft(fill=False))
        self.assertNotIn(pin.key, self.state()["fill_overrides"])

    def test_apply_uses_updated_render_config_without_overwriting_state(self):
        from wallpaper_tui_backend import Draft
        pw.save_json(pw.CONFIG_FILE, self.config)
        backend = self.backend()
        pin = backend.pins()[0]
        pw.save_json(pw.CONFIG_FILE, dict(self.config, resolution="32x24"))
        with pw.locked_state() as state:
            state["favorites"] = [pin.key]
        backend.apply(pin, Draft())
        self.assertIn("32x24", self.shown[-1][0])
        self.assertEqual(self.state()["favorites"], [pin.key])

    def test_save_preserves_unrelated_config_and_state_changes(self):
        from wallpaper_tui_backend import Draft
        backend = self.backend()
        pin = backend.pins()[0]
        changed = dict(self.config, fill_pins=[], crop="center")
        pw.save_json(pw.CONFIG_FILE, changed)
        with pw.locked_state() as state:
            state["favorites"] = [pin.key]
        result = backend.save(pin, Draft(angle=90))
        self.assertFalse(result.fill)
        self.assertEqual(self.state()["favorites"], [pin.key])
        with open(pw.CONFIG_FILE) as handle:
            self.assertEqual(json.load(handle)["crop"], "center")

    def test_corrupt_state_snapshot_does_not_repair_or_write(self):
        from wallpaper_tui_backend import TuiError
        backend = self.backend()
        with open(pw.STATE_FILE, "w") as handle:
            handle.write("{")
        before = set(os.listdir(self.root))
        with self.assertRaises(TuiError):
            backend.pins()
        with open(pw.STATE_FILE) as handle:
            self.assertEqual(handle.read(), "{")
        self.assertEqual(set(os.listdir(self.root)), before)

    def test_apply_conflicts_when_fill_config_changes(self):
        from wallpaper_tui_backend import Draft, TuiError
        backend = self.backend()
        pin = backend.pins()[0]
        pw.save_json(pw.CONFIG_FILE, dict(self.config, fill_pins=[]))
        with self.assertRaises(TuiError):
            backend.apply(pin, Draft(fill=False))
        self.assertEqual(self.shown, [])

    def test_cli_empty_library_does_not_create_config_or_directories(self):
        module = types.ModuleType("wallpaper_tui")
        module.run = lambda backend: len(backend.pins())
        for directory in (pw.IMAGE_DIR, pw.UPSCALED_DIR, pw.WALLPAPERS_DIR):
            shutil.rmtree(directory)
        with mock.patch.dict(sys.modules, {"wallpaper_tui": module}), mock.patch.object(
                pw.sys.stdin, "isatty", return_value=True), mock.patch.object(pw.sys.stdout, "isatty", return_value=True):
            self.assertEqual(pw.main(["tui"]), 0)
        self.assertFalse(os.path.exists(pw.CONFIG_FILE))
        self.assertFalse(os.path.exists(pw.IMAGE_DIR))

    def test_pin_listing_scans_sources_once_and_reports_dimensions(self):
        self.add_pin("456", size=(70, 35))
        backend = self.backend()
        with mock.patch.object(pw, "source_files", wraps=pw.source_files) as files:
            pins = backend.pins()
        self.assertEqual(files.call_count, 1)
        self.assertTrue(all("x" in pin.note for pin in pins))

    def test_tty_cli_with_invalid_config_does_not_write_during_preview(self):
        with open(pw.CONFIG_FILE, "w") as handle:
            json.dump({"fit_mode": "invalid"}, handle)
        module = types.ModuleType("wallpaper_tui")
        module.run = lambda backend: len(backend.pins()) and 0
        before = set(os.listdir(self.root))
        with mock.patch.dict(sys.modules, {"wallpaper_tui": module}), mock.patch.object(
                pw.sys.stdin, "isatty", return_value=True), mock.patch.object(pw.sys.stdout, "isatty", return_value=True):
            self.assertEqual(pw.main(["tui"]), 0)
        self.assertEqual(set(os.listdir(self.root)), before)
