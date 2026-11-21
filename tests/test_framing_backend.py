from __future__ import annotations

from functools import partial
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageStat

import pinterest_wallpaper as pw
from helpers import Sandbox
from wallpaper_framing import Framing
from wallpaper_tui_backend import Backend, Draft, Pin, TuiError


class FramingBackendTests(Sandbox):
    def setUp(self) -> None:
        super().setUp()
        self.add_pin("123", size=(160, 80))
        self.source = Path(pw.IMAGE_DIR) / "123.jpg"
        with Image.new("RGB", (160, 80), "red") as image:
            draw = ImageDraw.Draw(image)
            for index, color in enumerate(("green", "blue", "yellow"), start=1):
                draw.rectangle((40 * index, 0, 40 * index + 39, 79), fill=color)
            image.save(self.source)
        self.config.update(resolution="auto", fit_mode="cover", crop="center", backdrop="color", sharpen=False)
        pw.save_json(pw.CONFIG_FILE, self.config)
        self.screens[:] = [dict(self.screens[0], width=160, height=80),
                           dict(self.screens[0], id=17, width=80, height=80)]
        with pw.locked_state() as state:
            state["screens"] = self.screens
            state["pins"]["123"]["color"] = "#114455"
        self.backend = Backend(pw, self.config)
        self.pin = self.backend.pins()[0]

    def assert_draft_rejected(self, pin: Pin, draft: Draft, message: str) -> None:
        state_path = Path(pw.STATE_FILE)
        before = state_path.read_bytes()
        for name, operation in (("preview", partial(self.backend.preview, original=False)),
                                ("save", self.backend.save), ("apply", self.backend.apply)):
            with self.subTest(operation=name), self.assertRaisesRegex(TuiError, message):
                operation(pin, draft)
        self.assertEqual(state_path.read_bytes(), before)
        self.assertEqual(self.shown, [])

    def test_framing_is_unchanged_by_default(self) -> None:
        draft = Draft(90, True, (0.25, 0.75), True)

        self.assertIsNone(self.pin.framing)
        self.assertIsNone(draft.framing)
        self.assertFalse(draft.framing_changed)

    def test_framing_edits_reject_a_missing_source(self) -> None:
        self.source.unlink()

        self.assert_draft_rejected(self.pin, Draft(framing_changed=True), "no longer available")

    def test_invalid_framing_leaves_state_and_desktop_unchanged(self) -> None:
        for framing in (Framing("invalid"), Framing("proportional", "invalid"),
                        Framing("proportional", zoom=0), Framing("stretch", width=True)):
            with self.subTest(framing=framing):
                draft = Draft(angle=90, framing=framing, framing_changed=True)
                self.assert_draft_rejected(self.pin, draft, "framing")

    def test_save_sets_and_resets_framing_override(self) -> None:
        framing = Framing("stretch", width=125, height=75)
        saved = self.backend.save(self.pin, Draft(framing=framing, framing_changed=True))

        self.assertEqual(saved.framing, framing)
        self.assertEqual(self.state()["framing"][self.pin.key], framing._asdict())
        self.assertEqual(self.backend.pins()[0].framing, framing)

        reset = self.backend.save(saved, Draft(framing_changed=True))

        self.assertIsNone(reset.framing)
        self.assertNotIn(self.pin.key, self.state()["framing"])

    def test_stale_framing_rejects_both_set_and_reset(self) -> None:
        other = Backend(pw, self.config)
        other.save(other.pins()[0], Draft(framing=Framing("stretch", height=75), framing_changed=True))
        self.backend.pins()

        for framing in (Framing("proportional", zoom=150), None):
            with self.subTest(framing=framing):
                self.assert_draft_rejected(self.pin, Draft(framing=framing, framing_changed=True), "framing changed")

    def test_framing_save_keeps_concurrent_focus_rotation_and_fill(self) -> None:
        point = (0.25, 0.75)
        other = Backend(pw, self.config)
        other.save(other.pins()[0], Draft(90, True, point, True))
        framing = Framing("proportional", "1:1", 125)

        saved = self.backend.save(self.pin, Draft(framing=framing, framing_changed=True))

        self.assertEqual((saved.angle, saved.fill, saved.focus, saved.framing), (90, True, point, framing))

    def test_independent_save_keeps_concurrent_framing_when_flag_is_false(self) -> None:
        framing = Framing("proportional", "1:1", 125)
        other = Backend(pw, self.config)
        other.save(other.pins()[0], Draft(framing=framing, framing_changed=True))
        point = (0.25, 0.75)

        saved = self.backend.save(self.pin, Draft(90, True, point, True, Framing("stretch")))

        self.assertEqual((saved.angle, saved.fill, saved.focus, saved.framing), (90, True, point, framing))

    def test_bounded_preview_matches_full_framing_render(self) -> None:
        baseline = self.backend.preview(self.pin, Draft(), False)
        for framing in (Framing("proportional", "1:1", 150), Framing("stretch", width=150, height=50)):
            with self.subTest(framing=framing):
                draft = Draft(focus=(0.75, 0.25), focus_changed=True, framing=framing, framing_changed=True)
                full = self.backend.preview(self.pin, draft, False)
                bounded = self.backend.preview_task(self.pin, draft, False, (40, 40))()

                self.assertEqual(bounded.size, (40, 20))
                self.assertGreater(self.mean_difference(full, baseline), 10)
                expected = full.resize(bounded.size, Image.Resampling.LANCZOS)
                self.assertLess(self.mean_difference(bounded, expected), 4)

    def test_focus_guide_outlines_the_framed_crop_on_the_rotated_photo(self) -> None:
        draft = Draft(angle=90, focus=(0.25, 0.25), focus_changed=True,
                      framing=Framing("proportional", zoom=200), framing_changed=True)

        guide = self.backend.preview(self.pin, draft, False, (80, 80), display=2, focus_editor=True)

        self.assertEqual(guide.size, (40, 80))
        # On display 2 the crop is x=40..80, y=20..60 on the 80x160 rotated photo.
        self.assertEqual(guide.getpixel((20, 15)), (255, 255, 255))
        self.assertEqual(guide.getpixel((30, 10)), (255, 255, 255))
        self.assertEqual(guide.getpixel((29, 20)), (255, 255, 255))
        self.assertNotEqual(guide.getpixel((12, 10)), (255, 255, 255))

    def test_apply_renders_draft_without_persisting_framing(self) -> None:
        saved_framing = Framing("stretch", height=50)
        pin = self.backend.save(self.pin, Draft(framing=saved_framing, framing_changed=True))
        for framing in (Framing("proportional", "1:1", 150), None):
            with self.subTest(framing=framing):
                draft = Draft(framing=framing, framing_changed=True)
                expected = self.backend.preview(pin, draft, False)

                self.backend.apply(pin, draft)

                with Image.open(Path(pw.WALLPAPERS_DIR) / self.shown[-1][0]) as rendered:
                    self.assertEqual(rendered.size, expected.size)
                    self.assertLess(self.mean_difference(rendered, expected), 4)
                self.assertEqual(self.state()["framing"][pin.key], saved_framing._asdict())

    @staticmethod
    def mean_difference(left: Image.Image, right: Image.Image) -> float:
        return sum(ImageStat.Stat(ImageChops.difference(left, right)).mean) / 3
