from unittest import mock

from PIL import Image, ImageChops, ImageDraw, ImageStat

import pinterest_wallpaper as pw
from helpers import Sandbox
from wallpaper_tui_backend import Backend, Draft


class PreviewBackendTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.add_pin("123", size=(80, 40))
        self.config.update(resolution="60x40", fit_mode="blur", fill_pins=["123"])

    def test_sized_preview_respects_desktop_upscale_limit(self):
        self.config.update(resolution="600x400", fit_mode="cover", fill_pins=[],
                           max_upscale=1, backdrop="color", sharpen=False)
        with pw.locked_state() as state:
            state["pins"]["123"]["color"] = "#00ff00"
        backend = Backend(pw, self.config)
        pin = backend.pins()[0]
        desktop = backend.preview(pin, Draft(), False)
        preview = backend.preview(pin, Draft(), False, (60, 40))
        expected = desktop.resize((60, 40), Image.Resampling.LANCZOS)
        self.assertEqual(preview.size, (60, 40))
        self.assertEqual(preview.getpixel((0, 0)), expected.getpixel((0, 0)))
        self.assertGreater(preview.getpixel((30, 20))[0], 200)
        self.assertEqual(self.shown, [])

    def test_sized_original_preserves_aspect_ratio(self):
        backend = Backend(pw, self.config)
        pin = backend.pins()[0]
        preview = backend.preview(pin, Draft(angle=90), True, (20, 20))
        self.assertEqual(preview.size, (20, 10))

    def test_preview_task_uses_configuration_and_state_captured_at_submission(self):
        backend = Backend(pw, self.config)
        pin = backend.pins()[0]
        task = backend.preview_task(pin, Draft(), False, (30, 20))
        backend.config["resolution"] = "120x80"
        backend.config["fill_pins"].clear()
        backend._snapshot["rotations"][pin.key] = 180
        with mock.patch.object(pw, "render_image", wraps=pw.render_image) as render:
            task()
        settings = render.call_args.args[2]
        self.assertEqual(render.call_args.kwargs["reference_size"], (60, 40))
        self.assertEqual((settings["fit"], settings["rotate"]), ("cover", 0))

    def test_scaled_render_preserves_crop_rotation_blur_and_gradient(self):
        image = Image.new("RGB", (320, 200), "#346890")
        draw = ImageDraw.Draw(image)
        draw.rectangle((20, 20, 90, 180), fill="red")
        draw.rectangle((220, 10, 300, 190), fill="white")
        settings = {"rotate": 0, "fit": "cover", "crop": "smart", "max_upscale": 0,
                    "sharpen": True, "menubar": True, "backdrop": "blur"}
        for angle in (0, 90):
            for fit in ("cover", "blur"):
                for limit in (0, 0.5):
                    with self.subTest(angle=angle, fit=fit, limit=limit):
                        current = dict(settings, rotate=angle, fit=fit, max_upscale=limit)
                        desktop = pw.render_image(image, (600, 400), current)
                        expected = desktop.resize((150, 100), Image.Resampling.LANCZOS)
                        preview = pw.render_image(image, (150, 100), current, reference_size=(600, 400))
                        difference = ImageStat.Stat(ImageChops.difference(expected, preview)).mean
                        self.assertLess(sum(difference) / 3, 3)
