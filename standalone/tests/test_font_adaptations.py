"""Verify recorded font source overlays against the pristine source baseline.

These checks establish patch provenance and staging integrity. Font rendering
and feature behavior still require the native smoke harness after a real build.
"""
import difflib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    'font_overlay_engine', ROOT / 'standalone/tools/engine.py')
engine = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(engine)

FONT_PATHS = (
    'third_party/blink/renderer/core/css/css_font_selector.cc',
    'third_party/blink/renderer/platform/fonts/font_variant_alternates.h',
    'third_party/blink/renderer/platform/fonts/font_variant_alternates.cc',
    'third_party/blink/renderer/platform/fonts/font_cache.cc',
)


class FontAdaptationTests(unittest.TestCase):
    def setUp(self):
        self.recipe = json.loads(
            (ROOT / 'standalone/config/engine-adaptations.json').read_text())

    def test_font_patch_matches_exact_original_and_adapted_bytes(self):
        patch = (ROOT / self.recipe['patch']['path']).read_bytes()
        self.assertEqual(engine.digest(patch), self.recipe['patch']['sha256'])
        for name in FONT_PATHS:
            with self.subTest(path=name):
                original = (ROOT / name.removeprefix('third_party/blink/')).read_bytes()
                entry = self.recipe['files'][name]
                adapted = engine.adapt_bytes(original, entry)
                self.assertNotEqual(original, adapted)
                expected = ''.join(difflib.unified_diff(
                    original.decode().splitlines(keepends=True),
                    adapted.decode().splitlines(keepends=True),
                    fromfile='a/' + name, tofile='b/' + name, n=0)).encode()
                start = patch.index(('--- a/' + name + '\n').encode())
                end = patch.find(b'--- a/', start + 1)
                self.assertEqual(patch[start:end if end >= 0 else None], expected)
                with self.assertRaisesRegex(ValueError, 'Upstream'):
                    engine.adapt_bytes(original + b'// unreviewed drift\n', entry)

    def test_font_overlay_staging_keeps_original_sources_and_detects_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            view = Path(directory)
            owned = {}
            originals = {}
            for name in FONT_PATHS:
                source = ROOT / name.removeprefix('third_party/blink/')
                originals[source] = source.read_bytes()
                adapted = engine.adapt_bytes(originals[source], self.recipe['files'][name])
                engine.write_overlay(view, name, adapted, set())
                owned[name] = engine.digest(adapted)
            manifest = {'owned_files': owned, 'recipe': {'files': {}}}
            engine.verify_view(view, manifest)
            (view / FONT_PATHS[0]).write_bytes(b'// unrecorded change\n')
            with self.assertRaisesRegex(ValueError, 'adaptation changed'):
                engine.verify_view(view, manifest)
            for source, original in originals.items():
                self.assertEqual(source.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
