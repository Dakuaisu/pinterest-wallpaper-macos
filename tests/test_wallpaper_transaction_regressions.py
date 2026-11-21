import json
import os
import shutil
import subprocess
import unittest
from unittest import mock

import pinterest_wallpaper as pw
from helpers import REAL, Sandbox


class WallpaperTransactionTests(Sandbox):
    def test_prev_reconciles_actual_display_but_preserves_history_on_failure(self):
        self.add_pin("a")
        self.add_pin("b")
        with pw.locked_state() as state:
            state["history"] = [["a"], ["b"]]
            state["current"] = ["b"]
            state["current_screens"] = [1]
            state["on_screen"] = ["b.old.jpg"]
        def fail_after_set(assignment):
            raise pw.WallpaperTransactionError(["rollback failed"], [{"id": "1", "path": assignment[0][1]}])
        with mock.patch.object(pw, "set_wallpapers", side_effect=fail_after_set), pw.locked_state() as state:
            self.assertFalse(pw.go_back(self.config, state))
        state = self.state()
        self.assertEqual(state["current"], ["a"])
        self.assertEqual(state["history"], [["a"], ["b"]])

    def test_reapply_unknown_display_identity_fails_without_shifting_slots(self):
        self.add_pin("b")
        with pw.locked_state() as state:
            state["screens"] = [dict(self.screens[0]), dict(self.screens[0], id=2)]
            state["current"] = [None, "b"]
            state["current_screens"] = [1, 2]
            state["on_screen"] = ["unknown.jpg", "b.jpg"]
        state = self.state()
        with mock.patch.object(pw, "set_wallpapers") as setter, self.assertRaises(pw.WallpaperTransactionError):
            pw.reapply_current(self.config, state)
        setter.assert_not_called()
        self.assertEqual(state["current"], [None, "b"])

    def test_reconciliation_normalizes_unequal_display_arrays(self):
        state = self.state()
        state["current"] = ["old"]
        state["current_screens"] = []
        state["on_screen"] = []
        assignment = [({"id": 1}, "/one.jpg"), ({"id": 2}, "/two.jpg")]
        error = pw.WallpaperTransactionError(["partial"], [{"id": "2", "path": "/two.jpg"}])
        pw.reconcile_wallpaper_failure(error, assignment, ["new1", "new2"], state)
        self.assertEqual(state["current"], ["old", "new2"])
        self.assertEqual(state["current_screens"], [None, "2"])
        self.assertEqual(state["on_screen"], ["", "two.jpg"])

    def test_wildcard_assignment_reconciles_numeric_jxa_screen_ids(self):
        state = self.state()
        state["current"] = ["old"]
        state["current_screens"] = [None]
        state["on_screen"] = ["old.jpg"]
        assignment = [({"id": None}, "/target.jpg")]
        error = pw.WallpaperTransactionError(["partial"], [
            {"id": "11", "path": "/target.jpg"}, {"id": "22", "path": "/external.jpg"}
        ])
        pw.reconcile_wallpaper_failure(error, assignment, ["new"], state)
        self.assertEqual(state["current"], ["new", None])
        self.assertEqual(state["current_screens"], ["11", "22"])
        self.assertEqual(state["on_screen"], ["target.jpg", "external.jpg"])

    @unittest.skipUnless(shutil.which("node"), "Node.js is optional; Python reconciliation tests still cover state handling")
    def test_jxa_preflights_and_rolls_back_partial_set(self):
        harness = (
            "global.ObjC={import:function(){},unwrap:function(v){return v}};\n"
            "global.Ref=function(){return [null]};\n"
            "function $(v){return v};\n"
            "var screens=[1,2].map(function(id){return {deviceDescription:{objectForKey:function(){return id}}}});\n"
            "$.NSScreen={screens:{count:2,objectAtIndex:function(i){return screens[i]}}};\n"
            "$.NSURL={fileURLWithPath:function(path){return {path:path}}};\n"
            "$.NSFileManager={defaultManager:{fileExistsAtPath:function(){return true}}};\n"
            "var actual={1:{path:'/old1'},2:{path:'/old2'}};\n"
            "$.NSWorkspace={sharedWorkspace:{\n"
            " desktopImageURLForScreen:function(s){return actual[ObjC.unwrap(s.deviceDescription.objectForKey())]},\n"
            " desktopImageOptionsForScreen:function(){return {}},\n"
            " setDesktopImageURLForScreenOptionsError:function(url,s,o,e){var id=ObjC.unwrap(s.deviceDescription.objectForKey());\n"
            "   if(url.path==='/bad'){e[0]={localizedDescription:'setter failed'};return false} actual[id]=url;return true}\n"
            "}};\n"
        )
        command = ("node", "-e", harness + pw.JXA_SET_WALLPAPERS + "\nconsole.log(run(['1','/new1','2','/bad']));")
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        outcome = json.loads(result.stdout)
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["screens"], [{"id": "1", "path": "/old1"}, {"id": "2", "path": "/old2"}])

        preflight = harness.replace("fileExistsAtPath:function(){return true}",
                                     "fileExistsAtPath:function(path){return path !== '/missing'}")
        result = subprocess.run(("node", "-e", preflight + pw.JXA_SET_WALLPAPERS +
                                "\nconsole.log(run(['1','/missing','2','/new2']));console.log(JSON.stringify(actual));"),
                                capture_output=True, text=True, check=True)
        lines = result.stdout.splitlines()
        self.assertFalse(json.loads(lines[0])["ok"])
        self.assertEqual(json.loads(lines[1]), {"1": {"path": "/old1"}, "2": {"path": "/old2"}})

        rollback_failure = harness.replace("if(url.path==='/bad')", "if(url.path==='/bad'||url.path==='/old1')")
        result = subprocess.run(("node", "-e", rollback_failure + pw.JXA_SET_WALLPAPERS +
                                "\nconsole.log(run(['1','/new1','2','/bad']));"), capture_output=True, text=True, check=True)
        residual = json.loads(result.stdout)
        self.assertFalse(residual["ok"])
        self.assertTrue(any(error.startswith("rollback 1:") for error in residual["errors"]))
        self.assertEqual(residual["screens"][0]["path"], "/new1")

    def test_failed_display_transaction_reconciles_unknown_slots(self):
        state = self.state()
        state["current"] = ["old1", "old2"]
        state["current_screens"] = [1, 2]
        state["on_screen"] = ["old1.jpg", "old2.jpg"]
        assignment = [({"id": 1}, "/new1.jpg"), ({"id": 2}, "/new2.jpg")]
        error = pw.WallpaperTransactionError(["rollback failed"], [{"id": "1", "path": "/new1.jpg"}, {"id": "2", "path": "/external.jpg"}])
        pw.reconcile_wallpaper_failure(error, assignment, ["new1", "new2"], state)
        self.assertEqual(state["current"], ["new1", None])
        self.assertEqual(state["on_screen"], ["new1.jpg", "external.jpg"])

    def test_reapply_follows_display_ids_when_macos_reorders_the_displays(self):
        left = {"id": 11, "name": "Left", "width": 160, "height": 100}
        right = {"id": 22, "name": "Right", "width": 120, "height": 90}
        self.config["different_per_display"] = True
        self.add_pin("one")
        self.add_pin("two")
        shown = []
        with mock.patch.object(pw, "set_wallpapers", side_effect=lambda assignment: shown.append(
                {screen["id"]: os.path.basename(path).split(".")[0] for screen, path in assignment})):
            self.screens = [left, right]
            with pw.locked_state() as state:
                state["current"] = ["one", "two"]
                state["current_screens"] = [11, 22]
                pw.reapply_current(self.config, state)
            self.screens = [right, left]
            with pw.locked_state() as state:
                pw.reapply_current(self.config, state)
                self.assertEqual((state["current"], state["current_screens"]), (["two", "one"], [22, 11]))
                self.assertEqual(pw.pin_on_display(state, 1), "two")
            self.screens = [right, {"id": 33, "name": "New", "width": 160, "height": 100}]
            with pw.locked_state() as state:
                pw.reapply_current(self.config, state)
        self.assertEqual(shown, [{11: "one", 22: "two"}, {22: "two", 11: "one"}, {22: "two", 33: "one"}])

    def test_python_setter_decodes_transaction_result(self):
        assignment = [({"id": 1}, "/tmp/not-used.jpg")]
        with mock.patch("os.path.isfile", return_value=True), mock.patch.object(
            pw, "osascript", return_value=json.dumps({"ok": False, "errors": ["rollback failed"], "screens": [{"id": "1", "path": "/other.jpg"}]})
        ), self.assertRaises(pw.WallpaperTransactionError):
            REAL["set_wallpapers"](assignment)
