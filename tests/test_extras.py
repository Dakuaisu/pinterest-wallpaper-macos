import os
import shutil
import subprocess
import tempfile
import unittest

from helpers import ROOT

FAKE_PW = '#!/bin/bash\necho "pw:$*"\n'


def run(path, *args, cwd="/"):

    result = subprocess.run([path, *args], capture_output=True, text=True, cwd=cwd, timeout=30, check=False)

    return result.returncode, result.stdout.strip()


class ExtrasTests(unittest.TestCase):

    def setUp(self):

        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.repo = os.path.join(self.tmp, "repo")
        shutil.copytree(os.path.join(ROOT, "extras"), os.path.join(self.repo, "extras"))
        self.write(os.path.join(self.repo, "pw"), FAKE_PW)

    def write(self, path, text):

        os.makedirs(os.path.dirname(path), exist_ok=True)

        with open(path, "w") as handle:
            handle.write(text)

        os.chmod(path, 0o755)

    def test_every_extra_script_is_executable(self):

        for folder in ("swiftbar", "raycast"):
            for name in os.listdir(os.path.join(ROOT, "extras", folder)):
                with self.subTest(name=name):
                    self.assertTrue(os.access(os.path.join(ROOT, "extras", folder, name), os.X_OK))

    def test_swiftbar_plugin_finds_pw_through_absolute_and_relative_symlinks(self):

        plugin = os.path.join(self.repo, "extras", "swiftbar", "pinterest-wallpaper.5m.sh")
        plugins = os.path.join(self.tmp, "plugins")
        os.makedirs(plugins)

        os.symlink(plugin, os.path.join(plugins, "absolute.sh"))
        os.symlink(os.path.relpath(plugin, plugins), os.path.join(plugins, "relative.sh"))

        for name in ("absolute.sh", "relative.sh"):
            with self.subTest(name=name):
                self.assertEqual(run(os.path.join(plugins, name)), (0, "pw:menubar"))

    def test_raycast_commands_pass_the_right_arguments(self):

        raycast = os.path.join(self.repo, "extras", "raycast")
        cases = {
            ("next.sh",): "pw:next",
            ("previous.sh",): "pw:prev",
            ("ban.sh",): "pw:ban",
            ("favorite.sh",): "pw:fav",
            ("open-pin.sh",): "pw:open",
            ("resume.sh",): "pw:resume",
            ("status.sh",): "pw:status",
            ("rotate.sh",): "pw:rotate cw",
            ("rotate.sh", "ccw"): "pw:rotate ccw",
            ("pause.sh",): "pw:pause",
            ("pause.sh", ""): "pw:pause",
            ("pause.sh", "2h"): "pw:pause 2h",
        }

        for (script, *args), expected in cases.items():
            with self.subTest(script=script, args=args):
                self.assertEqual(run(os.path.join(raycast, script), *args), (0, expected))

    def test_raycast_metadata(self):

        raycast = os.path.join(ROOT, "extras", "raycast")

        for name in os.listdir(raycast):
            with open(os.path.join(raycast, name)) as handle:
                text = handle.read()

            for field in ("schemaVersion 1", "title ", "mode ", "packageName Pinterest Wallpaper"):
                with self.subTest(name=name, field=field):
                    self.assertIn(f"# @raycast.{field}", text)

    def test_script_entry_point_sets_the_exit_code(self):

        script = os.path.join(ROOT, "pinterest_wallpaper.py")

        self.assertEqual(run("/usr/bin/python3", script, "--help")[0], 0)
        self.assertEqual(run("/usr/bin/python3", script, "no-such-command")[0], 2)

    def test_pw_wrapper_prefers_the_venv_and_runs_from_its_own_folder(self):

        shutil.copy(os.path.join(ROOT, "pw"), os.path.join(self.repo, "pw"))
        self.write(
            os.path.join(self.repo, "pinterest_wallpaper.py"),
            "import os, sys\nprint('system', os.getcwd(), *sys.argv[1:])\n",
        )

        code, out = run(os.path.join(self.repo, "pw"), "next", "2")
        self.assertEqual((code, out), (0, f"system {self.repo} next 2"))

        self.write(os.path.join(self.repo, ".venv", "bin", "python3"), '#!/bin/bash\necho "venv $*"\n')

        code, out = run(os.path.join(self.repo, "pw"), "status")
        self.assertEqual((code, out), (0, "venv pinterest_wallpaper.py status"))


if __name__ == "__main__":
    unittest.main()
