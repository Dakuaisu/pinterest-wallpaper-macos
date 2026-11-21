import hashlib
import json

from PIL import Image, ImageDraw, ImageOps

from helpers import Sandbox, pw


class CropFocusTests(Sandbox):
    def settings(self, **overrides):
        return dict(pw.render_settings("pin", self.config, self.state(), False),
                    sharpen=False, **overrides)

    def test_state_keeps_only_normalized_pairs_for_valid_pin_ids(self) -> None:
        pw.save_state({"version": 2, "crop_focus": {
            "123": [0, 1], "e1_pin": [0.25, 0.75], "../bad": [0.5, 0.5],
            "boolean": [True, 0], "text": ["0.2", 0.5], "short": [0], "long": [0, 0, 0],
            "negative": [-0.1, 0], "above": [0, 1.01], "infinite": [float("inf"), 0],
            "nonfinite": [0, float("nan")], "huge": [10**400, 0], "null": None,
        }})

        self.assertEqual(self.state()["crop_focus"], {"123": [0, 1], "e1_pin": [0.25, 0.75]})

    def test_missing_or_malformed_field_defaults_to_empty(self) -> None:
        for state in ({"version": 1}, {"version": 2}, {"version": 2, "crop_focus": [[0, 1]]}):
            with self.subTest(state=state):
                pw.save_state(state)

                self.assertEqual(self.state()["crop_focus"], {})

    def test_focus_rotates_clockwise_in_normalized_image_coordinates(self) -> None:
        for angle, expected in ((0, (0.125, 0.25)), (90, (0.75, 0.125)),
                                (180, (0.875, 0.75)), (270, (0.25, 0.875))):
            with self.subTest(angle=angle):
                self.assertEqual(pw.focus_point_rotated((0.125, 0.25), angle), expected)

    def test_cover_centers_on_the_point_and_clamps_both_edges(self) -> None:
        for size, point, expected in (
            ((400, 100), (0, 0.6), (0, 0, 100, 100)),
            ((400, 100), (0.625, 0.2), (200, 0, 300, 100)),
            ((400, 100), (1, 0.2), (300, 0, 400, 100)),
            ((100, 400), (0.6, 0), (0, 0, 100, 100)),
            ((100, 400), (0.2, 1), (0, 300, 100, 400)),
        ):
            with self.subTest(size=size, point=point):
                img = Image.new("RGB", size)
                settings = self.settings(crop="center", crop_focus=point)

                self.assertEqual(pw.cover_crop_box(img, (100, 100), settings), expected)

    def test_rotated_source_box_matches_the_rendered_subject(self) -> None:
        img = Image.new("RGB", (400, 100), "blue")
        img.paste("red", (300, 0, 400, 100))
        settings = self.settings(crop="smart", crop_focus=(1, 0.5), rotate=90)
        rotated = img.transpose(Image.Transpose.ROTATE_270)

        self.assertEqual(pw.cover_crop_box(rotated, (100, 100), settings), (0, 300, 100, 400))
        self.assertEqual(pw.render_image(img, (100, 100), settings).getpixel((50, 50)), (255, 0, 0))

    def test_manual_preview_uses_the_reference_crop_when_output_rounds(self) -> None:
        img = Image.linear_gradient("L").resize((100, 400)).convert("RGB")
        settings = self.settings(crop_focus=(0.5, 1))

        preview = pw.render_image(img, (7, 4), settings, reference_size=(151, 100))

        expected_box = (0, 400 - 100 / 1.51, 100, 400)
        expected = img.resize((7, 4), Image.Resampling.LANCZOS, box=expected_box)
        self.assertEqual(preview.tobytes(), expected.tobytes())

    def test_fit_and_limited_cover_ignore_the_manual_point(self) -> None:
        img = Image.linear_gradient("L").resize((400, 100)).convert("RGB")
        size = (300, 300)
        for overrides in ({"fit": "blur"}, {"fit": "cover", "max_upscale": 2}):
            with self.subTest(overrides=overrides):
                legacy = self.settings(**overrides)
                focused = dict(legacy, crop_focus=(1, 0))

                self.assertEqual(pw.cover_crop_box(img, size, focused), (0, 0, 400, 100))
                self.assertEqual(pw.render_image(img, size, focused).tobytes(),
                                 pw.render_image(img, size, legacy).tobytes())

    def test_legacy_smart_and_center_use_pillow_fit_geometry(self) -> None:
        img = Image.new("RGB", (403, 101))
        draw = ImageDraw.Draw(img)
        for x in range(290, 400, 10):
            draw.rectangle((x, 10, x + 4, 90), fill="white")
        target, preview = (151, 100), (7, 4)
        smart = pw.smart_centering(img, target)
        self.assertGreater(smart[0], 0.5)

        for crop, centering in (("smart", smart), ("center", (0.5, 0.5))):
            with self.subTest(crop=crop):
                settings = self.settings(crop=crop)

                box = pw.cover_crop_box(img, target, settings)
                out = pw.render_image(img, preview, settings, reference_size=target)

                expected = ImageOps.fit(img, target, Image.Resampling.LANCZOS, centering=centering)
                self.assertEqual(img.resize(target, Image.Resampling.LANCZOS, box=box).tobytes(), expected.tobytes())
                expected = ImageOps.fit(img, preview, Image.Resampling.LANCZOS, centering=centering)
                self.assertEqual(out.tobytes(), expected.tobytes())

    def test_focus_changes_only_its_pins_render_path_and_clearing_restores_it(self) -> None:
        for key in ("pin", "other"):
            self.add_pin(key)
        files = pw.source_files()
        size = (160, 100)
        state = self.state()
        baseline = {key: pw.expected_render(key, size, self.config, state, files)[2] for key in files}

        with pw.locked_state() as saved:
            saved["crop_focus"] = {"pin": [0, 0.5]}
        state = self.state()
        first = pw.expected_render("pin", size, self.config, state, files)[2]
        state["crop_focus"]["pin"] = (1, 0.5)
        second = pw.expected_render("pin", size, self.config, state, files)[2]
        self.assertEqual(len({first, second, baseline["pin"]}), 3)

        self.assertEqual(pw.expected_render("other", size, self.config, state, files)[2],
                         baseline["other"])
        state["crop_focus"].clear()
        self.assertEqual(pw.expected_render("pin", size, self.config, state, files)[2], baseline["pin"])

    def test_no_focus_preserves_legacy_settings_tag(self) -> None:
        expected = {"v": 2, "fit": "cover", "crop": "smart", "backdrop": "blur", "color": None,
                    "sharpen": True, "menubar": False, "max_upscale": 0, "rotate": 0, "ai": ""}
        old_tag = hashlib.sha1(json.dumps(expected, sort_keys=True).encode()).hexdigest()[:8]

        state = self.state()
        state.pop("crop_focus", None)
        settings = pw.render_settings("pin", self.config, state, False)

        self.assertEqual(pw.settings_tag(settings), old_tag)
