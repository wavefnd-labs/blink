import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


TOOLS = Path(__file__).resolve().parents[1] / 'tools'
SPEC = importlib.util.spec_from_file_location('rustfmt_layout', TOOLS / 'rustfmt_layout.py')
rustfmt_layout = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rustfmt_layout)


class RustfmtLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name) / 'base'
        self.view = Path(self.temporary.name) / 'view'
        self.base.mkdir()
        self.view.mkdir()
        self.data = b'edition = "2024"\nignore = ["third_party/**"]\n'
        (self.base / '.rustfmt.toml').write_bytes(self.data)
        self.recipe = {
            'chromium_revision': 'reviewed-revision',
            'files': {'.rustfmt.toml': {
                'mode': 'copy', 'original_sha256': rustfmt_layout.digest(self.data)}}}

    def inputs(self):
        return rustfmt_layout.configuration_inputs(self.base, 'reviewed-revision', self.recipe)

    def test_copies_original_configuration_without_changing_ignore_patterns(self):
        inputs = self.inputs()
        self.assertEqual(inputs, {'.rustfmt.toml': self.data})
        (self.view / '.rustfmt.toml').write_bytes(inputs['.rustfmt.toml'])
        rustfmt_layout.verify_view(self.view, inputs)
        self.assertEqual((self.base / '.rustfmt.toml').read_bytes(), self.data)

    def test_rejects_configuration_symlink_even_when_bytes_match(self):
        (self.view / '.rustfmt.toml').symlink_to(self.base / '.rustfmt.toml')
        with self.assertRaisesRegex(ValueError, 'local copy'):
            rustfmt_layout.verify_view(self.view, self.inputs())

    def test_rejects_configuration_drift(self):
        (self.base / '.rustfmt.toml').write_text('ignore = []\n')
        with self.assertRaisesRegex(ValueError, 'configuration changed'):
            self.inputs()

    def test_rejects_revision_drift(self):
        with self.assertRaisesRegex(ValueError, 'revision differ'):
            rustfmt_layout.configuration_inputs(self.base, 'different', self.recipe)

    def test_rejects_recipe_path_traversal(self):
        self.recipe['files']['../escape'] = self.recipe['files'].pop('.rustfmt.toml')
        with self.assertRaisesRegex(ValueError, 'Invalid'):
            self.inputs()

    def test_rejects_changed_view_configuration(self):
        (self.view / '.rustfmt.toml').write_text('ignore = []\n')
        with self.assertRaisesRegex(ValueError, 'unchanged local copy'):
            rustfmt_layout.verify_view(self.view, self.inputs())

    @unittest.skipUnless((rustfmt_layout.BASE / 'third_party/rust-toolchain/bin/rustfmt').is_file(),
                         'Pinned rustfmt is not materialized')
    def test_pinned_formatter_reproduces_failure_and_formats_with_local_copy(self):
        report = rustfmt_layout.regression_check()
        self.assertTrue(report['verified'])
        self.assertTrue(report['formatting_enabled'])
        self.assertEqual(report['cases']['symlink']['exit_code'], 101)
        self.assertEqual(report['cases']['copy']['exit_code'], 0)
        self.assertFalse(report['full_compilation'])


if __name__ == '__main__':
    unittest.main()
