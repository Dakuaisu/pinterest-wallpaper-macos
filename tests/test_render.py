import copy
import io
import os
import time
import unittest
from unittest import mock

from PIL import Image, ImageDraw

from helpers import SCREEN, Sandbox, image_bytes, pw

TARGET = (3456, 2234)


def noisy(size):

    return Image.effect_noise(size, 90).convert("RGB")


class DecodeTests(Sandbox):

    def decode(self, data):

        return pw.decode(io.BytesIO(data))

    def test_transparency_is_flattened_onto_black(self):

        img, _, fmt = self.decode(image_bytes((10, 10), (255, 0, 0, 0), "PNG", mode="RGBA"))

        self.assertEqual((img.mode, img.getpixel((5, 5)), fmt), ("RGB", (0, 0, 0), "PNG"))

    def test_palette_transparency_is_flattened(self):

        buffer = io.BytesIO()
        Image.new("P", (10, 10), 1).save(buffer, "PNG", transparency=1)

        img, _, _ = self.decode(buffer.getvalue())

        self.assertEqual(img.getpixel((5, 5)), (0, 0, 0))

    def test_color_profile_is_kept_for_rgb_but_dropped_for_cmyk(self):

        _, icc, _ = self.decode(image_bytes(icc_profile=b"rgb-profile"))
        self.assertEqual(icc, b"rgb-profile")

        img, icc, _ = self.decode(image_bytes(color=(0, 0, 0, 0), mode="CMYK", icc_profile=b"cmyk-profile"))
        self.assertEqual((img.mode, icc), ("RGB", None))

    def test_exif_orientation_is_applied(self):

        exif = Image.Exif()
        exif[0x0112] = 6

        img, _, _ = self.decode(image_bytes((40, 20), exif=exif))

        self.assertEqual(img.size, (20, 40))

    def test_image_meta(self):

        meta = pw.image_meta(Image.new("RGB", (80, 40), "white"))

        self.assertEqual((meta["width"], meta["height"], meta["luma"], meta["avg"]), (80, 40, 255.0, "#ffffff"))

    def test_source_files_ignores_temp_and_hidden_files(self):

        for name in ("1.jpg", "2.png", ".DS_Store", "abc.tmp"):
            open(os.path.join(pw.IMAGE_DIR, name), "wb").close()

        self.assertEqual(pw.source_files(), {"1": "1.jpg", "2": "2.png"})


class CompositionTests(Sandbox):

    def settings(self, **overrides):

        return dict(pw.render_settings("k", self.config, copy.deepcopy(pw.DEFAULT_STATE), False), **overrides)

    def test_color_backdrop_uses_the_pin_color_or_the_average(self):

        img = Image.new("RGB", (10, 10), (10, 20, 30))

        self.assertEqual(pw.make_backdrop(img, (4, 4), "color", "#ff0000").getpixel((0, 0)), (255, 0, 0))
        self.assertEqual(pw.make_backdrop(img, (4, 4), "color", "red").getpixel((0, 0)), (10, 20, 30))
        self.assertEqual(pw.make_backdrop(img, (40, 30), "blur", "").size, (40, 30))

    def test_menubar_gradient_darkens_only_the_top_band(self):

        white = Image.new("RGB", (100, 1000), "white")
        shaded = pw.add_menubar_gradient(white)

        self.assertLess(shaded.getpixel((50, 0))[0], 200)
        self.assertEqual(shaded.getpixel((50, 999)), (255, 255, 255))

        rendered = pw.render_image(white, (100, 1000), self.settings(menubar=True))
        self.assertEqual(rendered.getpixel((50, 0)), shaded.getpixel((50, 0)))

    def test_smart_crop_follows_the_detail(self):

        tall = Image.new("RGB", (600, 1200), "white")
        tall.paste(noisy((600, 300)), (0, 100))
        wide = Image.new("RGB", (2000, 600), "black")
        wide.paste(noisy((400, 600)), (1550, 0))

        self.assertLess(pw.smart_centering(tall, TARGET)[1], 0.2)
        self.assertGreater(pw.smart_centering(wide, TARGET)[0], 0.8)

    def test_smart_crop_stays_centred_without_a_clear_reason(self):

        self.assertEqual(pw.smart_centering(Image.new("RGB", (2000, 600), "gray"), TARGET), (0.5, 0.5))
        self.assertEqual(pw.smart_centering(Image.new("RGB", TARGET, "gray"), TARGET), (0.5, 0.5))
        self.assertEqual(pw.smart_centering(noisy((4, 3)), TARGET), (0.5, 0.5))

    def test_small_text_in_a_corner_does_not_pull_the_crop(self):

        img = Image.new("RGB", (1778, 1000), "black")
        draw = ImageDraw.Draw(img)
        draw.ellipse((1250, 300, 1500, 700), fill="white")

        for x in range(10, 90, 6):
            draw.rectangle((x, 12, x + 2, 16), fill="white")

        self.assertEqual(pw.smart_centering(img, TARGET), (0.5, 0.5))

    def test_crop_setting_switches_between_smart_and_centre(self):

        wide = Image.new("RGB", (2000, 600), "black")
        wide.paste(noisy((400, 600)), (1550, 0))

        with mock.patch.object(pw, "smart_centering", wraps=pw.smart_centering) as smart:
            centre = pw.render_image(wide, (160, 100), self.settings(crop="center"))
            smart.assert_not_called()
            clever = pw.render_image(wide, (160, 100), self.settings(crop="smart"))
            smart.assert_called_once()

        self.assertNotEqual(list(centre.getdata()), list(clever.getdata()))

    def test_crop_is_part_of_the_render_tag(self):

        state = copy.deepcopy(pw.DEFAULT_STATE)
        smart = pw.settings_tag(pw.render_settings("k", self.config, state, False))
        self.config["crop"] = "center"

        self.assertNotEqual(pw.settings_tag(pw.render_settings("k", self.config, state, False)), smart)


class RenderCacheTests(Sandbox):

    def test_existing_render_is_reused_without_decoding(self):

        self.add_pin("a")
        state, files = self.state(), pw.source_files()
        first = pw.ensure_render("a", (160, 100), self.config, state, files)

        with mock.patch.object(pw, "decode", side_effect=AssertionError("re-rendered")):
            self.assertEqual(pw.ensure_render("a", (160, 100), self.config, state, files), first)

    def test_stale_renders_are_removed_unless_on_screen(self):

        self.add_pin("a")
        state, files = self.state(), pw.source_files()
        stale, shown, other_size = "a.160x100.deadbeef.jpg", "a.160x100.cafef00d.jpg", "a.320x200.deadbeef.jpg"

        for name in (stale, shown, other_size):
            open(os.path.join(pw.WALLPAPERS_DIR, name), "wb").close()

        state["on_screen"] = [shown]
        pw.ensure_render("a", (160, 100), self.config, state, files)
        remaining = set(os.listdir(pw.WALLPAPERS_DIR))

        self.assertNotIn(stale, remaining)
        self.assertTrue({shown, other_size} <= remaining)

    def test_render_for_screens_cycles_pins_over_screens(self):

        for key in "ab":
            self.add_pin(key)

        screens = [dict(SCREEN, id=i) for i in range(3)]
        assignment = pw.render_for_screens(["a", "b"], screens, self.config, self.state(), pw.source_files())

        self.assertEqual([os.path.basename(p).split(".")[0] for _, p in assignment], ["a", "b", "a"])

    def test_forget_source_removes_original_and_upscaled(self):

        self.add_pin("a")
        open(os.path.join(pw.UPSCALED_DIR, "a.jpg"), "wb").close()
        files = pw.source_files()

        pw.forget_source("a", files)

        self.assertNotIn("a", files)
        self.assertEqual(os.listdir(pw.IMAGE_DIR) + os.listdir(pw.UPSCALED_DIR), [])

    def test_prune_dir_handles_hidden_and_temporary_files(self):

        for name in (".DS_Store", "keep.jpg", "drop.jpg", "fresh.tmp", "old.tmp"):
            open(os.path.join(pw.WALLPAPERS_DIR, name), "wb").close()

        old = time.time() - 3600
        os.utime(os.path.join(pw.WALLPAPERS_DIR, "old.tmp"), (old, old))

        self.assertEqual(pw.prune_dir(pw.WALLPAPERS_DIR, lambda name: name == "keep.jpg"), 1)
        self.assertEqual(sorted(os.listdir(pw.WALLPAPERS_DIR)), [".DS_Store", "fresh.tmp", "keep.jpg"])


class SourceNoteTests(Sandbox):

    def state_with(self, width, height, rotation=0):

        state = copy.deepcopy(pw.DEFAULT_STATE)
        state["pins"]["p"] = {"width": width, "height": height}

        if rotation:
            state["rotations"]["p"] = rotation

        return state

    def test_cover_and_backdrop_notes(self):

        note = pw.source_note("p", self.state_with(735, 418), self.config, TARGET)
        self.assertEqual(note, "735x418 source, scaled 5.3x to fill 3456x2234")

        self.config["max_upscale"] = 2.5
        note = pw.source_note("p", self.state_with(735, 418), self.config, TARGET)
        self.assertEqual(note, "735x418 source, scaled 2.5x on a backdrop")

        self.config.update(fit_mode="blur", max_upscale=0)
        note = pw.source_note("p", self.state_with(3000, 3000), self.config, TARGET)
        self.assertEqual(note, "3000x3000 source, scaled 0.7x on a backdrop")

    def test_rotation_and_ai_upscaling_are_reflected(self):

        self.config["upscaler"] = "/bin/true"
        open(os.path.join(pw.UPSCALED_DIR, "p.jpg"), "wb").close()

        note = pw.source_note("p", self.state_with(400, 300, rotation=90), self.config, TARGET)

        self.assertTrue(note.startswith("1200x1600 AI-upscaled source"))

    def test_unknown_size_gives_no_note(self):

        self.assertEqual(pw.source_note("missing", copy.deepcopy(pw.DEFAULT_STATE), self.config, TARGET), "")

    def test_enlargement_helpers(self):

        self.assertEqual(pw.oriented_size({"width": 4, "height": 3}, 90), (3, 4))
        self.assertEqual(pw.enlargement({"width": 0}, 0, (10, 10)), 0)

        state = copy.deepcopy(pw.DEFAULT_STATE)
        state["screens"] = [dict(SCREEN), {"id": 2, "name": "Tall", "width": 100, "height": 400}]

        self.assertEqual(pw.largest_screen(self.config, state), (160, 400))


if __name__ == "__main__":
    unittest.main()
