"""A reviewed profile repair keeps completed native work at its original path.

Fixtures contain sentinels for Ninja state, PCMs and native outputs. No real
source checkout, build view, compiler, GN or Ninja is used by these tests.
"""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location('profile_upgrade_tests_tool',
    Path(__file__).resolve().parents[1] / 'tools/profile_upgrade.py')
upgrade = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upgrade)
COMPILER = 'build/config/compiler/BUILD.gn'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def key(value):
    # Use the caller's canonical engine serialization, independently of helper.
    return digest(json.dumps(value, sort_keys=True).encode())[:20]


class ProfileUpgradeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='blink-profile-upgrade-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.base = self.root / 'base'
        self.work = self.root / 'profiles/dev-cache'
        self.source = self.base / upgrade.OPUS
        self.original = b'config("opus_private_config") { cflags = [ "-Werror" ] }\n'
        self.opus = self.original.replace(b'"-Werror"', b'"-Werror", "-O1"')
        self.compiler = b'# Reviewed development compiler settings\n'
        self.write(self.source / 'BUILD.gn', self.original)
        self.write(self.source / 'src/codec.c', b'/* Original codec */\n')
        self.write(self.source / 'LICENSE', b'Original license\n')
        self.write(self.source / '.git', b'gitdir: unused fixture\n')
        self.previous = {'name': 'dev', 'cache': True, 'args_gn': 'is_debug=false\n',
            'configuration': {'version': 'old reviewed config'},
            'overlays': {COMPILER: digest(self.compiler)},
            'compiler_cache': {'wrapper_sha256': 'a' * 64, 'cache': '/same/cache'}}
        self.expected = copy.deepcopy(self.previous)
        self.expected['configuration'] = {'version': 'reviewed Opus correction'}
        self.expected['overlays'][upgrade.OVERLAY] = digest(self.opus)
        self.key_data = {'revision': 'a' * 40, 'recipe': {'unchanged': True},
            'gn': {'BUILD.gn': 'same'}, 'source_layout': str(self.base),
            'build_profile': self.expected}
        self.old_key = key({**self.key_data, 'build_profile': self.previous})
        self.new_key = key(self.key_data)
        self.view = self.work / 'views' / self.old_key / 'src'
        (self.view / 'third_party').mkdir(parents=True)
        (self.view / upgrade.OPUS).symlink_to(self.source, target_is_directory=True)
        self.write(self.view / COMPILER, self.compiler)
        self.manifest = {'key': self.old_key, 'source_layout': str(self.base),
            'recipe': {'files': {}}, 'build_profile': copy.deepcopy(self.previous),
            'owned_files': {COMPILER: digest(self.compiler)}, 'split_sources': {}}
        self.manifest_path = self.view.parent / 'view.json'
        self.write_json(self.manifest_path, self.manifest)
        self.pointer = self.work / 'current.json'
        self.write_json(self.pointer, {'key': self.old_key, 'view': str(self.view)})
        self.context = {'profile': 'dev', 'cache': True, 'isolated': True,
            'current_path': self.pointer, 'work_dir': self.work,
            'overlays': {COMPILER: self.compiler, upgrade.OVERLAY: self.opus}}
        self.native = self.view / 'out/Engine'
        for name in ('.ninja_log', '.ninja_deps', 'obj/codec.o', 'gen/core.pcm', 'gen/core.modulemap'):
            self.write(self.native / name, (name + ': preserved original bytes\n').encode())
        self.native_before = self.snapshot(self.native)
        self.source_before = self.snapshot(self.base)
        self.pointer_before = self.pointer.read_bytes()

    @staticmethod
    def write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def write_json(self, path, value):
        self.write(path, (json.dumps(value, indent=2) + '\n').encode())

    @staticmethod
    def snapshot(root):
        return {str(p.relative_to(root)): (digest(p.read_bytes()), p.stat().st_mtime_ns,
                    stat.S_IMODE(p.stat().st_mode))
                for p in root.rglob('*') if p.is_file()}

    def verify(self, view, manifest, profile):
        self.assertEqual(view, self.view)
        self.assertEqual(manifest['build_profile'], profile)
        for name, sha in manifest['owned_files'].items():
            path = view / name
            if path.is_symlink() or not path.is_file() or digest(path.read_bytes()) != sha:
                raise ValueError('Owned bytes changed: ' + name)

    def run_upgrade(self, **kwargs):
        return upgrade.select_view(self.context, self.key_data, key(self.key_data),
            approve=kwargs.get('approve', lambda old, new: old == self.previous and new == self.expected),
            verify_previous=lambda view, manifest: self.verify(view, manifest, self.previous),
            verify_current=lambda view, manifest: self.verify(view, manifest, self.expected))

    def assert_preserved(self):
        self.assertEqual(self.snapshot(self.native), self.native_before)
        self.assertEqual(self.snapshot(self.base), self.source_before)
        self.assertEqual(self.pointer.read_bytes(), self.pointer_before)

    def test_preserves_physical_identity_ninja_modules_objects_and_upstream(self):
        self.assertEqual(self.run_upgrade(), self.view)
        manifest = json.loads(self.manifest_path.read_text())
        self.assertEqual(manifest['key'], self.old_key)
        self.assertEqual(manifest['configuration_key'], self.new_key)
        self.assertEqual(manifest['profile_upgrade']['from_profile'], self.previous)
        self.assertEqual(manifest['profile_upgrade']['to_profile'], self.expected)
        self.assertEqual(manifest['split_sources'][upgrade.OPUS], str(self.source))
        target = self.view / upgrade.OPUS
        self.assertFalse(target.is_symlink())
        self.assertEqual((target / 'BUILD.gn').read_bytes(), self.opus)
        self.assertEqual((target / 'src').readlink(), self.source / 'src')
        self.assertEqual((target / 'LICENSE').readlink(), self.source / 'LICENSE')
        self.assertFalse(os.path.lexists(target / '.git'))
        self.assertFalse((self.work / 'views' / self.new_key).exists())
        self.assert_preserved()

    def test_repeated_prepare_keeps_overlay_and_manifest_mtime(self):
        self.run_upgrade()
        before = self.snapshot(self.view.parent)
        self.assertEqual(self.run_upgrade(), self.view)
        self.assertEqual(self.snapshot(self.view.parent), before)
        self.assert_preserved()

    def test_unrelated_key_input_change_requires_a_fresh_view(self):
        self.key_data['gn']['BUILD.gn'] = 'new GN behavior'
        self.assertIsNone(self.run_upgrade())
        self.assertEqual(json.loads(self.manifest_path.read_text()), self.manifest)
        self.assertTrue((self.view / upgrade.OPUS).is_symlink())
        self.assert_preserved()

    def test_unapproved_cache_or_args_change_does_not_upgrade(self):
        for field, value in [('args_gn', 'changed args\n'), ('compiler_cache', {'cache': '/elsewhere'})]:
            with self.subTest(field=field):
                changed = copy.deepcopy(self.expected)
                changed[field] = value
                self.key_data['build_profile'] = changed
                self.assertIsNone(self.run_upgrade())
                self.assertTrue((self.view / upgrade.OPUS).is_symlink())
        self.assert_preserved()

    def test_dirty_owned_file_rejects_before_publication(self):
        self.write(self.view / COMPILER, b'local manual compiler edit\n')
        with self.assertRaisesRegex(ValueError, 'Owned bytes changed'):
            self.run_upgrade()
        self.assertTrue((self.view / upgrade.OPUS).is_symlink())
        self.assertFalse((self.view.parent / 'profile-upgrade-intent.json').exists())
        self.assert_preserved()

    def test_unrecorded_local_opus_is_preserved_even_if_bytes_match(self):
        target = self.view / upgrade.OPUS
        target.unlink()
        self.write(target / 'BUILD.gn', self.original)
        self.write(target / 'manual.txt', b'User-owned input\n')
        with self.assertRaisesRegex(ValueError, 'Unrecorded local Opus'):
            self.run_upgrade()
        self.assertEqual((target / 'manual.txt').read_bytes(), b'User-owned input\n')
        self.assert_preserved()

    def test_foreign_pointer_namespace_or_original_alias_rejects(self):
        self.write_json(self.pointer, {'key': 'wrong', 'view': str(self.view)})
        with self.assertRaisesRegex(ValueError, 'physical view'):
            self.run_upgrade()
        self.write(self.pointer, self.pointer_before)
        target = self.view / upgrade.OPUS
        target.unlink()
        target.symlink_to(self.root / 'other-opus')
        with self.assertRaisesRegex(ValueError, 'Unrecorded local Opus'):
            self.run_upgrade()
        self.assert_preserved()

    def test_inconsistent_canonical_key_rejects(self):
        with self.assertRaisesRegex(ValueError, 'configuration key'):
            upgrade.select_view(self.context, self.key_data, 'incorrect',
                approve=lambda *args: True, verify_previous=lambda *args: None,
                verify_current=lambda *args: None)
        self.assert_preserved()

    def test_manifest_commit_interruption_resumes_without_republishing_sources(self):
        original_write = upgrade.atomic_json
        def interrupted(path, value):
            if path == self.manifest_path:
                raise OSError('Simulated interruption before manifest commit')
            original_write(path, value)
        with mock.patch.object(upgrade, 'atomic_json', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.run_upgrade()
        self.assertEqual(json.loads(self.manifest_path.read_text()), self.manifest)
        self.assertFalse((self.view / upgrade.OPUS).is_symlink())
        self.assertEqual(self.run_upgrade(), self.view)
        self.assert_preserved()

    def test_alias_unlink_rename_gap_resumes_from_complete_staged_directory(self):
        original_rename = Path.rename
        stage = self.view.parent / 'opus-profile-upgrade-stage'
        def interrupted(path, target):
            if path == stage:
                raise OSError('Simulated interruption after unlink')
            return original_rename(path, target)
        with mock.patch.object(Path, 'rename', new=interrupted):
            with self.assertRaises(OSError):
                self.run_upgrade()
        self.assertFalse(os.path.lexists(self.view / upgrade.OPUS))
        self.assertEqual((stage / 'BUILD.gn').read_bytes(), self.opus)
        self.assertEqual(self.run_upgrade(), self.view)
        self.assert_preserved()

    def test_tampered_incomplete_overlay_is_not_overwritten(self):
        original_write = upgrade.atomic_json
        def interrupted(path, value):
            if path == self.manifest_path:
                raise OSError('Stop before manifest commit')
            original_write(path, value)
        with mock.patch.object(upgrade, 'atomic_json', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.run_upgrade()
        self.write(self.view / upgrade.OVERLAY, b'Local overlay change after interruption\n')
        with self.assertRaisesRegex(ValueError, 'reviewed bytes'):
            self.run_upgrade()
        self.assertEqual((self.view / upgrade.OVERLAY).read_bytes(), b'Local overlay change after interruption\n')
        self.assert_preserved()

    def test_completed_commit_journal_cleanup_resumes(self):
        original_unlink = Path.unlink
        journal = self.view.parent / 'profile-upgrade-intent.json'
        def interrupted(path, *args, **kwargs):
            if path == journal:
                raise OSError('Stop before journal cleanup')
            return original_unlink(path, *args, **kwargs)
        with mock.patch.object(Path, 'unlink', new=interrupted):
            with self.assertRaises(OSError):
                self.run_upgrade()
        self.assertTrue(journal.exists())
        self.assertEqual(self.run_upgrade(), self.view)
        self.assertFalse(journal.exists())
        self.assert_preserved()

    def test_completed_receipt_and_alias_tampering_reject(self):
        self.run_upgrade()
        manifest = json.loads(self.manifest_path.read_text())
        manifest['profile_upgrade']['from_configuration_key'] = 'forged old identity'
        self.write_json(self.manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, 'receipt changed'):
            self.run_upgrade()
        manifest['profile_upgrade']['from_configuration_key'] = self.old_key
        self.write_json(self.manifest_path, manifest)
        alias = self.view / upgrade.OPUS / 'src'
        alias.unlink()
        alias.symlink_to(self.root / 'other-codec')
        with self.assertRaisesRegex(ValueError, 'source alias changed'):
            self.run_upgrade()
        self.assert_preserved()

    def test_metadata_alias_is_not_overwritten(self):
        journal = self.view.parent / 'profile-upgrade-intent.json'
        journal.symlink_to(self.root / 'missing-user-file')
        with self.assertRaisesRegex(ValueError, 'local regular file'):
            self.run_upgrade()
        self.assertTrue(journal.is_symlink())
        self.assert_preserved()

    def test_release_and_nonisolated_contexts_do_not_upgrade(self):
        for change in ({'profile': 'release'}, {'isolated': False}):
            with self.subTest(change=change):
                self.context.update(change)
                self.assertIsNone(self.run_upgrade())
                self.context.update(profile='dev', isolated=True)
        self.assert_preserved()


if __name__ == '__main__':
    unittest.main()
