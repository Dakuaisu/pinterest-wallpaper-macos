import json
import os
from unittest import mock

import pinterest_wallpaper as pw
from helpers import BOARD, Sandbox


class StateBoundaryTests(Sandbox):
    def test_repairs_nested_state_without_losing_valid_records(self):
        raw = {"version": 2, "banned": ["good", {}, "../bad"], "favorites": ["good", None],
               "history": [["good", []], "bad", [None]], "deck": ["good", {}],
               "pins": {"good": {"board": BOARD, "width": 300, "height": 400, "luma": 40, "avg": "#123456"}, "bad": []},
               "screens": [{"id": 1, "name": "Screen", "width": 100, "height": 80}, {"width": 0}],
               "rotations": {"good": 90, "bad": "wrong"}, "board_fails": {BOARD: {"count": "broken"}},
               "last_refresh_ok": float("inf"), "listings": {BOARD: {"pins": [None], "stamp": {}}}}
        with open(pw.STATE_FILE, "w") as handle:
            json.dump(raw, handle)
        state = pw.load_state()
        self.assertEqual(state["banned"], ["good"])
        self.assertEqual(state["history"], [["good"]])
        self.assertEqual(state["rotations"], {"good": 90})
        self.assertEqual(state["last_refresh_ok"], 0)
        self.assertIn("good", state["pins"])
        self.assertEqual(len(state["screens"]), 1)
        self.assertEqual(state["listings"], {})
        self.assertEqual(state["pins"]["good"]["width"], 300)

    def test_large_finite_integer_config_is_rejected_without_overflow(self):
        self.assertFalse(pw.CONFIG_RULES["refresh_hours"][0](10 ** 1000))

    def test_load_and_recovery_hold_state_lock(self):
        with open(pw.STATE_FILE, "w") as handle:
            handle.write("{")
        original = pw.os.replace
        def check(src, dst):
            with pw.file_lock(pw.STATE_LOCK, wait=False) as acquired:
                self.assertFalse(acquired)
            return original(src, dst)
        with mock.patch.object(pw.os, "replace", side_effect=check):
            pw.load_state()


class CacheIdentityTests(Sandbox):
    def test_render_identity_tracks_executable_provenance(self):
        self.add_pin("1")
        executable = self.make_upscaler()
        alternate = os.path.join(self.root, "alternate-upscaler")
        with open(alternate, "w") as handle:
            handle.write("#!/bin/sh\nexit 1\n")
        os.chmod(alternate, 0o755)
        self.config["upscaler"] = executable
        state = self.state()
        files = pw.source_files()
        first_cache = pw.upscale_cache_paths("1", executable, self.config["upscaler_model"],
                                             os.path.join(pw.IMAGE_DIR, files["1"]))[0]
        with open(first_cache, "wb") as handle:
            handle.write(b"first derivative")
        first_render = pw.expected_render("1", (100, 100), self.config, state, files)[2]
        self.config["upscaler"] = alternate
        second_cache = pw.upscale_cache_paths("1", alternate, self.config["upscaler_model"],
                                              os.path.join(pw.IMAGE_DIR, files["1"]))[0]
        with open(second_cache, "wb") as handle:
            handle.write(b"second derivative")
        second_render = pw.expected_render("1", (100, 100), self.config, state, files)[2]
        self.assertNotEqual(first_render, second_render)

    def test_unavailable_upscaler_uses_original_source(self):
        self.add_pin("1")
        self.config["upscaler"] = os.path.join(self.root, "missing-upscaler")
        source, ai = pw.source_for("1", self.config, pw.source_files())
        self.assertEqual(source, os.path.join(pw.IMAGE_DIR, "1.jpg"))
        self.assertFalse(ai)

    def test_cache_invalidates_when_model_or_source_changes(self):
        self.add_pin("1", size=(400, 300))
        self.config["upscaler"] = self.make_upscaler()
        state = self.state()
        state["screens"] = [dict(self.screens[0], width=1600, height=1000)]
        files = pw.source_files()
        with mock.patch.object(pw, "run_upscaler", side_effect=RuntimeError("failed")) as run:
            pw.upscale_sources(self.config, state, files, 1)
            pw.upscale_sources(self.config, state, files, 1)
            self.assertEqual(run.call_count, 1)
            self.config["upscaler_model"] = "other"
            pw.upscale_sources(self.config, state, files, 1)
            self.assertEqual(run.call_count, 2)
            with open(os.path.join(pw.IMAGE_DIR, files["1"]), "ab") as handle:
                handle.write(b"changed")
            pw.upscale_sources(self.config, state, files, 1)
            self.assertEqual(run.call_count, 3)
