from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import urllib.error


TOOLS = Path(__file__).resolve().parents[1] / 'tools'
SPEC = importlib.util.spec_from_file_location('source_archive_tools', TOOLS / 'source_archive.py')
archive_tools = importlib.util.module_from_spec(SPEC)
with mock.patch.object(sys, 'path', [str(TOOLS), *sys.path]):
    SPEC.loader.exec_module(archive_tools)


class SourceArchiveTests(unittest.TestCase):
    repository = 'https://dawn.googlesource.com/dawn.git'
    revision = 'a' * 40

    def fixture(self):
        files = [('100644', 'config.json5', b'{upstream:true}\n'),
                 ('100755', 'nested/generator', b'#!/bin/sh\nexit 0\n'),
                 ('120000', 'alias', b'nested/generator')]
        entries = [(mode, subprocess.check_output(['git', 'hash-object', '--stdin'],
                    input=data).decode().strip(), name) for mode, name, data in files]
        return files, entries

    def write_tar(self, path, files):
        with tarfile.open(path, 'w:gz') as tar:
            for mode, name, data in files:
                member = tarfile.TarInfo(name)
                if mode == '120000':
                    member.type = tarfile.SYMTYPE
                    member.linkname = os.fsdecode(data)
                    tar.addfile(member)
                elif mode == 'hardlink':
                    member.type = tarfile.LNKTYPE
                    member.linkname = os.fsdecode(data)
                    tar.addfile(member)
                else:
                    member.mode = 0o755 if mode == '100755' else 0o644
                    member.size = len(data)
                    tar.addfile(member, io.BytesIO(data))

    def restore(self, root, files, entries):
        archive = root / 'official.tar.gz'
        self.write_tar(archive, files)
        with mock.patch.object(archive_tools, 'fetch_archive', return_value=(archive, 'official-url')):
            with redirect_stdout(io.StringIO()):
                return archive_tools.restore_archive(self.repository, self.revision, root / 'source',
                                                     entries, root / 'work', 'fixture')

    def test_real_git_blob_inventory_restores_exact_modes_links_and_is_idempotent(self):
        files, entries = self.fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.restore(root, files, entries)
            self.assertEqual(result['restored_files'], 3)
            self.assertEqual(result['fetched_blobs'], 0)
            self.assertEqual(result['transport'], 'official_gitiles_archive')
            self.assertEqual((root / 'source/config.json5').read_bytes(), files[0][2])
            self.assertTrue((root / 'source/nested/generator').stat().st_mode & 0o100)
            self.assertEqual(os.readlink(root / 'source/alias'), 'nested/generator')
            second = self.restore(root, files, entries)
            self.assertEqual(second['restored_files'], 0)
            self.assertEqual(second['verified_existing'], 3)
            self.assertEqual(second['inventory_sha256'], result['inventory_sha256'])

    def test_transformed_blob_rejects_complete_archive_before_original_writes(self):
        files, entries = self.fixture()
        files[1] = (files[1][0], files[1][1], b'export-subst changed these bytes\n')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'source').mkdir()
            existing = root / 'source/config.json5'
            existing.write_bytes(files[0][2])
            with self.assertRaisesRegex(ValueError, 'Git blob mismatch'):
                self.restore(root, files, entries)
            self.assertEqual(existing.read_bytes(), files[0][2])
            self.assertFalse((root / 'source/nested').exists())
            self.assertFalse((root / 'source/alias').exists())

    def test_missing_extra_duplicate_wrong_type_mode_and_traversal_fail_closed(self):
        files, entries = self.fixture()
        cases = [
            (files[:-1], 'missing from source archive'),
            (files + [('100644', 'extra.h', b'extra')], 'Extra file'),
            (files + [files[0]], 'Duplicate file'),
            ([files[0], ('100644', files[1][1], files[1][2]), files[2]], 'executable mode mismatch'),
            ([files[0], ('hardlink', files[1][1], b'config.json5'), files[2]], 'type mismatch'),
            (files + [('100644', '../escaped.h', b'escape')], 'Unsafe source archive path'),
            (files + [('100644', '/absolute.h', b'escape')], 'Unsafe source archive path'),
        ]
        for content, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with self.assertRaisesRegex(ValueError, message):
                    self.restore(root, content, entries)
                self.assertFalse((root / 'source').exists())

    def test_existing_drift_and_symlink_ancestor_are_rejected_before_download(self):
        _, entries = self.fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'source').mkdir()
            edited = root / 'source/config.json5'
            edited.write_bytes(b'local edit')
            with mock.patch.object(archive_tools, 'fetch_archive') as fetch:
                with self.assertRaisesRegex(ValueError, 'Existing upstream input differs'):
                    archive_tools.restore_archive(self.repository, self.revision, root / 'source',
                                                  entries, root / 'work', 'fixture')
                fetch.assert_not_called()
            outside = root / 'outside'
            outside.mkdir()
            (root / 'link').symlink_to(outside, target_is_directory=True)
            with mock.patch.object(archive_tools, 'fetch_archive') as fetch:
                with self.assertRaisesRegex(ValueError, 'ancestor is a symlink'):
                    archive_tools.restore_archive(self.repository, self.revision, root / 'link/source',
                                                  entries, root / 'work', 'fixture')
                fetch.assert_not_called()
            self.assertEqual(edited.read_bytes(), b'local edit')
            self.assertFalse((outside / 'source').exists())

    def test_official_immutable_archive_url_is_required(self):
        self.assertEqual(archive_tools.archive_url(self.repository, self.revision),
                         'https://dawn.googlesource.com/dawn/+archive/' + self.revision + '.tar.gz')
        for repository, revision in [('http://dawn.googlesource.com/dawn', self.revision),
                                     ('https://googlesource.com.example.com/dawn', self.revision),
                                     ('https://user:password@dawn.googlesource.com/dawn', self.revision),
                                     (self.repository, 'main')]:
            with self.subTest(repository=repository, revision=revision), self.assertRaises(ValueError):
                archive_tools.archive_url(repository, revision)

    def test_ignored_resume_range_restarts_partial_and_reuses_completed_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            cache = work / 'source-archives'
            cache.mkdir()
            key = hashlib.sha256(self.repository.encode()).hexdigest()[:20] + '-' + self.revision
            partial = cache / (key + '.tar.gz.partial')
            partial.write_bytes(b'incomplete old response')
            response = io.BytesIO(b'complete new archive')
            response.status, response.headers = 200, {'Content-Length': '20'}
            with mock.patch.object(archive_tools.urllib.request, 'urlopen', return_value=response) as request:
                with redirect_stdout(io.StringIO()):
                    path, _ = archive_tools.fetch_archive(self.repository, self.revision, work, 'fixture')
                self.assertTrue(request.call_args.args[0].has_header('Range'))
                self.assertEqual(path.read_bytes(), b'complete new archive')
                self.assertFalse(partial.exists())
                archive_tools.fetch_archive(self.repository, self.revision, work, 'fixture')
                self.assertEqual(request.call_count, 1)

    def test_resume_identity_mismatch_is_an_integrity_error_not_transport_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            response = io.BytesIO(b'body')
            response.status, response.headers = 206, {'Content-Range': 'bytes 100-103/104'}
            with mock.patch.object(archive_tools.urllib.request, 'urlopen', return_value=response):
                with self.assertRaisesRegex(ValueError, 'resume range mismatch'):
                    archive_tools.fetch_archive(self.repository, self.revision, Path(directory), 'fixture')

    def test_network_unavailability_has_a_distinct_explicit_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(archive_tools.urllib.request, 'urlopen', side_effect=urllib.error.URLError('offline')):
                with mock.patch.object(archive_tools.time, 'sleep'):
                    with self.assertRaises(archive_tools.ArchiveUnavailable):
                        archive_tools.fetch_archive(self.repository, self.revision, Path(directory), 'fixture')
