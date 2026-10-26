import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('blink_automation', Path(__file__).resolve().parents[1] / 'tools/automation.py')
automation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(automation)


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'standalone/config').mkdir(parents=True)
        (self.root / 'standalone/gn').mkdir()
        self.version = b'MAJOR=158\nMINOR=0\nBUILD=8114\nPATCH=0\n'
        (self.root / automation.VERSION).write_bytes(self.version)
        self.config = {'product': 'blink-engine', 'abi_version': 1, 'package_revision': 3,
                       'release_track': 'development', 'upstream_channel': 'stable',
                       'upstream_platform': 'linux', 'supported_profile': 'linux-x64'}
        self.write('release.json', self.config)
        self.write('upstream.lock.json', {'revision': 'a' * 40})
        self.write('engine-adaptations.json', {'chromium_revision': 'a' * 40,
                   'version_metadata': {'original_sha256': hashlib.sha256(self.version).hexdigest()}})
        self.state = {'git_commit': 'b' * 40, 'git_worktree_clean': True, 'locks': {'test': 'c' * 64}}
        patch = mock.patch.object(automation, 'snapshot', return_value=self.state)
        patch.start()
        self.addCleanup(patch.stop)

    def write(self, name, value):
        (self.root / 'standalone/config' / name).write_text(json.dumps(value))

    def evidence(self):
        path = self.root / 'standalone/out/blink-engine-sdk.tar.gz'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'example binary package')
        return {'snapshot': self.state, 'compiled': True, 'runtime_smoke_passed': True,
                'artifacts': [{'path': path.relative_to(self.root).as_posix(), 'sha256': automation.sha256(path)}]}

    def sdk_inputs(self):
        output = self.root / 'standalone/work/build'
        output.mkdir(parents=True)
        for name in ('libblink_embed.so', 'blink_embed_smoke'):
            (output / name).write_bytes(b'\x7fELFtest-' + name.encode())
        (output / 'blink_embed_smoke').chmod(0o755)
        for name in ('icudtl.dat', 'v8_context_snapshot.bin', 'blink_resources.pak'):
            (output / name).write_bytes(b'upstream resource')
        (self.root / 'standalone/include').mkdir()
        (self.root / 'standalone/include/blink_engine.h').write_text('/* embedding contract */\n')
        (self.root / 'standalone/engine').mkdir()
        (self.root / 'standalone/engine/NOTICE').write_text('original integration attribution\n')
        (self.root / 'standalone/runtime.md').write_text('Implemented embedding scope and missing capabilities\n')
        for name in automation.LOCKS:
            path = self.root / 'standalone/config' / name
            if not path.exists():
                path.write_text('{}\n')
        for name in ('LICENSE', 'LICENSE_FOR_ABOUT_CREDITS', 'README.md'):
            (self.root / name).write_text('original upstream ' + name)
        return output, {'icu_data': output / 'icudtl.dat', 'v8_snapshot': output / 'v8_context_snapshot.bin',
                        'blink_resources': output / 'blink_resources.pak'}

    def test_version_preserves_full_upstream_and_separate_package_revision(self):
        value = automation.identity(self.root)
        self.assertEqual(value['version'], '158.0.8114.0-engine.3')
        self.assertEqual(value['abi_version'], 1)
        self.assertEqual(value['release_track'], 'development')

    def test_version_rejects_mutated_upstream_metadata(self):
        (self.root / automation.VERSION).write_text('MAJOR=158\nMINOR=0\nBUILD=8114\nPATCH=99\n')
        with self.assertRaisesRegex(ValueError, 'reviewed upstream'):
            automation.identity(self.root)

    def test_version_rejects_duplicate_and_incomplete_fields(self):
        for value in ('MAJOR=1\nMAJOR=2', 'MAJOR=1\nMINOR=2\nBUILD=3'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                automation.parse_version(value)

    def discover(self, version='157.0.8000.1', source=None, commit='d' * 40):
        results = iter([{'versions': [{'version': version}]}, {'commit': commit}])
        body = source or b'MAJOR=157\nMINOR=0\nBUILD=8000\nPATCH=1\n'
        return automation.discover(self.root, lambda _: next(results), lambda _: base64.b64encode(body))

    def test_upstream_discovery_resolves_official_immutable_commit(self):
        value = self.discover()
        self.assertEqual(value['candidate']['revision'], 'd' * 40)
        self.assertFalse(value['source_update_performed'])
        self.assertFalse(value['same_version'])
        self.assertIn('refs/tags/157.0.8000.1', value['sources']['chromium_tag'])
        self.assertIn('/' + 'd' * 40 + '/', value['sources']['version_file'])

    def test_upstream_does_not_confuse_main_with_stable(self):
        value = self.discover()
        self.assertEqual(value['channel'], 'stable')
        self.assertEqual(value['current']['release_track'], 'development')
        self.assertEqual(value['current']['upstream_version'], '158.0.8114.0')

    def test_upstream_rejects_tag_version_mismatch(self):
        with self.assertRaisesRegex(ValueError, 'disagrees'):
            self.discover(source=self.version)

    def test_upstream_rejects_mutable_commit_identity(self):
        with self.assertRaisesRegex(ValueError, 'commit'):
            self.discover(commit='refs/heads/main')

    def test_missing_evidence_blocks_release(self):
        self.assertFalse(automation.release_plan(self.root)['publishable'])

    def test_proposal_manifest_cannot_be_mistaken_for_completed_source_update(self):
        self.write('upstream-update.json', {'status': 'awaiting-source-import'})
        report = automation.release_plan(self.root, self.evidence())
        self.assertFalse(report['publishable'])
        self.assertTrue(any('unresolved upstream proposal' in reason for reason in report['blockers']))

    def test_uncommitted_sources_cannot_be_published_as_head(self):
        self.state['git_worktree_clean'] = False
        self.assertFalse(automation.release_plan(self.root, self.evidence())['publishable'])

    def test_dry_run_cannot_be_release_evidence(self):
        evidence = self.evidence()
        evidence['compiled'] = False
        self.assertFalse(automation.release_plan(self.root, evidence)['publishable'])

    def test_low_optimization_build_cannot_be_published_as_release(self):
        evidence = {**self.evidence(), 'build_profile': 'dev'}
        report = automation.release_plan(self.root, evidence)
        self.assertFalse(report['publishable'])
        self.assertTrue(any('Development profile' in reason for reason in report['blockers']))

    def test_changed_locks_invalidate_successful_build(self):
        evidence = self.evidence()
        evidence['snapshot'] = {'git_commit': 'e' * 40}
        self.assertFalse(automation.release_plan(self.root, evidence)['publishable'])

    def test_artifact_changed_since_validation_blocks_release(self):
        evidence = self.evidence()
        (self.root / evidence['artifacts'][0]['path']).write_bytes(b'changed')
        self.assertFalse(automation.release_plan(self.root, evidence)['publishable'])

    def test_artifacts_must_not_escape_repository(self):
        evidence = self.evidence()
        evidence['artifacts'][0]['path'] = '../outside'
        self.assertFalse(automation.release_plan(self.root, evidence)['publishable'])

    def test_validated_development_manifest_can_be_ready(self):
        report = automation.release_plan(self.root, self.evidence())
        self.assertTrue(report['publishable'])
        self.assertFalse(report['published'])

    def test_stable_requires_matching_official_candidate(self):
        self.config['release_track'] = 'stable'
        self.write('release.json', self.config)
        evidence = self.evidence()
        self.assertFalse(automation.release_plan(self.root, evidence)['publishable'])
        candidate = {'channel': 'stable', 'candidate': {'revision': 'a' * 40, 'version': '158.0.8114.0'}}
        self.assertTrue(automation.release_plan(self.root, evidence, candidate)['publishable'])

    def test_sdk_stages_explicit_resources_header_and_original_notice(self):
        output, resources = self.sdk_inputs()
        records = automation.stage_sdk(output, resources=resources, root=self.root)
        names = [row['path'] for row in records]
        self.assertTrue(any(name.endswith('/lib/libblink_embed.so') for name in names))
        self.assertTrue(any(name.endswith('/include/blink_engine.h') for name in names))
        self.assertTrue(any(name.endswith('/notices/blink/LICENSE') for name in names))
        manifest_path = next(self.root / name for name in names if name.endswith('/sdk-manifest.json'))
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest['resources']['v8_snapshot'], 'share/blink/v8_context_snapshot.bin')
        self.assertFalse(manifest['license_audit_completed'])

    def test_sdk_does_not_guess_snapshot_or_accept_missing_resources(self):
        output, resources = self.sdk_inputs()
        resources.pop('v8_snapshot')
        with self.assertRaisesRegex(ValueError, 'explicit'):
            automation.stage_sdk(output, resources=resources, root=self.root)

    def test_sdk_does_not_overwrite_changed_staged_files(self):
        output, resources = self.sdk_inputs()
        records = automation.stage_sdk(output, resources=resources, root=self.root)
        path = next(self.root / row['path'] for row in records if row['path'].endswith('/lib/libblink_embed.so'))
        path.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'changed'):
            automation.stage_sdk(output, resources=resources, root=self.root)

    def test_sdk_does_not_write_outside_build_products(self):
        output, resources = self.sdk_inputs()
        with self.assertRaisesRegex(ValueError, 'inside standalone/out'):
            automation.stage_sdk(output, self.root / 'renderer', resources=resources, root=self.root)

    def sdk_evidence(self):
        output, resources = self.sdk_inputs()
        records = automation.stage_sdk(output, resources=resources, root=self.root)
        manifest = next(self.root / row['path'] for row in records if row['path'].endswith('/sdk-manifest.json'))
        return {'snapshot': self.state, 'compiled': True, 'runtime_smoke_passed': True,
                'sdk_staged': True, 'sdk_directory': manifest.parent.relative_to(self.root).as_posix(),
                'artifacts': records}

    def test_ci_artifact_selects_exactly_the_validated_sdk_directory(self):
        evidence = self.sdk_evidence()
        selected = automation.sdk_artifact_directory(evidence, self.root)
        self.assertEqual(selected, evidence['sdk_directory'])
        (self.root / 'standalone/out/sdk/older-cached-sdk').mkdir()
        self.assertEqual(automation.sdk_artifact_directory(evidence, self.root), selected)

    def test_low_optimization_build_cannot_select_release_sdk_artifacts(self):
        evidence = {**self.sdk_evidence(), 'build_profile': 'dev'}
        with self.assertRaisesRegex(ValueError, 'Development profile'):
            automation.sdk_artifact_directory(evidence, self.root)

    def test_ci_artifact_rejects_extra_files_and_stale_receipts(self):
        evidence = self.sdk_evidence()
        extra = self.root / evidence['sdk_directory'] / 'not-validated.txt'
        extra.write_text('not from the smoke run')
        with self.assertRaisesRegex(ValueError, 'inventory'):
            automation.sdk_artifact_directory(evidence, self.root)
        extra.unlink()
        evidence['snapshot'] = {'git_commit': 'stale'}
        with self.assertRaisesRegex(ValueError, 'matching'):
            automation.sdk_artifact_directory(evidence, self.root)

    def test_ci_artifact_cannot_select_all_cached_sdks_or_inject_outputs(self):
        evidence = self.sdk_evidence()
        for name in ('standalone/out/sdk', '/tmp/other', 'standalone/out/sdk/*',
                     'standalone/out/sdk/build\nsdk_directory=other'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                automation.sdk_artifact_directory({**evidence, 'sdk_directory': name}, self.root)

    def test_ci_archive_preserves_executable_mode_and_exact_inventory(self):
        evidence = self.sdk_evidence()
        result = automation.archive_sdk_artifact(evidence, self.root)
        self.assertEqual(automation.sha256(self.root / result['archive_path']), result['archive_sha256'])
        sdk_name = Path(evidence['sdk_directory']).name
        with tarfile.open(self.root / result['archive_path'], 'r:gz') as archive:
            self.assertTrue(archive.getmember(sdk_name + '/bin/blink_embed_smoke').mode & 0o111)
            expected = {sdk_name + '/' + Path(row['path']).relative_to(evidence['sdk_directory']).as_posix()
                        for row in evidence['artifacts']}
            self.assertEqual(set(archive.getnames()), expected)
        self.assertEqual(automation.archive_sdk_artifact(evidence, self.root), result)

    def test_sdk_preserves_chromium_root_library_notices(self):
        output, resources = self.sdk_inputs()
        source = self.root / 'standalone/work/graph/src'
        source.mkdir(parents=True)
        (source / 'LICENSE').write_bytes(b'original Chromium root license')
        (source / 'AUTHORS').write_bytes(b'original Chromium authors')
        upstream = self.root / 'standalone/upstream/chromium'
        upstream.mkdir(parents=True)
        (upstream / 'README.md').write_bytes(b'original Chromium README')
        records = automation.stage_sdk(output, resources=resources, root=self.root)
        by_name = {Path(row['path']).relative_to('standalone/out/sdk/158.0.8114.0-engine.3-linux-x64').as_posix(): self.root / row['path']
                   for row in records}
        self.assertEqual(by_name['notices/dependencies/AUTHORS'].read_bytes(), b'original Chromium authors')
        self.assertEqual(by_name['notices/chromium/LICENSE'].read_bytes(), b'original Chromium root license')
        self.assertEqual(by_name['notices/chromium/README.md'].read_bytes(), b'original Chromium README')


class SnapshotTests(unittest.TestCase):
    def test_generated_reports_do_not_dirty_sources_but_runtime_edits_do(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for folder in ('standalone/config', 'standalone/gn', 'standalone/engine', 'standalone/reports'):
                (root / folder).mkdir(parents=True, exist_ok=True)
            for name in automation.LOCKS:
                (root / 'standalone/config' / name).write_text('{}\n')
            (root / automation.VERSION).write_text('MAJOR=1\nMINOR=0\nBUILD=1\nPATCH=0\n')
            runtime = root / 'standalone/engine/runtime.cc'
            runtime.write_text('// version one\n')
            report = root / 'standalone/reports/build.json'
            report.write_text('{"compiled": false}\n')
            for arguments in (['init', '-q'], ['config', 'user.name', 'Test'], ['config', 'user.email', 'test@example.invalid'],
                              ['add', '.'], ['commit', '-qm', 'fixture']):
                subprocess.run(['git', '-C', str(root), *arguments], check=True)
            before = automation.snapshot(root)
            self.assertTrue(before['git_worktree_clean'])
            report.write_text('{"compiled": true}\n')
            after_report = automation.snapshot(root)
            self.assertTrue(after_report['git_worktree_clean'])
            self.assertEqual(before, after_report)
            runtime.write_text('// changed runtime\n')
            changed = automation.snapshot(root)
            self.assertFalse(changed['git_worktree_clean'])
            self.assertNotEqual(before['support_files'], changed['support_files'])


class RunnerPreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        config = self.root / 'standalone/config'
        config.mkdir(parents=True)
        (config / 'release.json').write_text('{"full_build_minimum_free_gib": 120}')
        (config / 'dependencies.lock.json').write_text('{"profile":{"script_executable":"/usr/bin/python3"}}')
        patches = [mock.patch.object(automation.sys, 'platform', 'linux'),
                   mock.patch.object(automation.platform, 'machine', return_value='x86_64'),
                   mock.patch.object(automation.shutil, 'disk_usage', return_value=SimpleNamespace(free=200 * 1024**3)),
                   mock.patch.object(automation.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name),
                   mock.patch.object(automation.subprocess, 'check_output', return_value='3 14\n')]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_ready_runner_checks_absolute_gn_python(self):
        result = automation.runner_preflight(self.root)
        self.assertTrue(result['passed'])
        self.assertEqual(result['gn_python'], '/usr/bin/python3')
        automation.subprocess.check_output.assert_called_once()

    def test_modern_path_python_cannot_hide_an_old_absolute_gn_python(self):
        automation.subprocess.check_output.return_value = '3 10\n'
        with self.assertRaisesRegex(ValueError, 'GN uses /usr/bin/python3'):
            automation.runner_preflight(self.root)

    def test_missing_host_tools_fail_before_source_downloads(self):
        automation.shutil.which.side_effect = lambda name: None if name == 'pkg-config' else '/usr/bin/' + name
        with self.assertRaisesRegex(ValueError, 'pkg-config'):
            automation.runner_preflight(self.root)
        automation.subprocess.check_output.assert_not_called()

    def test_runner_architecture_and_disk_are_required(self):
        automation.platform.machine.return_value = 'aarch64'
        with self.assertRaisesRegex(ValueError, 'x86_64'):
            automation.runner_preflight(self.root)
        automation.platform.machine.return_value = 'x86_64'
        automation.shutil.disk_usage.return_value = SimpleNamespace(free=50 * 1024**3)
        with self.assertRaisesRegex(ValueError, '120 GiB'):
            automation.runner_preflight(self.root)


if __name__ == '__main__':
    unittest.main()
