from __future__ import annotations

from pathlib import Path
from threading import get_ident
from unittest import mock

from PIL import Image

import pinterest_wallpaper as pw
from helpers import Sandbox
from wallpaper_tui_backend import Backend, Draft, TuiError
from wallpaper_tui_preview import PreviewWorker


class CropFocusBackendTests(Sandbox):
    def setUp(self) -> None:
        super().setUp()
        self.add_pin("123", size=(160, 80))
        self.config.update(resolution="auto", fit_mode="cover", crop="center", max_upscale=0)
        pw.save_json(pw.CONFIG_FILE, self.config)
        self.screens[:] = [dict(self.screens[0], id=42, width=80, height=160),
                           dict(self.screens[0], id=17, width=80, height=80)]
        with pw.locked_state() as state:
            state["screens"] = self.screens
        self.backend = Backend(pw, self.config)
        self.pin = self.backend.pins()[0]

    def test_save_focus_round_trips_and_reset_removes_override(self) -> None:
        point = (0.25, 0.75)
        saved = self.backend.save(self.pin, Draft(focus=point, focus_changed=True))

        self.assertEqual(saved.focus, point)
        self.assertEqual(self.state()["crop_focus"][self.pin.key], list(point))
        self.assertEqual(self.backend.pins()[0].focus, point)

        reset = self.backend.save(saved, Draft(focus_changed=True))

        self.assertIsNone(reset.focus)
        self.assertNotIn(self.pin.key, self.state()["crop_focus"])

    def test_stale_focus_blocks_edits_but_not_unrelated_saves(self) -> None:
        with pw.locked_state() as state:
            state.setdefault("crop_focus", {})[self.pin.key] = [0.4, 0.7]

        with self.assertRaises(TuiError):
            self.backend.save(self.pin, Draft(focus_changed=True))

        saved = self.backend.save(self.pin, Draft(angle=90))
        self.assertEqual(saved.focus, (0.4, 0.7))

    def test_invalid_focus_cannot_be_saved(self) -> None:
        for point in ((-0.1, 0.5), (0.5, float("nan")), (True, 0.5)):
            with self.subTest(point=point), self.assertRaises(TuiError):
                self.backend.save(self.pin, Draft(focus=point, focus_changed=True))
        self.assertNotIn(self.pin.key, self.state().get("crop_focus", {}))

    def test_apply_uses_focus_for_rendering_without_persisting_it(self) -> None:
        with mock.patch.object(pw, "render_for_screens", wraps=pw.render_for_screens) as render:
            self.backend.apply(self.pin, Draft(focus=(0.2, 0.6), focus_changed=True))

        self.assertEqual(render.call_args.args[3]["crop_focus"][self.pin.key], [0.2, 0.6])
        self.assertNotIn(self.pin.key, self.state().get("crop_focus", {}))

    def test_ordinary_preview_uses_display_number_and_falls_back_to_first(self) -> None:
        self.assertEqual(self.backend.preview(self.pin, Draft(), False, (40, 40), display=2).size, (40, 40))
        self.assertEqual(self.backend.preview(self.pin, Draft(), False, (40, 40), display=99).size, (20, 40))

    def test_editor_draws_rotated_marker_and_display_crop_on_whole_photo(self) -> None:
        source = Path(pw.IMAGE_DIR) / "123.jpg"
        before = source.read_bytes(), Path(pw.STATE_FILE).read_bytes()
        draft = Draft(angle=90, focus=(0.25, 0.25), focus_changed=True)
        with Image.open(source) as image:
            reference = image.transpose(Image.Transpose.ROTATE_270).resize((40, 80), Image.Resampling.LANCZOS)

        preview = self.backend.preview(self.pin, draft, True, (80, 80), display=2, focus_editor=True)

        self.assertEqual(preview.size, (40, 80))
        self.assertNotEqual(preview.getpixel((29, 20)), reference.getpixel((29, 20)))
        self.assertTrue(any(preview.getpixel((15, y)) != reference.getpixel((15, y)) for y in range(39, 42)))
        self.assertEqual(preview.getpixel((15, 65)), reference.getpixel((15, 65)))
        self.assertEqual((source.read_bytes(), Path(pw.STATE_FILE).read_bytes()), before)
        self.assertEqual(self.shown, [])

    def test_editor_outlines_full_photo_when_real_display_exceeds_upscale_limit(self) -> None:
        self.backend.config["max_upscale"] = 0.75  # A 40px preview fits the cap, an 80px display does not.
        draft = Draft(angle=90, focus=(0.25, 0.25), focus_changed=True)

        preview = self.backend.preview(self.pin, draft, False, (40, 40), display=2, focus_editor=True)

        self.assertEqual(preview.size, (20, 40))
        self.assertNotEqual(preview.getpixel((0, 0)), (254, 0, 0))
        self.assertEqual(preview.getpixel((4, 20)), (254, 0, 0))
        self.assertNotEqual(preview.getpixel((14, 10)), (254, 0, 0))

    def test_editor_task_reads_sources_on_worker_using_captured_state_and_config(self) -> None:
        submitter = get_ident()
        decoder = pw.decode

        def decode(path: str) -> tuple[Image.Image, bytes | None, str | None]:
            self.assertNotEqual(get_ident(), submitter)
            return decoder(path)

        worker = PreviewWorker()
        self.addCleanup(worker.close)
        draft = Draft(angle=90, focus=(0.25, 0.25), focus_changed=True)
        with mock.patch.object(pw, "decode", side_effect=decode):
            task = self.backend.preview_task(self.pin, draft, False, (80, 80), display=2, focus_editor=True)
            self.backend.config["max_upscale"] = 0.5
            self.backend._snapshot["screens"].pop()
            worker.request("editor", lambda: task)
            self.assertTrue(worker.ready.wait(2))
            result = worker.request("editor", lambda: task)

        self.assertIsNone(result.error)
        assert result.image is not None
        self.assertEqual(result.image.size, (40, 80))
        self.assertTrue(any(result.image.getpixel((15, y)) != (254, 0, 0) for y in range(39, 42)))
