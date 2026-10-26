"""Keep extracted Blink Python package discovery inside the guarded view."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1] / 'tools'


def module(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


layout, journal, engine = (module(name) for name in
                           ('blink_python_layout', 'generator_layout', 'engine'))


class BlinkPythonLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base = self.root / 'base'
        self.extracted = self.root / 'extracted'
        self.view = self.root / 'view'
        self.name = 'third_party/blink/renderer/bindings/scripts/entry.py'
        self.source = self.extracted / 'renderer/bindings/scripts/entry.py'
        self.source.parent.mkdir(parents=True)
        self.data = b'import web_idl\nprint(web_idl.result)\n'
        self.source.write_bytes(self.data)
        package = self.source.parent / 'web_idl'
        package.mkdir()
        (package / '__init__.py').write_text(
            'import os, sys\n'
            'root = os.path.abspath(os.path.join(os.path.dirname(__file__), '
            '*([".."] * 6)))\n'
            'sys.path.insert(1, os.path.join(root, '
            '"third_party/blink/renderer/build/scripts"))\n'
            'from blinkbuild import result\n')
        blinkbuild = self.extracted / 'renderer/build/scripts/blinkbuild'
        blinkbuild.mkdir(parents=True)
        (blinkbuild / '__init__.py').write_text('result = "pinned host import"\n')
        (self.base / 'third_party').mkdir(parents=True)
        (self.base / 'third_party/blink').symlink_to(self.extracted)
        self.view.mkdir()
        (self.view / 'third_party').symlink_to(self.base / 'third_party')
        self.upstream = {'revision': 'pin', 'protected_roots': ['renderer']}
        self.recipe = {'schema': 1, 'chromium_revision': 'pin',
                       'source_root_names': ['renderer'],
                       'repository': 'https://chromium.googlesource.com/chromium/src',
                       'files': {self.name: {
                           'mode': 'copy',
                           'source_path': 'renderer/bindings/scripts/entry.py',
                           'original_mode': '100644',
                           'original_sha256': hashlib.sha256(self.data).hexdigest(),
                           'git_blob': hashlib.sha1(b'blob ' + str(len(self.data)).encode()
                                                   + b'\0' + self.data).hexdigest()}}}
        self.dawn = {'chromium_revision': 'pin', 'files': {}}

    def inputs(self):
        return layout.configuration_inputs(self.base, 'pin', self.recipe, self.upstream)

    def test_copy_preserves_package_depth_without_modifying_pristine_source(self):
        before = self.source.stat()
        negative = subprocess.run([sys.executable, '-B', str(self.view / self.name)],
                                  capture_output=True, text=True)
        self.assertNotEqual(negative.returncode, 0)
        self.assertIn("No module named 'blinkbuild'", negative.stderr)
        manifest = {'owned_files': {}, 'split_sources': {}}
        recipe = layout.combined_recipe(self.dawn, 'pin', self.recipe, self.upstream)
        journal.stage(self.view, self.base, manifest, self.inputs(),
                      engine.write_overlay, {'renderer'}, recipe)
        positive = subprocess.run([sys.executable, '-B', str(self.view / self.name)],
                                  capture_output=True, text=True)
        self.assertEqual(positive.returncode, 0, positive.stderr)
        self.assertEqual(positive.stdout, 'pinned host import\n')
        # Importing and hashing the source can update read-atime under relatime.
        # Preserve its identity, content, mode and all write-related metadata.
        after = self.source.stat()
        for field in ('st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_size',
                      'st_mtime_ns', 'st_ctime_ns'):
            self.assertEqual(getattr(after, field), getattr(before, field), field)
        self.assertEqual(self.source.read_bytes(), self.data)
        self.assertEqual((self.view / self.name).read_bytes(), self.data)
        self.assertTrue((self.view / self.name).parent.joinpath('web_idl').is_symlink())

    def test_revision_path_mapping_blob_bytes_and_mode_are_reviewed(self):
        self.assertEqual(self.inputs(), {self.name: self.data})
        with self.assertRaisesRegex(ValueError, 'Chromium revision differ'):
            layout.configuration_inputs(self.base, 'other', self.recipe, self.upstream)
        self.recipe['files'][self.name]['source_path'] = 'renderer/unreviewed.py'
        with self.assertRaisesRegex(ValueError, 'Invalid Blink Python entry'):
            self.inputs()
        self.recipe['files'][self.name]['source_path'] = 'renderer/bindings/scripts/entry.py'
        self.recipe['files'][self.name]['git_blob'] = 'other-blob'
        with self.assertRaisesRegex(ValueError, 'Pristine Blink Python entry changed'):
            self.inputs()
        self.recipe['files'][self.name]['git_blob'] = hashlib.sha1(
            b'blob ' + str(len(self.data)).encode() + b'\0' + self.data).hexdigest()
        self.source.chmod(0o755)
        with self.assertRaisesRegex(ValueError, 'Pristine Blink Python entry changed'):
            self.inputs()

    def test_combined_recipe_preserves_dawn_and_has_disjoint_provenance(self):
        self.dawn['files']['third_party/dawn/generator/entry.py'] = {'original_mode': '100644'}
        result = layout.combined_recipe(self.dawn, 'pin', self.recipe, self.upstream)
        self.assertEqual(len(result['files']), 2)
        self.assertEqual(len(self.dawn['files']), 1)
        self.assertEqual(result['blink_python_layout'], self.recipe)
        with self.assertRaisesRegex(ValueError, 'revisions differ'):
            layout.combined_recipe({'chromium_revision': 'other'}, 'pin', self.recipe,
                                   self.upstream)

    def test_copy_coverage_records_exact_selected_action_consumers(self):
        graph = {'//a': {'script': '//' + self.name},
                 '//b': {'script': '//' + self.name},
                 '//other': {'script': '//third_party/other/entry.py'}}
        self.assertEqual(layout.selected_entry_coverage(graph, self.recipe),
                         {self.name: ['//a', '//b']})
        with self.assertRaisesRegex(ValueError, 'absent from selected graph'):
            layout.selected_entry_coverage({}, self.recipe)

    def test_unreviewed_build_entry_is_rejected(self):
        self.recipe['files']['third_party/blink/renderer/build/scripts/unreviewed.py'] = (
            self.recipe['files'].pop(self.name))
        with self.assertRaisesRegex(ValueError, 'Invalid Blink Python entry'):
            self.inputs()

    def test_split_extracted_root_omits_project_and_local_document_directories(self):
        for name in ('standalone', 'docs', '.github', '__pycache__'):
            (self.extracted / name).mkdir()
        manifest = {'owned_files': {}, 'split_sources': {}}
        recipe = layout.combined_recipe(self.dawn, 'pin', self.recipe, self.upstream)
        with mock.patch.object(journal, 'ROOT', self.extracted), mock.patch.object(
                engine, 'ROOT', self.extracted):
            self.assertTrue(journal.stage(self.view, self.base, manifest, self.inputs(),
                                          engine.write_overlay, {'renderer'}, recipe))
            self.assertEqual({p.name for p in (self.view / 'third_party/blink').iterdir()},
                             {'renderer'})
            self.assertFalse(journal.stage(self.view, self.base, manifest, self.inputs(),
                                           engine.write_overlay, {'renderer'}, recipe))

    def test_root_scope_changes_require_review(self):
        self.recipe['source_root_names'].append('standalone')
        with self.assertRaisesRegex(ValueError, 'Chromium revision differ'):
            self.inputs()


if __name__ == '__main__':
    unittest.main()
