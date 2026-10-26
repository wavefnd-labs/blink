import gzip
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

SUPPORT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("blink_history", SUPPORT / "tools/history.py")
history = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(history)


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def commit(root, message):
    git(root, "add", "--all")
    git(root, "commit", "--quiet", "-m", message)
    return git(root, "rev-parse", "HEAD")


def metadata(root, revision):
    raw = subprocess.check_output(["git", "-C", str(root), "cat-file", "commit", revision])
    header, _, message = raw.partition(b"\n\n")
    fields = [line for line in header.splitlines() if line.startswith((b"author ", b"committer ", b"encoding "))]
    return fields, message


class HistoryTests(unittest.TestCase):
    def test_refuses_existing_output_before_touching_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "new directory"):
                history.extract("unused", "a" * 40, directory)

    def test_path_migration_preserves_tree_metadata_and_original_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, output = root / "source", root / "cache.git", root / "extracted.git"
            source.mkdir()
            subprocess.run(["git", "init", "--quiet", str(source)], check=True)
            for key, value in {
                "user.name": "History fixture", "user.email": "history-fixture@example.invalid",
                "commit.gpgsign": "false", "core.hooksPath": "/dev/null", "core.autocrlf": "false",
            }.items():
                git(source, "config", key, value)
            (source / "unrelated.txt").write_text("outside Blink")
            commit(source, "Unrelated Chromium change")
            old_path = source / "third_party/WebKit/Source/header.h"
            old_path.parent.mkdir(parents=True)
            old_path.write_bytes(b"// exact upstream bytes\n")
            first = commit(source, "Original Blink change\n\nKeep this message verbatim.")
            modern = source / "third_party/blink"
            modern.mkdir(parents=True)
            (modern / "OWNERS").write_text("new owners")
            (source / "third_party/WebKit/OWNERS").write_text("old owners")
            transition = commit(source, "Both Blink paths coexist during migration")
            new_path = source / "third_party/blink/renderer/header.h"
            new_path.parent.mkdir(parents=True)
            old_path.rename(new_path)
            (source / "third_party/WebKit/OWNERS").unlink()
            second = commit(source, "Move WebKit to Blink")
            (source / "unrelated.txt").write_text("another unrelated change")
            original_tip = commit(source, "Another unrelated Chromium change")
            subprocess.run(["git", "clone", "--bare", "--quiet", str(source), str(cache)], check=True)
            report = history.extract(cache, original_tip, output)
            self.assertEqual(report["extracted_commit_count"], 3)
            self.assertEqual(report["transition_commits"], 1)
            self.assertEqual(report["original_blink_tree"], report["extracted_tree"])
            self.assertEqual(git(cache, "rev-parse", "HEAD"), original_tip)
            self.assertEqual(git(output, "ls-tree", "--name-only", "HEAD"), "OWNERS\nrenderer")
            with gzip.open(output / "blink-commit-map.tsv.gz", "rt") as stream:
                mappings = dict(line.split()[:2] for line in stream if not line.startswith("old"))
            for original in (first, transition, second):
                self.assertEqual(metadata(cache, original), metadata(output, mappings[original]))
            self.assertEqual(git(output, "show", mappings[transition] + ":OWNERS"), "new owners")
            self.assertEqual(git(output, "show", mappings[transition] + ":.upstream-WebKit/OWNERS"), "old owners")


if __name__ == "__main__":
    unittest.main()
