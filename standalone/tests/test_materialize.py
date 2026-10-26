from contextlib import redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    'materialization_tools', Path(__file__).resolve().parents[1] / 'tools/materialize.py')
materialize = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(materialize)


class MaterializationTests(unittest.TestCase):
    def test_symlink_git_hash_keeps_literal_dot_and_repeated_slash_text(self):
        with tempfile.TemporaryDirectory() as directory:
            link = Path(directory) / 'literal-link'
            text = b'./nested//not-yet-present'
            link.symlink_to(os.fsdecode(text))
            expected = subprocess.check_output(['git', 'hash-object', '--stdin'], input=text).decode().strip()
            self.assertEqual(materialize.blob_digest(link), expected)

    def source_fixture(self, root):
        repository = root / 'upstream'
        subprocess.run(['git', 'init', '--quiet', str(repository)], check=True)
        (repository / 'nested').mkdir()
        (repository / 'nested/resource_ids.spec').write_bytes(b'\x00upstream resource\n\xff')
        (repository / 'header.h').write_bytes(b'#pragma once\n')
        (repository / 'generator').write_bytes(b'#!/bin/sh\nexit 0\n')
        (repository / 'generator').chmod(0o755)
        (repository / 'alias').symlink_to('nested/resource_ids.spec')
        (repository / 'dangling').symlink_to('not-yet-generated')
        subprocess.run(['git', '-C', str(repository), 'add', '.'], check=True)
        revision = subprocess.check_output(
            ['git', '-C', str(repository), 'write-tree'], text=True).strip()
        cache = repository / '.git'
        return cache, revision, materialize.tree_entries(cache, revision)

    def restore(self, cache, revision, destination, entries):
        with redirect_stdout(io.StringIO()):
            return materialize.materialize_tree(cache, revision, destination, entries, 'fixture')

    def test_full_git_tree_preserves_exact_blobs_symlinks_modes_and_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache, revision, entries = self.source_fixture(root)
            destination = root / 'restored'
            first = self.restore(cache, revision, destination, entries)
            self.assertEqual(first['restored_files'], 5)
            self.assertEqual(first['fetched_blobs'], 0)
            self.assertEqual((destination / 'nested/resource_ids.spec').read_bytes(),
                             b'\x00upstream resource\n\xff')
            self.assertTrue((destination / 'generator').stat().st_mode & stat.S_IXUSR)
            self.assertFalse((destination / 'header.h').stat().st_mode & stat.S_IXUSR)
            self.assertEqual(os.readlink(destination / 'alias'), 'nested/resource_ids.spec')
            self.assertEqual(os.readlink(destination / 'dangling'), 'not-yet-generated')
            for mode, oid, name in entries:
                self.assertEqual(materialize.blob_digest(destination / name), oid)
            second = self.restore(cache, revision, destination, entries)
            self.assertEqual(second['restored_files'], 0)
            self.assertEqual(second['verified_existing'], 5)
            self.assertEqual(first['inventory_sha256'], second['inventory_sha256'])

    def test_drift_rejects_entire_restore_before_fetch_or_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache, revision, entries = self.source_fixture(root)
            destination = root / 'restored'
            destination.mkdir()
            edited = destination / 'header.h'
            edited.write_bytes(b'local edit which must be preserved\n')
            with mock.patch.object(materialize, 'fetch_blobs') as fetch:
                with self.assertRaisesRegex(ValueError, 'Existing upstream input differs'):
                    self.restore(cache, revision, destination, entries)
                fetch.assert_not_called()
            self.assertEqual(edited.read_bytes(), b'local edit which must be preserved\n')
            self.assertFalse((destination / 'generator').exists())

    def test_executable_mode_and_symlink_type_drift_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache, revision, entries = self.source_fixture(root)
            destination = root / 'restored'
            self.restore(cache, revision, destination, entries)
            (destination / 'generator').chmod(0o644)
            with self.assertRaisesRegex(ValueError, 'executable mode differs'):
                self.restore(cache, revision, destination, entries)
            (destination / 'generator').chmod(0o755)
            (destination / 'alias').unlink()
            (destination / 'alias').write_bytes(b'nested/resource_ids.spec')
            with self.assertRaisesRegex(ValueError, 'Existing upstream input differs'):
                self.restore(cache, revision, destination, entries)

    def test_symlink_destination_or_parent_cannot_receive_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache, revision, entries = self.source_fixture(root)
            outside = root / 'outside'
            outside.mkdir()
            alias = root / 'source-link'
            alias.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'destination is a symlink'):
                self.restore(cache, revision, alias, entries)
            destination = root / 'restored'
            destination.mkdir()
            (destination / 'nested').symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'parent is a symlink'):
                self.restore(cache, revision, destination, entries)
            self.assertFalse((outside / 'resource_ids.spec').exists())

    def test_selection_keeps_headers_and_resources_but_excludes_browser_and_blink(self):
        names = ['.rustfmt.toml', 'base/config.json5', 'base/include/private.h',
                 'tools/gritsettings/resource_ids.spec', 'third_party/abseil-cpp/a.h',
                 'third_party/blink/renderer/core.cc', 'chrome/VERSION', 'content/api.h',
                 'headless/main.cc', 'android_webview/build.cc', 'ios/foo.h',
                 'unused/subtree/file.cc']
        entries = [('100644', 'a' * 40, name) for name in names]
        selected = materialize.main_entries(entries, {'base', 'tools', 'third_party'})
        self.assertEqual([item[2] for item in selected], names[:5])

    def test_engine_graph_browser_inputs_stop_source_selection(self):
        graph = {'//base:version': {'inputs': ['//chrome/VERSION']}}
        with self.assertRaisesRegex(ValueError, 'browser source directories'):
            materialize.source_tops(graph)
        graph['//base:version']['inputs'] = ['//standalone/gn/VERSION', '//base/config.h']
        selected = materialize.source_tops(graph)
        self.assertIn('base', selected)
        self.assertNotIn('standalone', selected)

    def test_tree_reader_rejects_parent_traversal_and_does_not_recurse_gitlinks(self):
        malicious = b'100644 blob ' + b'a' * 40 + b'\t../escaped.h\0'
        with mock.patch.object(materialize, 'git', return_value=malicious):
            with self.assertRaisesRegex(ValueError, 'Unsafe upstream tree path'):
                materialize.tree_entries(Path('/unused'), 'a' * 40)
        rows = (b'160000 commit ' + b'b' * 40 + b'\tvendor\0'
                + b'100644 blob ' + b'a' * 40 + b'\treal.h\0')
        with mock.patch.object(materialize, 'git', return_value=rows):
            self.assertEqual(materialize.tree_entries(Path('/unused'), 'a' * 40),
                             [('100644', 'a' * 40, 'real.h')])

    def test_read_only_tree_inventory_disables_lazy_git_network_fetch(self):
        with mock.patch.object(materialize, 'git', return_value=b'') as git:
            self.assertEqual(materialize.tree_entries(Path('/unused'), 'a' * 40,
                                                      no_lazy_fetch=True), [])
            self.assertEqual(git.call_args.kwargs['env']['GIT_NO_LAZY_FETCH'], '1')

    def test_interrupted_atomic_copy_leaves_no_torn_original_or_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / 'verified-stage', root / 'original.h'
            source.write_bytes(b'complete verified upstream source')

            def interrupted_copy(original, output):
                output.write(original.read(4))
                raise OSError('simulated interruption')

            with mock.patch('shutil.copyfileobj', side_effect=interrupted_copy):
                with self.assertRaisesRegex(OSError, 'simulated interruption'):
                    materialize.install_file(target, '100644', source=source)
            self.assertFalse(target.exists())
            self.assertEqual(list(root.glob('.blink-source-*')), [])
            target.write_bytes(b'local source which must not be overwritten')
            with self.assertRaises(FileExistsError):
                materialize.install_file(target, '100644', data=b'upstream source')
            self.assertEqual(target.read_bytes(), b'local source which must not be overwritten')
            self.assertEqual(list(root.glob('.blink-source-*')), [])

    def test_atomic_publication_preserves_binary_data_and_git_executable_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = b'\x00exact upstream binary bytes\xff\n'
            executable = root / 'generator'
            materialize.install_file(executable, '100755', data=binary)
            self.assertEqual(executable.read_bytes(), binary)
            self.assertEqual(executable.stat().st_mode & 0o777, 0o755)
            source = root / 'stage'
            source.write_bytes(binary)
            ordinary = root / 'header'
            materialize.install_file(ordinary, '100644', source=source)
            self.assertEqual(ordinary.read_bytes(), binary)
            self.assertEqual(ordinary.stat().st_mode & 0o777, 0o644)
            self.assertEqual(list(root.glob('.blink-source-*')), [])
