import copy
import json
import os

from PIL import Image

from helpers import Sandbox, pw


class FillPinsTests(Sandbox):
    def test_missing_option_defaults_to_empty_without_warning(self):
        with open(pw.CONFIG_FILE, "w") as handle:
            json.dump({}, handle)

        config, warnings = pw.load_config()

        self.assertEqual(config["fill_pins"], [])
        self.assertEqual(warnings, [])

    def test_valid_ids_are_loaded(self):
        with open(pw.CONFIG_FILE, "w") as handle:
            json.dump({"fill_pins": ["123", "e1_ab-CD_9"]}, handle)

        config, warnings = pw.load_config()

        self.assertEqual(config["fill_pins"], ["123", "e1_ab-CD_9"])
        self.assertEqual(warnings, [])

    def test_invalid_list_is_replaced_by_empty_default(self):
        for value in (None, "123", [""], ["../pin"], ["a/b"], [1], ["a" * 121], ["ok", "bad.id"]):
            with self.subTest(value=value):
                with open(pw.CONFIG_FILE, "w") as handle:
                    json.dump({"fill_pins": value}, handle)

                config, warnings = pw.load_config()

                self.assertEqual(config["fill_pins"], [])
                self.assertEqual(len(warnings), 1)

    def test_listed_pin_renders_fill_without_changing_other_pins_or_original(self):
        self.add_pin("listed", size=(40, 20))
        self.add_pin("other", size=(40, 20))
        self.config.update(fit_mode="blur", max_upscale=1, backdrop="color", sharpen=False, fill_pins=["listed"])
        state, files = self.state(), pw.source_files()
        state["pins"]["other"]["color"] = "#000000"
        source_path = os.path.join(pw.IMAGE_DIR, files["listed"])
        with open(source_path, "rb") as handle:
            original = handle.read()

        listed = pw.ensure_render("listed", (80, 80), self.config, state, files)
        other = pw.ensure_render("other", (80, 80), self.config, state, files)

        with Image.open(listed) as image:
            self.assertGreater(image.getpixel((0, 0))[0], 200)
        with Image.open(other) as image:
            self.assertLess(image.getpixel((0, 0))[0], 30)
        with open(source_path, "rb") as handle:
            self.assertEqual(handle.read(), original)

    def test_render_cache_key_changes_only_for_listed_pin(self):
        for key in ("listed", "other"):
            self.add_pin(key, size=(40, 20))
        self.config.update(fit_mode="blur", max_upscale=1)
        state, files = self.state(), pw.source_files()
        before = {key: pw.expected_render(key, (80, 80), self.config, state, files)[2] for key in files}

        self.config["fill_pins"] = ["listed"]

        self.assertNotEqual(pw.expected_render("listed", (80, 80), self.config, state, files)[2], before["listed"])
        self.assertEqual(pw.expected_render("other", (80, 80), self.config, state, files)[2], before["other"])

    def test_source_note_matches_effective_fill_settings(self):
        state = copy.deepcopy(pw.DEFAULT_STATE)
        state["pins"]["listed"] = {"width": 40, "height": 20}
        state["pins"]["other"] = {"width": 40, "height": 20}
        self.config.update(fit_mode="blur", max_upscale=1, fill_pins=["listed"])

        listed = pw.source_note("listed", state, self.config, (80, 80))
        other = pw.source_note("other", state, self.config, (80, 80))

        self.assertEqual(listed, "40x20 source, scaled 4.0x to fill 80x80")
        self.assertEqual(other, "40x20 source, scaled 1.0x on a backdrop")
