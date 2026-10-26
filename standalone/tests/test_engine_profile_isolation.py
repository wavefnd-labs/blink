"""Keep opt-in development builds away from an existing release checkout/cache.

The fixture exercises real view staging and report writes in a temporary source
tree. Profile/cache policy providers are stubs; no repository view, compiler,
downloader, GN, Ninja or real cache is invoked by these tests.
"""
from contextlib import ExitStack, redirect_stdout
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock


TOOL = Path(__file__).resolve().parents[1] / 'tools/engine.py'
SPEC = importlib.util.spec_from_file_location('isolated_engine_tools', TOOL)
engine = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(engine)


class EngineProfileIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='blink-profile-tests-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.support = self.root / 'standalone'
        self.base = self.support / 'work/graph/src'
        self.work = self.support / 'work/engine'
        self.gn = self.support / 'work/graph/tools/gn'
        self.revision = 'a' * 40
        self.original_compiler = b'# Original upstream compiler configuration\n'
        self.profile_definition = {'name': 'dev', 'reviewed_configuration': 'version-1'}
        self.profile_overlay = b'# Reviewed low-optimization compiler configuration\n'
        self.cache_definition = {'backend': 'test-cache', 'reviewed_archive': 'version-1'}
        self.cache_prepared = {
            'binary': str(self.support / 'work/compiler-cache/tools/ccache'),
            'wrapper': 'python3 -B ' + str(self.support / 'tools/compiler_cache.py') + ' wrap',
            'wrapper_sha256': engine.digest(b'fixture compiler wrapper version 1'),
            'config': str(self.support / 'work/compiler-cache/private.conf'),
            'cache_directory': str(self.support / 'work/compiler-cache/data'),
        }
        self.write(self.base / 'build/config/gclient_args.gni', b'# Fixed source fixture\n')
        self.write(self.base / 'build/config/compiler/BUILD.gn', self.original_compiler)
        self.write(self.support / 'work/graph/declared-deps.json', b'{}\n')
        self.write(self.gn, b'fixture GN identity; never execute\n')
        self.write(self.support / 'gn/BUILD.gn', b'group("blink_engine") {}\n')
        self.write(self.support / 'gn/dotfile.gn', b'buildconfig = "//build/BUILDCONFIG.gn"\n')
        self.write(self.support / 'gn/VERSION', b'MAJOR=1\nMINOR=0\nBUILD=0\nPATCH=0\n')
        self.write(self.support / 'patches/import.patch', b'fixture reviewed import patch\n')
        self.recipe = {
            'chromium_revision': self.revision, 'files': {},
            'patch': {'path': 'standalone/patches/import.patch',
                      'sha256': engine.digest((self.support / 'patches/import.patch').read_bytes())},
            'version_metadata': {'original_sha256': engine.digest((self.support / 'gn/VERSION').read_bytes())},
        }
        self.write_json(self.support / 'config/upstream.lock.json', {
            'revision': self.revision, 'protected_roots': ['public', 'renderer']})
        self.write_json(self.support / 'config/rustfmt-layout.json', {'files': {}})
        self.write_json(self.support / 'config/dependencies.lock.json', {
            'chromium_revision': self.revision,
            'profile': {'gn_sha256': engine.digest(self.gn.read_bytes()),
                        'script_executable': '/usr/bin/python3'}})
        self.write_json(self.support / 'config/engine-adaptations.json', self.recipe)

        self.profiles = mock.Mock()
        self.profiles.identity.side_effect = lambda name: copy.deepcopy(self.profile_definition)
        self.profiles.args_text.return_value = 'is_debug = false\nis_official_build = false\n'
        self.profiles.overlays.side_effect = lambda name, base: {
            'build/config/compiler/BUILD.gn': self.profile_overlay}
        self.cache = mock.Mock()
        self.cache.prepare.side_effect = lambda download=False: copy.deepcopy(self.cache_prepared)
        self.cache.load.side_effect = lambda: copy.deepcopy(self.cache_definition)
        self.cache.environment.side_effect = self.cache_environment
        self.generator_layout = mock.Mock()

        def stage(view, base, manifest, *args, **kwargs):
            manifest['generator_layout_copies'] = {}
            return False

        self.generator_layout.stage.side_effect = stage
        rust_layout = mock.Mock()
        rust_layout.configuration_inputs.return_value = {}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.multiple(engine, ROOT=self.root, SUPPORT=self.support,
                                                    BASE=self.base, WORK=self.work, GN=self.gn))
        self.stack.enter_context(mock.patch.object(engine, 'verify_blink'))
        self.stack.enter_context(mock.patch.object(engine, 'verify_implicit_sources'))
        self.stack.enter_context(mock.patch.object(engine, 'layout_module', return_value=rust_layout))
        self.stack.enter_context(mock.patch.object(engine, 'generator_layout_inputs',
            return_value=(self.generator_layout, {}, {'files': {}})))
        self.modules = self.stack.enter_context(mock.patch.object(engine, 'support_module',
                                                                  side_effect=self.module))
        self.stack.enter_context(mock.patch.object(engine.subprocess, 'check_output',
                                                   side_effect=self.git_metadata))

    @staticmethod
    def write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def write_json(self, path, value):
        self.write(path, (json.dumps(value) + '\n').encode())

    def module(self, name):
        if name == 'build_profiles':
            return self.profiles
        if name == 'compiler_cache':
            return self.cache
        self.fail('Unexpected policy provider: ' + name)

    def git_metadata(self, command, **kwargs):
        self.assertEqual(command[:1], ['git'], 'Tests may not run GN/Ninja or a compiler')
        if command[-2:] == ['rev-parse', 'HEAD']:
            return self.revision + '\n'
        if 'ls-tree' in command:
            return 'build\n'
        self.fail('Unexpected Git command: ' + repr(command))

    @staticmethod
    def cache_environment(inherited, prepared):
        result = {name: value for name, value in inherited.items() if not name.startswith('CCACHE_')}
        result.update(CCACHE_CONFIGPATH=prepared['config'], CCACHE_DIR=prepared['cache_directory'])
        return result

    @staticmethod
    def snapshot(path):
        data = path.read_bytes()
        metadata = path.stat()
        return hashlib.sha256(data).hexdigest(), metadata.st_size, metadata.st_mtime_ns, stat.S_IMODE(metadata.st_mode)

    def release_sentinel(self):
        view = engine.prepare()
        sentinel = view / 'out/Engine/obj/completed-release.o'
        self.write(sentinel, b'Existing completed release object; preserve this cache\n')
        return view, sentinel, self.snapshot(sentinel)

    def cli(self, *args):
        with mock.patch.object(sys, 'argv', ['engine.py', *args]), redirect_stdout(io.StringIO()):
            return engine.main()

    def test_explicit_default_release_reuses_legacy_key_pointer_and_completed_objects(self):
        view, sentinel, original = self.release_sentinel()
        pointer = self.work / 'current.json'
        pointer_before = self.snapshot(pointer)
        context = engine.build_context('release')
        self.assertEqual(engine.prepare(context), view)
        self.assertEqual(self.snapshot(pointer)[0], pointer_before[0])
        self.assertEqual(self.snapshot(sentinel), original)
        self.assertEqual(context['work_dir'], self.work)
        self.assertEqual(engine.current_path(context), pointer)
        self.assertEqual(engine.report_path('build.json', context), self.support / 'reports/build.json')
        self.assertNotIn('build_profile', engine.read_json(view.parent / 'view.json'))
        self.modules.assert_not_called()

    def test_dev_cache_and_no_cache_stage_separate_views_without_touching_release(self):
        release, sentinel, original = self.release_sentinel()
        pointer = self.work / 'current.json'
        pointer_before = self.snapshot(pointer)
        uncached = engine.build_context('dev', cache=False)
        cached = engine.build_context('dev')
        uncached_view = engine.prepare(uncached)
        cached_view = engine.prepare(cached)
        self.assertEqual(len({release, uncached_view, cached_view}), 3)
        self.assertNotEqual(uncached['work_dir'], cached['work_dir'])
        self.assertNotEqual(engine.read_json(uncached_view.parent / 'view.json')['key'],
                            engine.read_json(cached_view.parent / 'view.json')['key'])
        self.assertNotIn('cc_wrapper', uncached['args'])
        self.assertIn(self.cache_prepared['wrapper'], cached['args'])
        self.assertEqual((self.base / 'build/config/compiler/BUILD.gn').read_bytes(), self.original_compiler)
        self.assertEqual((release / 'build/config/compiler/BUILD.gn').read_bytes(), self.original_compiler)
        self.assertEqual((cached_view / 'build/config/compiler/BUILD.gn').read_bytes(), self.profile_overlay)
        self.assertEqual(self.snapshot(pointer), pointer_before)
        self.assertEqual(self.snapshot(sentinel), original)
        self.assertEqual(engine.read_json(engine.current_path(uncached))['view'], str(uncached_view))
        self.assertEqual(engine.read_json(engine.current_path(cached))['view'], str(cached_view))

    def test_dev_input_report_cannot_overwrite_release_report(self):
        context = engine.build_context('dev', cache=False)
        view = engine.prepare(context)
        release_report = self.support / 'reports/linux-x64-engine-build-inputs.json'
        self.write(release_report, b'Existing release verification receipt\n')
        before = self.snapshot(release_report)
        with mock.patch.object(engine.subprocess, 'check_output',
                               return_value='../../build/config/gclient_args.gni\n'):
            checked = engine.check_declared_inputs('ninja-not-executed', view / 'out/Engine',
                                                   view, ['blink_engine'], context)
        self.assertTrue(checked['all_declared_source_inputs_present'])
        self.assertEqual(self.snapshot(release_report), before)
        self.assertTrue(engine.report_path('linux-x64-engine-build-inputs.json', context).is_file())

    def test_cli_dev_dry_run_keeps_build_report_and_log_in_its_own_view(self):
        context = engine.build_context('dev', cache=False)
        view = engine.prepare(context)
        output = view / 'out/Engine'
        release_report = self.support / 'reports/linux-x64-engine-build.json'
        self.write(release_report, b'Existing successful release build receipt\n')
        before = self.snapshot(release_report)
        ninja = self.base / 'third_party/ninja/ninja'
        self.write(ninja, b'fixture Ninja identity; never execute\n')
        process = mock.Mock()
        process.stdout = io.StringIO('[1/1] simulated dry-run command\n')
        process.wait.return_value = 0
        inputs = {'declared_source_input_count': 0, 'missing_source_inputs': []}
        with mock.patch.object(engine, 'NINJA', ninja), \
                mock.patch.object(engine, 'generate', return_value=(view, output)), \
                mock.patch.object(engine, 'inspect'), \
                mock.patch.object(engine, 'check_build_prerequisites'), \
                mock.patch.object(engine, 'check_declared_inputs', return_value=inputs), \
                mock.patch.object(engine.subprocess, 'Popen', return_value=process) as child, \
                mock.patch.object(engine.subprocess, 'check_output', return_value='fixture-ninja-version\n'):
            self.assertEqual(self.cli('build', '--dry-run', '--profile', 'dev', '--no-cache'), 0)
        self.assertEqual(self.snapshot(release_report), before)
        report = engine.read_json(engine.report_path('linux-x64-engine-build.json', context))
        self.assertEqual(report['build_profile'], 'dev')
        self.assertFalse(report['compiled'])
        self.assertEqual(Path(report['log']), view.parent / 'ninja-build.log')
        self.assertIn('-n', child.call_args.args[0])
        self.assertIn(str(output), child.call_args.args[0])
        self.assertFalse((self.work / 'views').exists())

    def test_child_environments_keep_python_and_optional_cache_state_local(self):
        release = engine.build_context('release')
        uncached = engine.build_context('dev', cache=False)
        cached = engine.build_context('dev')
        hostile = {'PYTHONPATH': '/unreviewed/python', 'PYTHONPYCACHEPREFIX': '/global/python-cache',
                   'CCACHE_DIR': '/global/compiler-cache', 'CCACHE_PREFIX': 'unreviewed-wrapper'}
        with mock.patch.dict(os.environ, hostile, clear=True):
            inherited = dict(os.environ)
            environments = [engine.build_environment(context) for context in (release, uncached, cached)]
            self.assertEqual(dict(os.environ), inherited)
        for context, environment in zip((release, uncached, cached), environments):
            self.assertNotIn('PYTHONPATH', environment)
            self.assertEqual(environment['PYTHONDONTWRITEBYTECODE'], '1')
            self.assertEqual(Path(environment['PYTHONPYCACHEPREFIX']), context['work_dir'] / 'python-cache')
            self.assertFalse(Path(environment['PYTHONPYCACHEPREFIX']).is_relative_to(self.base))
        self.assertEqual(len({env['PYTHONPYCACHEPREFIX'] for env in environments}), 3)
        self.assertEqual(environments[-1]['CCACHE_DIR'], self.cache_prepared['cache_directory'])
        self.assertNotIn('CCACHE_PREFIX', environments[-1])
        self.cache.environment.assert_called_once()
        self.assertNotIn('PYTHONPATH', self.cache.environment.call_args.args[0])

    def test_cli_release_cache_request_fails_before_loading_tools_or_staging(self):
        with mock.patch.object(engine, 'generate') as generate:
            with self.assertRaisesRegex(ValueError, 'release'):
                self.cli('gen', '--profile', 'release', '--cache')
        generate.assert_not_called()
        self.modules.assert_not_called()
        self.assertFalse(self.work.exists())

    def test_verify_rejects_changed_profile_policy_without_rebuilding_or_repairing(self):
        context = engine.build_context('dev', cache=False)
        view = engine.prepare(context)
        sentinel = view / 'out/Engine/obj/completed.o'
        self.write(sentinel, b'Existing development object\n')
        preserved = {p: self.snapshot(p) for p in (sentinel, engine.current_path(context), view.parent / 'view.json')}
        self.profile_definition['reviewed_configuration'] = 'unreviewed-version-2'
        with mock.patch.object(engine, 'generate') as generate:
            with self.assertRaisesRegex(ValueError, 'development profile'):
                self.cli('verify', '--profile', 'dev', '--no-cache')
            generate.assert_not_called()
        self.assertEqual({p: self.snapshot(p) for p in preserved}, preserved)

    def test_verify_rejects_cache_identity_drift_without_publishing_a_new_pointer(self):
        context = engine.build_context('dev')
        view = engine.prepare(context)
        pointer_before = self.snapshot(engine.current_path(context))
        manifest_before = self.snapshot(view.parent / 'view.json')
        self.cache_definition['reviewed_archive'] = 'unreviewed-version-2'
        with self.assertRaisesRegex(ValueError, 'cache settings changed'):
            self.cli('verify', '--profile', 'dev')
        self.cache.prepare.assert_called_with(download=False)
        self.assertEqual(self.snapshot(engine.current_path(context)), pointer_before)
        self.assertEqual(self.snapshot(view.parent / 'view.json'), manifest_before)

    def test_changed_cache_wrapper_code_cannot_reuse_an_old_verified_view(self):
        context = engine.build_context('dev')
        view = engine.prepare(context)
        before = self.snapshot(engine.current_path(context))
        self.cache_prepared['wrapper_sha256'] = engine.digest(b'changed dependency-file behavior')
        with self.assertRaisesRegex(ValueError, 'cache settings changed'):
            self.cli('verify', '--profile', 'dev')
        self.assertEqual(self.snapshot(engine.current_path(context)), before)
        self.assertEqual(engine.read_json(view.parent / 'view.json')['build_profile']['compiler_cache']['pin'],
                         self.cache_definition)

    def test_release_verify_rejects_a_pointer_to_a_development_view(self):
        context = engine.build_context('dev', cache=False)
        view = engine.prepare(context)
        self.write_json(self.work / 'current.json', {'view': str(view), 'key': view.parent.name})
        before = self.snapshot(self.work / 'current.json')
        with self.assertRaisesRegex(ValueError, 'release pointer'):
            self.cli('verify')
        self.assertEqual(self.snapshot(self.work / 'current.json'), before)


if __name__ == '__main__':
    unittest.main()
