"""Receipt/runner failure guards with temporary files and a mocked native child.

These tests do not compile or execute Blink and do not write production runtime
success evidence. The native result fixtures are confined to a temporary repo.
"""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('runtime_checks_tools', Path(__file__).resolve().parents[1] / 'tools/runtime_checks.py')
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


def passing_json():
    return {'schema_version': 3, 'abi_version': 1, 'initialized': True,
            'html_committed': True, 'host_resource_fetches': 1, 'pixel_assertions': True,
            'resized': True, 'wrong_thread_rejected': True, 'shutdown': True,
            'width': 80, 'height': 80, 'rgba_fnv1a64': '1234567890abcdef',
            'javascript': True, 'input': True, 'navigation': True,
            'ime': True, 'child_frames': True, 'storage': True, 'workers': True}


class RuntimeChecksTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.view = self.root / 'standalone/work/engine/views/fixture/src'
        self.output = self.view / 'out/Engine'
        self.output.mkdir(parents=True)
        self.state = {'git_commit': 'a' * 40, 'git_worktree_clean': True,
                      'locks': {'test': 'b' * 64}, 'support_files': {}}
        patch = mock.patch.object(runtime.automation, 'snapshot', side_effect=lambda root: copy.deepcopy(self.state))
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(runtime.automation, 'identity', return_value={
            'version': '1.0.1.0-engine.1', 'profile': 'linux-x64', 'abi_version': 1,
            'release_track': 'development'})
        patch.start()
        self.addCleanup(patch.stop)
        self.write('libblink_embed.so', b'\x7fELFfixture-shared-library')
        smoke = self.write('blink_embed_smoke', b'\x7fELFfixture-harness')
        smoke.chmod(0o755)
        self.write('icudtl.dat', b'fixture ICU')
        self.write('v8_context_snapshot.bin', b'fixture Blink V8 snapshot')
        self.write('snapshot_blob.bin', b'fixture other snapshot that must not be chosen')
        self.write('gen/third_party/blink/public/resources/blink_resources.pak', b'fixture resources')
        self.flags(True)
        for name in runtime.automation.LOCKS:
            path = self.root / 'standalone/config' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{}\n')
        for name in ('LICENSE', 'LICENSE_FOR_ABOUT_CREDITS', 'README.md'):
            (self.root / name).write_text('fixture original ' + name)
        (self.root / 'standalone/include').mkdir()
        (self.root / 'standalone/include/blink_engine.h').write_text('/* fixture API */\n')
        (self.root / 'standalone/engine').mkdir()
        (self.root / 'standalone/engine/NOTICE').write_text('fixture integration notices\n')
        (self.root / 'standalone/runtime.md').write_text('fixture: unsupported host integrations\n')

    def write(self, name, data):
        path = self.output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def flags(self, enabled, filename='v8_context_snapshot.bin'):
        self.write(runtime.BUILDFLAGS,
                   ('#define BUILDFLAG_INTERNAL_USE_V8_CONTEXT_SNAPSHOT() (' + str(int(enabled)) + ')\n' +
                    '#define BUILDFLAG_INTERNAL_V8_CONTEXT_SNAPSHOT_FILENAME() ("' + filename + '")\n').encode())

    def build(self, with_smoke=True):
        report = runtime.build_receipt(self.view, self.output, with_smoke, copy.deepcopy(self.state), root=self.root)
        report.update(exit_code=0, dry_run=False)
        runtime.automation.write_json(self.root / runtime.BUILD_REPORT, report)
        return report

    def result(self, value=None, code=0):
        return subprocess.CompletedProcess(['fixture'], code, json.dumps(passing_json() if value is None else value), 'fixture diagnostic')

    def test_build_receipt_binds_all_native_resources(self):
        receipt = self.build()
        self.assertTrue(receipt['compiled'])
        self.assertTrue(receipt['smoke_built'])
        self.assertFalse(receipt['runtime_smoke_passed'])
        self.assertEqual(receipt['runtime_resources']['v8_snapshot'], 'v8_context_snapshot.bin')
        runtime.verify_build(receipt, self.root)
        self.assertTrue(all(not Path(row['path']).is_absolute() for row in receipt['built_artifacts']))

    def test_build_cannot_bind_a_source_snapshot_that_changed(self):
        old = copy.deepcopy(self.state)
        self.state['locks']['test'] = 'c' * 64
        with self.assertRaisesRegex(ValueError, 'changed during'):
            runtime.build_receipt(self.view, self.output, True, old, root=self.root)

    def test_generated_flag_selects_snapshot_even_if_both_exist(self):
        self.flags(False)
        self.assertEqual(runtime.resource_paths(self.output)['v8_snapshot'], 'snapshot_blob.bin')
        self.flags(True)
        self.assertEqual(runtime.resource_paths(self.output)['v8_snapshot'], 'v8_context_snapshot.bin')

    def test_missing_selected_snapshot_does_not_fall_back_to_other_file(self):
        (self.output / 'v8_context_snapshot.bin').unlink()
        with self.assertRaisesRegex(ValueError, 'missing'):
            runtime.resource_paths(self.output)

    def test_generated_filename_cannot_escape_output(self):
        self.flags(True, '../elsewhere.bin')
        with self.assertRaises(ValueError):
            runtime.resource_paths(self.output)

    def test_symlink_cannot_replace_a_built_resource(self):
        path = self.output / 'icudtl.dat'
        path.unlink()
        path.symlink_to(self.output / 'snapshot_blob.bin')
        with self.assertRaisesRegex(ValueError, 'invalid'):
            self.build()

    def test_smoke_refuses_stale_source_and_invalidates_old_success(self):
        self.build()
        runtime.automation.write_json(self.root / runtime.SMOKE_REPORT, {'runtime_smoke_passed': True})
        self.state['git_commit'] = 'd' * 40
        with mock.patch.object(runtime.subprocess, 'run') as child:
            with self.assertRaisesRegex(ValueError, 'stale'):
                runtime.smoke(root=self.root)
            child.assert_not_called()
        report = runtime.automation.read_json(self.root / runtime.SMOKE_REPORT)
        self.assertFalse(report['runtime_smoke_passed'])

    def test_smoke_refuses_changed_binary_without_executing_it(self):
        self.build()
        self.write('libblink_embed.so', b'\x7fELFchanged-shared-library')
        with mock.patch.object(runtime.subprocess, 'run') as child:
            with self.assertRaisesRegex(ValueError, 'changed after compilation'):
                runtime.smoke(root=self.root)
            child.assert_not_called()

    def test_a_library_only_build_cannot_use_a_stale_harness(self):
        self.build(with_smoke=False)
        with mock.patch.object(runtime.subprocess, 'run') as child:
            with self.assertRaisesRegex(ValueError, '--with-smoke'):
                runtime.smoke(root=self.root)
            child.assert_not_called()

    def test_dry_run_receipt_cannot_execute_native_smoke(self):
        report = self.build()
        report['dry_run'] = True
        runtime.automation.write_json(self.root / runtime.BUILD_REPORT, report)
        with mock.patch.object(runtime.subprocess, 'run') as child:
            with self.assertRaisesRegex(ValueError, 'dry-run'):
                runtime.smoke(root=self.root)
            child.assert_not_called()

    def test_successful_native_result_stages_sdk_and_preserves_child_environment(self):
        self.build()
        with mock.patch.dict(os.environ, {'LD_LIBRARY_PATH': '/fixture/host-libraries'}):
            with mock.patch.object(runtime.subprocess, 'run', return_value=self.result()) as child:
                receipt = runtime.smoke(root=self.root)
            self.assertEqual(os.environ['LD_LIBRARY_PATH'], '/fixture/host-libraries')
        self.assertTrue(receipt['runtime_smoke_passed'])
        self.assertTrue(receipt['sdk_staged'])
        self.assertTrue(receipt['artifacts'])
        args, kwargs = child.call_args
        self.assertEqual(args[0][0], str(self.output / 'blink_embed_smoke'))
        self.assertIn('--v8-snapshot=' + str(self.output / 'v8_context_snapshot.bin'), args[0])
        self.assertEqual(kwargs['env']['LD_LIBRARY_PATH'], str(self.output) + os.pathsep + '/fixture/host-libraries')
        self.assertTrue(all(not Path(row['path']).is_absolute() for row in receipt['artifacts']))
        staged = self.root / receipt['sdk_directory']
        self.assertTrue((staged / 'notices/blink/ENGINE_NOTICE').exists())
        self.assertTrue((staged / 'docs/runtime.md').exists())
        self.assertTrue(runtime.automation.release_plan(self.root, receipt)['publishable'])

    def test_nonzero_native_exit_is_not_runtime_success(self):
        self.build()
        with mock.patch.object(runtime.subprocess, 'run', return_value=self.result(code=9)):
            with self.assertRaisesRegex(ValueError, 'status 9'):
                runtime.smoke(root=self.root)
        receipt = runtime.automation.read_json(self.root / runtime.SMOKE_REPORT)
        self.assertFalse(receipt['runtime_smoke_passed'])
        self.assertEqual(receipt['exit_code'], 9)
        self.assertFalse(receipt['sdk_staged'])

    def test_zero_exit_without_valid_pixel_receipt_is_not_success(self):
        self.build()
        value = passing_json()
        value['pixel_assertions'] = False
        with mock.patch.object(runtime.subprocess, 'run', return_value=self.result(value)):
            with self.assertRaisesRegex(ValueError, 'pixel_assertions'):
                runtime.smoke(root=self.root)
        self.assertFalse(runtime.automation.read_json(self.root / runtime.SMOKE_REPORT)['runtime_smoke_passed'])

    def test_native_timeout_is_recorded_as_failure(self):
        self.build()
        with mock.patch.object(runtime.subprocess, 'run', side_effect=subprocess.TimeoutExpired('fixture', 1)):
            with self.assertRaisesRegex(ValueError, 'timed out'):
                runtime.smoke(root=self.root, timeout=1)
        self.assertFalse(runtime.automation.read_json(self.root / runtime.SMOKE_REPORT)['runtime_smoke_passed'])

    def test_build_outputs_changed_during_native_execution_invalidate_receipt(self):
        self.build()
        def changed_during_execution(*args, **kwargs):
            self.write('v8_context_snapshot.bin', b'changed while the native child ran')
            return self.result()
        with mock.patch.object(runtime.subprocess, 'run', side_effect=changed_during_execution):
            with self.assertRaisesRegex(ValueError, 'changed after compilation'):
                runtime.smoke(root=self.root)
        report = runtime.automation.read_json(self.root / runtime.SMOKE_REPORT)
        self.assertFalse(report['runtime_smoke_passed'])
        self.assertFalse(report['sdk_staged'])

    def test_unstaged_sdk_does_not_leave_a_successful_distribution_receipt(self):
        self.build()
        with mock.patch.object(runtime.subprocess, 'run', return_value=self.result()):
            with mock.patch.object(runtime.automation, 'stage_sdk', side_effect=ValueError('fixture staging failed')):
                with self.assertRaisesRegex(ValueError, 'staging failed'):
                    runtime.smoke(root=self.root)
        report = runtime.automation.read_json(self.root / runtime.SMOKE_REPORT)
        self.assertTrue(report['native_smoke_passed'])
        self.assertFalse(report['runtime_smoke_passed'])
        self.assertEqual(report['artifacts'], [])

    def test_pixel_receipt_rejects_wrong_dimensions_missing_fields_and_bool_counts(self):
        for field, bad in (('width', 40), ('height', '80'), ('host_resource_fetches', True),
                           ('abi_version', 2), ('rgba_fnv1a64', '1234'), ('shutdown', 1),
                           ('schema_version', 1), ('javascript', False), ('input', False),
                           ('navigation', False), ('ime', False), ('child_frames', False),
                           ('storage', False), ('workers', False), ('schema_version', 2)):
            with self.subTest(field=field):
                value = passing_json()
                value[field] = bad
                with self.assertRaises(ValueError):
                    runtime.validate_smoke_json(json.dumps(value))
        value = passing_json()
        del value['wrong_thread_rejected']
        with self.assertRaises(ValueError):
            runtime.validate_smoke_json(json.dumps(value))

    def test_duplicate_or_diagnostic_only_json_does_not_fake_a_pixel_receipt(self):
        text = json.dumps(passing_json())
        for bad in (text.replace('"initialized": true', '"initialized": false, "initialized": true'),
                    '{"message":"looks successful"}', 'native startup succeeded\n' + text):
            with self.subTest(text=bad), self.assertRaises(ValueError):
                runtime.validate_smoke_json(bad)


if __name__ == '__main__':
    unittest.main()
