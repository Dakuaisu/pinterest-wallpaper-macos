import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class WrapperSymlinkTests(unittest.TestCase):

    def setUp(self):

        self.temp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.temp)
        self.project = os.path.join(self.temp, "project with spaces")
        self.cwd = os.path.join(self.temp, "unrelated cwd")
        os.makedirs(os.path.join(self.project, ".venv", "bin"))
        os.makedirs(self.cwd)
        shutil.copy(os.path.join(ROOT, "pw"), os.path.join(self.project, "pw"))
        self.result = os.path.join(self.temp, "invocation.txt")
        fake_python = os.path.join(self.project, ".venv", "bin", "python3")

        with open(fake_python, "w") as handle:
            handle.write("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$PW_TEST_RESULT\"\n")

        os.chmod(fake_python, 0o755)

    def invoke(self, path):

        env = dict(os.environ, PW_TEST_RESULT=self.result)
        subprocess.run([path, "status", "argument with spaces", "--flag"], cwd=self.cwd, env=env, check=True)

        with open(self.result) as handle:
            return handle.read().splitlines()

    def test_direct_invocation_preserves_venv_and_arguments(self):

        self.assertEqual(self.invoke(os.path.join(self.project, "pw")), [
            "pinterest_wallpaper.py", "status", "argument with spaces", "--flag",
        ])

    def test_absolute_relative_and_chained_symlinks_resolve_project(self):

        absolute_dir = os.path.join(self.temp, "absolute links")
        relative_dir = os.path.join(self.temp, "relative links")
        os.makedirs(absolute_dir)
        os.makedirs(relative_dir)
        absolute_link = os.path.join(absolute_dir, "pw-absolute")
        relative_link = os.path.join(relative_dir, "pw-relative")
        chain_link = os.path.join(relative_dir, "pw-chain")
        os.symlink(os.path.join(self.project, "pw"), absolute_link)
        os.symlink(os.path.relpath(os.path.join(self.project, "pw"), relative_dir), relative_link)
        os.symlink("pw-relative", chain_link)

        for link in (absolute_link, relative_link, chain_link):
            for invocation in (link, os.path.relpath(link, self.cwd)):
                with self.subTest(invocation=invocation):
                    self.assertEqual(self.invoke(invocation), [
                        "pinterest_wallpaper.py", "status", "argument with spaces", "--flag",
                    ])


if __name__ == "__main__":
    unittest.main()
