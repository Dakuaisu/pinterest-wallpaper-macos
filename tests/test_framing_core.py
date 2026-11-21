import copy

from PIL import Image

import wallpaper_framing
from helpers import Sandbox, pw


class FramingTests(Sandbox):
    def settings(self, framing, **kwargs):
        settings = pw.render_settings("pin", self.config, self.state(), False)
        settings.update(framing=framing, sharpen=False, backdrop="color", **kwargs)
        return settings

    def test_parse_rejects_invalid_values_and_accepts_hashable_defaults(self):
        framing = wallpaper_framing.parse_framing({"mode": "proportional"})
        self.assertEqual(framing, wallpaper_framing.Framing("proportional"))
        self.assertEqual(len({framing, framing}), 1)
        for value in (None, {}, {"mode": []}, {"mode": "stretch", "width": True},
                      {"mode": "stretch", "height": 401}, {"mode": "proportional", "aspect": "bad"},
                      {"mode": "stretch", "zoom": 24}, {"mode": "stretch", "extra": 1}):
            with self.subTest(value=value):
                self.assertIsNone(wallpaper_framing.parse_framing(value))

    def test_state_filters_each_pin_and_preserves_missing_overrides(self):
        pw.save_state({"version": 2, "framing": {"ok": {"mode": "stretch", "width": 50},
                      "../bad": {"mode": "stretch"}, "bad": {"mode": "bad"}}})
        self.assertEqual(self.state()["framing"], {"ok": wallpaper_framing.Framing("stretch", width=50)._asdict()})
        pw.save_state({"version": 2})
        self.assertEqual(self.state()["framing"], {})

    def test_cache_changes_only_for_overridden_pin_and_restores_on_clear(self):
        state = copy.deepcopy(self.state())
        baseline = pw.render_settings("pin", self.config, state, False)
        state["framing"] = {"pin": {"mode": "proportional", "zoom": 150}}
        changed = pw.render_settings("pin", self.config, state, False)
        self.assertNotEqual(pw.settings_tag(baseline), pw.settings_tag(changed))
        self.assertNotIn("framing", pw.render_settings("other", self.config, state, False))
        state["framing"].clear()
        self.assertEqual(pw.render_settings("pin", self.config, state, False), baseline)

    def test_proportional_crop_focus_rotation_matches_render(self):
        img = Image.new("RGB", (400, 100), "blue")
        img.paste("red", (300, 0, 400, 100))
        settings = self.settings(wallpaper_framing.Framing("proportional"), rotate=90, crop_focus=(1, 0.5))
        rotated = img.transpose(Image.Transpose.ROTATE_270)
        self.assertEqual(pw.cover_crop_box(rotated, (100, 100), settings), (0, 300, 100, 400))
        self.assertEqual(pw.render_image(img, (100, 100), settings).getpixel((50, 50)), (255, 0, 0))

    def test_zoom_out_and_stretch_use_backdrop_and_reference_dimensions(self):
        img = Image.new("RGB", (100, 100), "red")
        for framing, corner in ((wallpaper_framing.Framing("proportional", zoom=50), (0, 0)),
                                (wallpaper_framing.Framing("stretch", width=50), (0, 50))):
            with self.subTest(framing=framing):
                settings = self.settings(framing)
                out = pw.render_image(img, (100, 100), settings, "#000000")
                self.assertEqual(out.getpixel(corner), (0, 0, 0))
                self.assertEqual(out.getpixel((50, 50)), (255, 0, 0))
                preview = pw.render_image(img, (50, 50), settings, "#000000", reference_size=(100, 100))
                self.assertEqual(preview.getpixel((25, 25)), (255, 0, 0))

    def test_stretch_100_fills_display_even_for_nonmatching_source(self):
        img = Image.new("RGB", (200, 50), "red")
        out = pw.render_image(img, (100, 100), self.settings(wallpaper_framing.Framing("stretch")), "#000000")
        self.assertEqual(out.getpixel((0, 0)), (255, 0, 0))
        self.assertEqual(out.getpixel((99, 99)), (255, 0, 0))

    def test_aspect_frame_and_extreme_zoom_have_bounded_geometry(self):
        img = Image.new("RGB", (400, 200), "red")
        settings = self.settings(wallpaper_framing.Framing("proportional", aspect="1:1", zoom=400),
                                 crop_focus=(1, 0.5))
        geometry = wallpaper_framing.geometry(img.size, (200, 100), settings, (20, 10))
        self.assertEqual(geometry.destination, (5, 0, 15, 10))
        self.assertEqual(geometry.source, pw.cover_crop_box(img, (200, 100), settings))
        self.assertEqual(geometry.source, (350, 75, 400, 125))
        out = pw.render_image(img, (20, 10), settings, "#000000", reference_size=(200, 100))
        self.assertEqual(out.size, (20, 10))
        self.assertEqual(out.getpixel((0, 5)), (0, 0, 0))
        self.assertEqual(out.getpixel((10, 5)), (255, 0, 0))

    def test_render_cache_reuses_custom_render_and_changes_after_override(self):
        self.add_pin("pin")
        state, files = self.state(), pw.source_files()
        original = pw.ensure_render("pin", (100, 100), self.config, state, files)
        state["framing"]["pin"] = wallpaper_framing.Framing("stretch", width=50)._asdict()
        custom = pw.ensure_render("pin", (100, 100), self.config, state, files)
        self.assertNotEqual(original, custom)
        self.assertEqual(pw.ensure_render("pin", (100, 100), self.config, state, files), custom)
