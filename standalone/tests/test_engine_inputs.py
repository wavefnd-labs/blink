"""Declared build-input validation without executing any Ninja build actions."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    'engine_input_tools', Path(__file__).resolve().parents[1] / 'tools/engine.py')
engine = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(engine)


class DeclaredEngineInputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.view = self.root / 'view'
        self.output = self.view / 'out/Engine'
        self.output.mkdir(parents=True)
        self.support = self.root / 'support'
        patcher = mock.patch.object(engine, 'SUPPORT', self.support)
        patcher.start()
        self.addCleanup(patcher.stop)

    def inspect(self, inputs):
        with mock.patch.object(engine.subprocess, 'check_output', return_value=inputs) as ninja:
            report = engine.check_declared_inputs('pinned-ninja', self.output, self.view, ['blink_engine'])
        ninja.assert_called_once_with(
            ['pinned-ninja', '-C', str(self.output), '-t', 'inputs', 'blink_engine'], text=True)
        saved = json.loads((self.support / 'reports/linux-x64-engine-build-inputs.json').read_text())
        self.assertEqual(saved, report)
        self.assertFalse(report['compiled'])
        return report

    def test_shell_quoted_source_filename_with_spaces_is_present(self):
        source = self.view / 'resources/a file with spaces.json'
        source.parent.mkdir()
        source.write_text('{}')
        report = self.inspect("'../../resources/a file with spaces.json'\n")
        self.assertEqual(report['declared_input_count'], 1)
        self.assertEqual(report['declared_source_input_count'], 1)
        self.assertEqual(report['missing_source_inputs'], [])
        self.assertTrue(report['all_declared_source_inputs_present'])

    def test_source_symlink_to_pinned_external_tree_is_valid(self):
        original = self.root / 'pinned-source'
        original.mkdir()
        (original / 'header.h').write_text('// upstream header')
        (self.view / 'public').symlink_to(original, target_is_directory=True)
        report = self.inspect('../../public/header.h\n')
        self.assertEqual(report['declared_source_input_count'], 1)
        self.assertTrue(report['all_declared_source_inputs_present'])
        self.assertEqual(report['missing_source_inputs'], [])

    def test_missing_generated_outputs_are_excluded_from_source_checks(self):
        report = self.inspect('gen/generated.h\nobj/library.o\n./gen/generated.h\n')
        self.assertEqual(report['declared_input_count'], 3)
        self.assertEqual(report['declared_source_input_count'], 0)
        self.assertEqual(report['missing_source_inputs'], [])
        self.assertTrue(report['all_declared_source_inputs_present'])

    def test_missing_declared_sources_are_counted_and_deduplicated(self):
        (self.view / 'existing.cc').write_text('// source')
        report = self.inspect('../../missing_z.h\n../../existing.cc\n../../missing_a.h\n../../missing_z.h\ngen/generated.h\n')
        self.assertEqual(report['declared_input_count'], 5)
        self.assertEqual(report['declared_source_input_count'], 3)
        self.assertEqual(report['missing_source_inputs'], ['missing_a.h', 'missing_z.h'])
        self.assertFalse(report['all_declared_source_inputs_present'])

    def test_lexical_path_escaping_view_is_rejected_before_report(self):
        (self.root / 'outside.h').write_text('// exists, but outside the source view')
        with mock.patch.object(engine.subprocess, 'check_output', return_value='../../../outside.h\n'):
            with self.assertRaisesRegex(ValueError, 'escapes the engine view'):
                engine.check_declared_inputs('pinned-ninja', self.output, self.view, ['blink_engine'])
        self.assertFalse((self.support / 'reports/linux-x64-engine-build-inputs.json').exists())

    def test_unquoted_ambiguous_input_is_rejected(self):
        with mock.patch.object(engine.subprocess, 'check_output', return_value='../../two paths.h\n'):
            with self.assertRaisesRegex(ValueError, 'Unexpected Ninja input path'):
                engine.check_declared_inputs('pinned-ninja', self.output, self.view, ['blink_engine'])


if __name__ == '__main__':
    unittest.main()
