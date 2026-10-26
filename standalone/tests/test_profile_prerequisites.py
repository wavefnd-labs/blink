"""Profile preflight reports and probe logs cannot clobber release evidence."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('profile_prerequisites',
    Path(__file__).resolve().parents[1] / 'tools/build_prerequisites.py')
prerequisites = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prerequisites)


class ProfilePrerequisiteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.support = self.root / 'standalone'
        self.view = self.support / 'work/engine/profiles/dev-no-cache/views/key/src'
        (self.view / 'out/Engine').mkdir(parents=True)
        data = b'# reviewed fixture script\n'
        (self.view / 'entry.py').write_bytes(data)
        self.graph = {'//engine:generator': {'type': 'action', 'script': '//entry.py'}}
        config_dir = self.support / 'config'
        config_dir.mkdir()
        config = {'schema': 1, 'chromium_revision': 'pin', 'lock_sha256': {},
                  'audited_action_scripts': {'entry.py': hashlib.sha256(data).hexdigest()},
                  'python_help_entries': ['entry.py'], 'executable_probes': {},
                  'dynamic_libraries': {}, 'library_search_directory': 'lib'}
        (config_dir / 'build-prerequisites.json').write_text(json.dumps(config))
        (config_dir / 'upstream.lock.json').write_text('{"revision":"pin"}\n')
        (config_dir / 'dependencies.lock.json').write_text(json.dumps(
            {'profile': {'script_executable': sys.executable}}))
        for name, value in (('ROOT', self.root), ('SUPPORT', self.support),
                            ('CONFIG', config_dir / 'build-prerequisites.json')):
            patch = mock.patch.object(prerequisites, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(prerequisites.subprocess, 'run',
            return_value=subprocess.CompletedProcess(['fixture'], 0, 'fixture safe help\n'))
        self.probes = patch.start()
        self.addCleanup(patch.stop)
        self.release_report = self.support / 'reports/linux-x64-build-prerequisites.json'
        self.release_log = self.support / 'work/diagnostics/prerequisites/python-0.log'
        for path in (self.release_report, self.release_log):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('preserve release evidence\n')

    def preserved(self):
        for path in (self.release_report, self.release_log):
            self.assertEqual(path.read_text(), 'preserve release evidence\n')

    def test_explicit_dev_report_and_log_directories(self):
        report = self.support / 'reports/profiles/dev/prerequisites.json'
        diagnostics = self.support / 'work/engine/profiles/dev/diagnostics/prerequisites'
        result = prerequisites.check(self.view, self.graph, report_path=report,
                                     diagnostics_dir=diagnostics)
        self.assertTrue(result['passed'])
        self.assertTrue(report.is_file())
        self.assertTrue((diagnostics / 'python-0.log').is_file())
        self.preserved()

    def test_manual_dev_view_also_defaults_to_isolated_paths(self):
        result = prerequisites.check(self.view, self.graph)
        self.assertTrue(result['passed'])
        self.assertTrue((self.view.parent / 'reports/linux-x64-build-prerequisites.json').is_file())
        self.assertTrue((self.view.parent / 'diagnostics/prerequisites/python-0.log').is_file())
        self.preserved()

    def test_nested_verifiers_receive_selected_view_and_isolated_reports(self):
        diagnostics = self.support / 'work/engine/profiles/dev/diagnostics/prerequisites'
        prerequisites.check(self.view, self.graph, diagnostics_dir=diagnostics)
        commands = [call.args[0] for call in self.probes.call_args_list]
        package = next(c for c in commands if 'materialize_packages.py' in c[2])
        self.assertEqual(package[package.index('--view') + 1], str(self.view))
        self.assertEqual(package[package.index('--tools-report') + 1],
                         str(diagnostics / 'materialize-build-tools.json'))
        for name in ('materialize_implicit_sources', 'materialize_nested_sources'):
            command = next(c for c in commands if name + '.py' in c[2])
            self.assertIn('--verify', command)
            self.assertEqual(command[command.index('--report-path') + 1],
                             str(diagnostics / (name + '.json')))
        self.preserved()


if __name__ == '__main__':
    unittest.main()
