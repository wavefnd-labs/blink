from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


TOOLS = Path(__file__).resolve().parents[1] / 'tools'
SPEC = importlib.util.spec_from_file_location('history_archive', TOOLS / 'history_archive.py')
archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive)


class HistoryArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / 'repository'
        self.repository.mkdir()
        self.git('init', '--quiet')
        self.git('config', 'user.name', 'History fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('remote', 'add', 'official', 'https://github.com/chromium/chromium.git')
        self.git('config', 'remote.official.promisor', 'true')
        self.git('config', 'remote.official.partialclonefilter', 'blob:none')
        (self.repository / 'source.cc').write_text('pristine source\n')
        self.git('add', 'source.cc')
        self.git('commit', '--quiet', '-m', 'Original authors and source')
        (self.repository / 'source.cc').write_text('uncommitted local edit\n')
        self.inventory = self.root / 'missing.objects'
        self.report = self.root / 'work' / 'restoration.json'
        self.content = {}

    def git(self, *arguments, input=None):
        return subprocess.check_output(['git', '-C', str(self.repository), *arguments],
                                       input=input, env=archive.offline_environment())

    def blob(self, content, present=False):
        args = ['hash-object'] + (['-w'] if present else []) + ['--stdin']
        oid = self.git(*args, input=content).decode().strip()
        self.content[oid] = content
        return oid

    def state(self):
        return {name: (self.repository / '.git' / name).read_bytes() for name in ('config', 'index')} | {
            'refs': self.git('for-each-ref', '--format=%(refname) %(objectname)'),
            'head': (self.repository / '.git/HEAD').read_bytes(),
            'source': (self.repository / 'source.cc').read_bytes(),
            'status': self.git('status', '--porcelain=v1'),
        }

    def restore(self, ids=None, **kwargs):
        if ids is not None:
            self.inventory.write_text(''.join(oid + '\n' for oid in ids))
        with redirect_stdout(io.StringIO()):
            return archive.restore(self.repository, 'official', self.inventory, report=self.report,
                                   minimum_free_gib=0, **kwargs)

    def fetch(self, repository, remote, objects, log):
        self.assertEqual(repository, self.repository)
        self.assertEqual(remote, 'official')
        for oid in objects:
            actual = self.git('hash-object', '-w', '--stdin', input=self.content[oid]).decode().strip()
            self.assertEqual(actual, oid)
        log.write_text('Fixture object transfer completed\n')

    def test_resume_checks_real_objects_and_preserves_refs_index_configuration_and_edits(self):
        first, second = self.blob(b'first historical source'), self.blob(b'second historical source')
        present = self.blob(b'already restored source', present=True)
        before = self.state()
        calls = []

        def interrupted(repository, remote, objects, log):
            calls.append(list(objects))
            if second in objects:
                log.write_text('network failed after earlier batch\n')
                raise RuntimeError('Fixture transfer failed')
            self.fetch(repository, remote, objects, log)

        with mock.patch.object(archive, 'fetch_objects', side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, 'Fixture transfer failed'):
                self.restore([first, present, second], batch_size=2)
        failure = json.loads(self.report.read_text())
        self.assertEqual(failure['status'], 'failed')
        self.assertFalse(failure['complete'])
        self.assertEqual(failure['verified_entries'], 2)
        self.assertIn('network failed', Path(failure['last_batch']['log']).read_text())
        self.assertEqual(calls, [[first], [second]])
        with mock.patch.object(archive, 'fetch_objects', side_effect=self.fetch) as fetch:
            result = self.restore(batch_size=2)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.args[2], [second])
        self.assertTrue(result['complete'])
        self.assertEqual(result['verified_entries'], 3)
        self.assertEqual(self.state(), before)
        self.assertNotEqual(result['log_directory'], failure['log_directory'])
        self.assertTrue(Path(failure['last_batch']['log']).exists())

    def test_stale_and_duplicate_missing_entries_do_not_fetch_or_modify_git_state(self):
        present = self.blob(b'old audit was stale', present=True)
        before = self.state()
        with mock.patch.object(archive, 'fetch_objects') as fetch:
            result = self.restore([present, '?' + present, present], batch_size=2)
        fetch.assert_not_called()
        self.assertEqual(result['verified_entries'], 3)
        self.assertEqual(result['fetch_requested_objects'], 0)
        self.assertEqual(self.state(), before)

    def test_entire_inventory_is_validated_before_first_fetch(self):
        first = self.blob(b'not present')
        before = self.state()
        with mock.patch.object(archive, 'fetch_objects') as fetch:
            with self.assertRaisesRegex(ValueError, 'inventory line 2'):
                self.restore([first, 'main:source.cc'], batch_size=1)
        fetch.assert_not_called()
        self.assertEqual(archive.missing_objects(self.repository, [first]), [first])
        self.assertEqual(self.state(), before)

    def test_success_exit_with_absent_requested_objects_fails_and_retains_request(self):
        missing = self.blob(b'server did not deliver it')
        before = self.state()
        with mock.patch.object(archive, 'fetch_objects'):
            with self.assertRaisesRegex(RuntimeError, 'requested objects are absent'):
                self.restore([missing])
        result = json.loads(self.report.read_text())
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['complete'])
        self.assertEqual(result['verified_entries'], 0)
        self.assertEqual(Path(result['last_batch']['request_file']).read_text(), missing + '\n')
        self.assertEqual(self.state(), before)

    def test_low_disk_space_rejects_fetch_and_records_actionable_failure(self):
        missing = self.blob(b'requires more disk')
        usage = type('Usage', (), {'free': 0})()
        with mock.patch.object(archive.shutil, 'disk_usage', return_value=usage):
            with mock.patch.object(archive, 'fetch_objects') as fetch:
                with self.assertRaisesRegex(RuntimeError, 'Insufficient disk space'):
                    self.inventory.write_text(missing + '\n')
                    with redirect_stdout(io.StringIO()):
                        archive.restore(self.repository, 'official', self.inventory,
                                        report=self.report, minimum_free_gib=20)
        fetch.assert_not_called()
        self.assertEqual(json.loads(self.report.read_text())['status'], 'failed')

    def test_remote_must_be_configured_official_https_promisor(self):
        present = self.blob(b'present', present=True)
        self.inventory.write_text(present + '\n')
        invalid = ['file:///tmp/untrusted.git', 'https://github.com.example.org/chromium/chromium.git',
                   'https://github.com/other/chromium.git', 'http://github.com/chromium/chromium.git',
                   'https://user:secret@github.com/chromium/chromium.git']
        for url in invalid:
            with self.subTest(url=url):
                self.git('remote', 'set-url', 'official', url)
                with self.assertRaisesRegex(ValueError, 'official Chromium'):
                    self.restore()
        self.git('remote', 'set-url', 'official', 'https://chromium.googlesource.com/chromium/blink.git')
        self.git('config', 'remote.official.promisor', 'false')
        with self.assertRaisesRegex(ValueError, 'configured as promisor'):
            self.restore()
        self.git('config', 'remote.official.promisor', 'true')
        self.assertTrue(self.restore()['complete'])

    def test_fetch_invocation_has_only_targeted_ids_and_preserves_git_fetch_metadata(self):
        missing = self.blob(b'targeted object')
        before = self.state()
        log = self.root / 'fetch.log'
        with mock.patch.object(archive.subprocess, 'run', return_value=type('Result', (), {'returncode': 1})()) as run:
            with self.assertRaisesRegex(RuntimeError, 'see .*fetch.log'):
                archive.fetch_objects(self.repository, 'official', [missing], log)
        command = run.call_args.args[0]
        self.assertIn('--no-write-fetch-head', command)
        self.assertIn('--no-tags', command)
        self.assertIn('--no-prune', command)
        self.assertIn('--no-prune-tags', command)
        self.assertIn('--refmap=', command)
        self.assertIn('--recurse-submodules=no', command)
        self.assertIn('--filter=blob:none', command)
        self.assertIn('fetch.negotiationAlgorithm=noop', command)
        self.assertIn('maintenance.auto=false', command)
        self.assertEqual(run.call_args.kwargs['input'], (missing + '\n').encode())
        self.assertEqual(run.call_args.kwargs['env']['GIT_NO_LAZY_FETCH'], '1')
        self.assertEqual(self.state(), before)

    def test_report_cannot_overwrite_inventory_or_repository_metadata(self):
        present = self.blob(b'present', present=True)
        self.inventory.write_text(present + '\n')
        before = self.state()
        for report in (self.inventory, self.repository / '.git/config'):
            with self.subTest(report=report), self.assertRaisesRegex(ValueError, 'must not overwrite'):
                archive.restore(self.repository, 'official', self.inventory,
                                report=report, minimum_free_gib=0)
        self.assertEqual(self.state(), before)

    def test_offline_checks_never_inherit_enabled_lazy_fetch(self):
        missing = self.blob(b'not locally present')
        with mock.patch.dict(os.environ, {'GIT_NO_LAZY_FETCH': '0'}):
            self.assertEqual(archive.missing_objects(self.repository, [missing]), [missing])

    def test_linked_worktree_report_cannot_overwrite_shared_git_configuration(self):
        present = self.blob(b'present in shared object store', present=True)
        self.inventory.write_text(present + '\n')
        linked = self.root / 'linked'
        self.git('worktree', 'add', '--quiet', '--detach', str(linked), 'HEAD')
        before = self.state()
        with self.assertRaisesRegex(ValueError, 'must not overwrite'):
            archive.restore(linked, 'official', self.inventory,
                            report=self.repository / '.git/config', minimum_free_gib=0)
        self.assertEqual(self.state(), before)

    def test_empty_inventory_is_complete_and_invalid_limits_cannot_disable_disk_guard(self):
        with mock.patch.object(archive, 'fetch_objects') as fetch:
            self.assertTrue(self.restore([])['complete'])
        fetch.assert_not_called()
        for minimum in (float('nan'), float('inf'), -1):
            with self.subTest(minimum=minimum), self.assertRaises(ValueError):
                archive.restore(self.repository, 'official', self.inventory,
                                report=self.report, minimum_free_gib=minimum)

    def audit(self, refs=None):
        with redirect_stdout(io.StringIO()):
            return archive.audit(self.repository, refs or ['HEAD'], report=self.report)

    def test_offline_audit_pins_complete_history_and_preserves_repository(self):
        self.git('branch', 'legacy-fixture', 'HEAD')
        before = self.state()
        with mock.patch.object(archive, 'fetch_objects') as fetch:
            result = self.audit(['HEAD', 'legacy-fixture'])
        fetch.assert_not_called()
        self.assertTrue(result['complete'])
        self.assertTrue(result['publication_ready'])
        self.assertTrue(result['refs_unchanged'])
        self.assertEqual(result['missing_entries'], 0)
        self.assertEqual(result['pinned_refs']['HEAD'], self.git('rev-parse', 'HEAD').decode().strip())
        self.assertEqual(result['verified_present_entries'], result['present_entries'])
        self.assertEqual(result['present_entries'], 3)  # Original commit, tree and source blob.
        self.assertEqual(result['largest_reachable_blob']['size_bytes'], len(b'pristine source\n'))
        self.assertEqual(Path(result['missing_manifest']).read_text(), '')
        self.assertEqual(len(Path(result['type_size_manifest']).read_text().splitlines()), 3)
        self.assertEqual(self.state(), before)

    def test_audit_reports_missing_history_as_not_publishable_and_saves_restore_inventory(self):
        oid = self.git('rev-parse', 'HEAD:source.cc').decode().strip()
        (self.repository / '.git/objects' / oid[:2] / oid[2:]).unlink()
        with mock.patch.object(archive, 'fetch_objects') as fetch:
            result = self.audit()
        fetch.assert_not_called()
        self.assertTrue(result['complete'])
        self.assertFalse(result['publication_ready'])
        self.assertEqual(result['missing_entries'], 1)
        self.assertEqual(Path(result['missing_manifest']).read_text(), oid + '\n')
        self.assertEqual(result['verified_present_entries'], 2)

    def test_audit_gates_oversized_historical_blob_even_when_tip_is_small(self):
        original_blob = self.git('rev-parse', 'HEAD:source.cc').decode().strip()
        (self.repository / 'source.cc').write_text('x')
        self.git('add', 'source.cc')
        self.git('commit', '--quiet', '-m', 'Small current source, large old source remains')
        with mock.patch.object(archive, 'MAXIMUM_GITHUB_BLOB_BYTES', 8):
            result = self.audit()
        self.assertFalse(result['publication_ready'])
        self.assertEqual(result['oversized_blobs'], [{'oid': original_blob,
                                                      'size_bytes': len(b'pristine source\n')}])
        # GitHub rejects blobs larger than the limit; exactly at the limit is valid.
        with mock.patch.object(archive, 'MAXIMUM_GITHUB_BLOB_BYTES', len(b'pristine source\n')):
            self.assertTrue(self.audit()['publication_ready'])

    def test_audit_ref_movement_during_check_blocks_publication(self):
        original = archive.pin_refs
        tree = self.git('rev-parse', 'HEAD^{tree}').decode().strip()
        next_commit = self.git('commit-tree', tree, '-p', 'HEAD', input=b'Concurrent commit\n').decode().strip()
        branch = self.git('symbolic-ref', 'HEAD').decode().strip()
        calls = 0

        def moved(repository, refs):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.git('update-ref', branch, next_commit)
            return original(repository, refs)

        with mock.patch.object(archive, 'pin_refs', side_effect=moved):
            result = self.audit()
        self.assertFalse(result['refs_unchanged'])
        self.assertFalse(result['publication_ready'])
        self.assertNotEqual(result['pinned_refs'], result['refs_after_check'])

    def test_shallow_history_cannot_be_reported_complete(self):
        (self.repository / '.git/shallow').write_bytes(self.git('rev-parse', 'HEAD'))
        with self.assertRaisesRegex(ValueError, 'shallow repository'):
            self.audit()
        result = json.loads(self.report.read_text())
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['publication_ready'])
        self.assertFalse(result['complete'])

    def test_disappearing_object_during_size_check_fails_closed(self):
        real_run = archive.subprocess.run
        oid = self.git('rev-parse', 'HEAD:source.cc').decode().strip()

        def remove_before_size_check(command, **kwargs):
            if 'cat-file' in command:
                (self.repository / '.git/objects' / oid[:2] / oid[2:]).unlink()
            return real_run(command, **kwargs)

        with mock.patch.object(archive.subprocess, 'run', side_effect=remove_before_size_check):
            with self.assertRaisesRegex(ValueError, 'disappeared'):
                self.audit()
        result = json.loads(self.report.read_text())
        self.assertFalse(result['publication_ready'])
        self.assertEqual(result['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
