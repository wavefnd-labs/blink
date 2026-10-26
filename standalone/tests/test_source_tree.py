from pathlib import Path
import importlib.util
import subprocess
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("blink_tree_tools", Path(__file__).resolve().parents[1] / "tools/blink.py")
blink = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(blink)


class SourceTreeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.git("init", "--quiet")
        for key, value in {
            "user.name": "Source fixture", "user.email": "source-fixture@example.invalid",
            "commit.gpgsign": "false", "core.hooksPath": "/dev/null", "core.autocrlf": "false",
        }.items():
            self.git("config", key, value)
        (self.root / "public").mkdir()
        self.source = self.root / "public/header.h"
        self.source.write_text("original source\n")
        self.git("add", "public")
        self.git("commit", "--quiet", "-m", "Original upstream")
        self.lock = {"files": {}, "git_source_tree": {"tree": self.git("rev-parse", "HEAD^{tree}").strip()}}

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], text=True)

    def test_project_helpers_do_not_pollute_original_tree_verification(self):
        (self.root / "standalone/tools").mkdir(parents=True)
        (self.root / "standalone/tools/helper.py").write_text("# project support\n")
        self.assertEqual(blink.verify_files(self.lock, self.root), [])

    def test_staged_adaptation_is_reported_against_immutable_baseline(self):
        self.source.write_text("local integration change\n")
        self.git("add", "public/header.h")
        errors = blink.verify_files(self.lock, self.root)
        self.assertTrue(any("differs (M): public/header.h" in error for error in errors))

    def test_even_ignored_untracked_source_additions_are_reported(self):
        (self.root / "public/.gitignore").write_text("local.cc\n")
        (self.root / "public/local.cc").write_text("// local source\n")
        errors = blink.verify_files(self.lock, self.root)
        self.assertIn("unexpected file in upstream tree: public/local.cc", errors)
