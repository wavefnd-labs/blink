import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('prerequisites',
    Path(__file__).resolve().parents[1] / 'tools/build_prerequisites.py')
prerequisites = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prerequisites)


class PrerequisiteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.view = Path(self.temporary.name)
        (self.view / 'entry.py').write_text('import reviewed_dependency\n')
        self.graph = {'//engine:generator': {'type': 'action', 'script': '//entry.py'}}
        self.config = {'audited_action_scripts': {
            'entry.py': hashlib.sha256((self.view / 'entry.py').read_bytes()).hexdigest()},
            'python_help_entries': ['entry.py']}

    def test_selected_inventory_checks_actual_bytes(self):
        self.assertEqual(prerequisites.verify_inventory(self.view, self.graph, self.config), 1)
        (self.view / 'entry.py').write_text('import new_unreviewed_dependency\n')
        with self.assertRaisesRegex(ValueError, 'missing or changed'):
            prerequisites.verify_inventory(self.view, self.graph, self.config)

    def test_new_action_or_removed_probe_requires_review(self):
        self.graph['//engine:new'] = {'type': 'action', 'script': '//unknown.py'}
        with self.assertRaisesRegex(ValueError, 'scripts changed'):
            prerequisites.verify_inventory(self.view, self.graph, self.config)
        del self.graph['//engine:new']
        self.config['python_help_entries'].append('unselected.py')
        with self.assertRaisesRegex(ValueError, 'not a selected action'):
            prerequisites.verify_inventory(self.view, self.graph, self.config)

    def test_missing_selected_script_fails_before_probes(self):
        (self.view / 'entry.py').unlink()
        with self.assertRaisesRegex(ValueError, 'missing or changed'):
            prerequisites.verify_inventory(self.view, self.graph, self.config)

    def test_tool_probes_cannot_turn_into_build_commands(self):
        for args in (['--version'], ['--help'], ['version']):
            self.assertEqual(prerequisites.safe_arguments(args), args)
        for args in ([], ['build'], ['-c', 'engine.cc'], ['--version', '--output=engine.o']):
            with self.assertRaisesRegex(ValueError, 'only request version/help'):
                prerequisites.safe_arguments(args)

    def test_prerequisite_paths_stay_in_selected_source_view(self):
        for name in ('/external/tool', '../tool', 'dir/../tool', 'dir//tool'):
            with self.assertRaisesRegex(ValueError, 'Invalid prerequisite path'):
                prerequisites.relative_path(name)
