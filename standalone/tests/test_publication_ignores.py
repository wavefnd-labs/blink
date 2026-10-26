"""Check source publication excludes local artifacts but retains build inputs."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class PublicationIgnoreTests(unittest.TestCase):
    def test_local_outputs_are_excluded_and_pinned_sources_remain_publishable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / 'standalone').mkdir()
            for relative in ['.gitignore', 'standalone/.gitignore']:
                shutil.copyfile(ROOT / relative, root / relative)
            ignored = ['docs/standalone.md', 'standalone/work/engine/libblink_embed.so',
                       'standalone/out/sdk/libblink_embed.so',
                       'standalone/reports/profiles/dev-cache/linux-x64-engine-entry.json',
                       'standalone/reports/linux-x64-engine-build.json',
                       'standalone/reports/linux-x64-runtime-check.json',
                       'standalone/reports/linux-x64-runtime-smoke.json',
                       'standalone/reports/wave-browser-integration.json']
            included = ['LICENSE', 'README.md', 'public/web/blink.h',
                        'standalone/engine/engine.cc', 'standalone/config/upstream.lock.json',
                        'standalone/reports/linux-x64-blink-graph.json.gz',
                        'standalone/upstream/history/blink-commit-map.tsv.gz']
            for path in ignored + included:
                with self.subTest(path=path):
                    result = subprocess.run(['git', '-c', 'core.excludesFile=/dev/null',
                        '-C', str(root), 'check-ignore', '--no-index', '-q', '--', path])
                    self.assertEqual(result.returncode, 0 if path in ignored else 1)


if __name__ == '__main__':
    unittest.main()
