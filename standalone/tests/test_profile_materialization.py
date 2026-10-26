"""Selected-view tool probes and verifier reports preserve release evidence."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1] / 'tools'


def module(name):
    spec = importlib.util.spec_from_file_location('profile_' + name, TOOLS / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    with mock.patch.object(sys, 'path', [str(TOOLS), *sys.path]):
        spec.loader.exec_module(result)
    return result


packages = module('materialize_packages')
implicit = module('materialize_implicit_sources')
nested = module('materialize_nested_sources')


class ProfileMaterializationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_tool_check_uses_explicit_view_without_release_pointer_or_report(self):
        work = self.root / 'graph'
        source = work / 'src'
        source.mkdir(parents=True)
        view = self.root / 'profile-view'
        output = view / 'out/Engine'
        output.mkdir(parents=True)
        (view / 'third_party').symlink_to(source / 'third_party')
        (view / 'buildtools').symlink_to(source / 'buildtools')
        current = self.root / 'standalone/work/engine/current.json'
        current.parent.mkdir(parents=True)
        current.write_text('{"view":"/missing-release-view-must-not-be-read"}\n')
        old_current = current.read_bytes()
        (output / 'build.ninja').write_text('command = ' + str(work / 'tools/gn') + ' --regeneration gen .')
        names = ['../../third_party/llvm-build/Release+Asserts/bin/clang',
                 '../../third_party/llvm-build/Release+Asserts/bin/clang++',
                 '../../third_party/rust-toolchain/bin/rustc',
                 '../../third_party/node/linux/node-linux-x64/bin/node',
                 '../../third_party/gperf/cipd/bin/gperf',
                 '../../third_party/devtools-frontend/src/third_party/esbuild/esbuild',
                 '../../third_party/dawn/tools/generate-sources-gn.py',
                 '../../buildtools/third_party/mold/cipd/mold', '/usr/bin/python3']
        (output / 'toolchain.ninja').write_text('\n'.join(names))
        lock = self.root / 'lock.json'
        lock.write_text(json.dumps({'chromium_revision': 'fixed', 'profile': {
            'name': 'baseline', 'script_executable': '/usr/bin/python3', 'gn_sha256': '0' * 64}}))
        release_report = self.root / 'release-tools.json'
        release_report.write_text('preserve release tools receipt\n')
        selected_report = self.root / 'profile/tools.json'
        with mock.patch.multiple(packages, ROOT=self.root, WORK=work, LOCK=lock,
                                 TOOLS_REPORT=release_report):
            with mock.patch.object(packages, 'sha256', return_value='0' * 64):
                with mock.patch.object(packages.subprocess, 'run', side_effect=lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 0, stdout='version', stderr='')):
                    report = packages.check_tools(view=view, report_path=selected_report)
        self.assertTrue(report['ninja_paths_verified'])
        self.assertTrue(selected_report.is_file())
        self.assertEqual(current.read_bytes(), old_current)
        self.assertEqual(release_report.read_text(), 'preserve release tools receipt\n')

    def test_implicit_verification_report_override_preserves_default(self):
        work = self.root / 'graph'
        (work / 'chromium.git').mkdir(parents=True)
        release_report = self.root / 'release-implicit.json'
        release_report.write_text('preserved implicit receipt\n')
        report = self.root / 'profile/implicit.json'
        lock = {'chromium_revision': 'pin', 'dependencies': {}, 'main_headers': {}}
        with mock.patch.multiple(implicit, ROOT=self.root, REPORT=release_report):
            with mock.patch.multiple(implicit.materialize, WORK=work, SRC=work / 'src'):
                with mock.patch.object(implicit, 'load_selection', return_value=(lock, {}, {}, 'hash')):
                    with mock.patch.object(implicit.materialize, 'dependency') as download:
                        with redirect_stdout(io.StringIO()):
                            self.assertEqual(implicit.main(['--verify', '--report-path', str(report)]), 0)
                        download.assert_not_called()
        self.assertTrue(json.loads(report.read_text())['complete'])
        self.assertEqual(release_report.read_text(), 'preserved implicit receipt\n')

    def test_nested_verification_report_override_preserves_default(self):
        support = self.root / 'standalone'
        config = support / 'config'
        config.mkdir(parents=True)
        lock = config / 'nested-source-dependencies.lock.json'
        lock.write_text(json.dumps({'chromium_revision': 'pin', 'profile': 'baseline',
            'root_deps_sha256': 'hash', 'dependencies': {}}))
        (config / 'dependencies.lock.json').write_text(json.dumps({
            'chromium_revision': 'pin', 'profile': {'name': 'baseline'}, 'root_deps_sha256': 'hash'}))
        release_report = self.root / 'release-nested.json'
        release_report.write_text('preserved nested receipt\n')
        report = self.root / 'profile/nested.json'
        with mock.patch.multiple(nested, SUPPORT=support, LOCK=lock, REPORT=release_report):
            with mock.patch.object(nested.dependencies, 'inventory', return_value={'deps_sha256': 'hash'}):
                with mock.patch.object(nested.materialize, 'dependency') as download:
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(nested.main(['--verify', '--report-path', str(report)]), 0)
                    download.assert_not_called()
        self.assertTrue(json.loads(report.read_text())['complete'])
        self.assertEqual(release_report.read_text(), 'preserved nested receipt\n')


if __name__ == '__main__':
    unittest.main()
