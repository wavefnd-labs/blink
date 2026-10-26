"""Exercise TLS recipe migration using temporary sources and native sentinels."""
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

SPEC = importlib.util.spec_from_file_location('engine_view_upgrade_tests_tool',
    Path(__file__).resolve().parents[1] / 'tools/engine_view_upgrade.py')
upgrade = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upgrade)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def key(data):
    return sha(json.dumps(data, sort_keys=True).encode())[:20]


class EngineViewUpgradeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='blink-tls-view-upgrade-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.configure()

    @staticmethod
    def write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def write_json(self, path, value):
        self.write(path, (json.dumps(value, indent=2) + '\n').encode())

    @staticmethod
    def snapshot(root):
        return {str(p.relative_to(root)): (sha(p.read_bytes()), p.stat().st_mtime_ns,
                    stat.S_IMODE(p.stat().st_mode)) for p in root.rglob('*') if p.is_file()}

    def configure(self, profile='release', cache=False, opus=False):
        self.base = self.root / 'base'
        self.work = self.root / (profile + str(cache) + str(opus))
        self.original = b'library_tls = false\n'
        self.adapted = b'library_tls = true\n'
        self.old_recipe = {'chromium_revision': 'a' * 40, 'files': {}, 'scope': 'old', 'patch': 'old'}
        self.recipe = copy.deepcopy(self.old_recipe)
        self.recipe.update(scope='reviewed TLS', patch='reviewed TLS', shared_library_tls={'approved': True})
        self.overlays = {}
        for name in upgrade.PATHS:
            source = self.base / name
            self.write(source, self.original)
            self.write(source.parent / 'LICENSE', b'Original notice\n')
            self.write(source.parent / 'subdir/input.h', b'Original source\n')
            self.write(source.parent / '.git', b'Fixture git metadata\n')
            self.overlays[name] = self.adapted
            self.recipe['files'][name] = {'sha256': sha(self.original), 'adapted_sha256': sha(self.adapted),
                'replacements': [{'old': self.original.decode(), 'new': self.adapted.decode()}]}
        self.policy = {'schema': 1, 'id': 'tls-library-v1', 'chromium_revision': 'a' * 40,
            'from_recipe_sha256': upgrade.digest(self.old_recipe), 'to_recipe_sha256': upgrade.digest(self.recipe),
            'overlay_paths': list(upgrade.PATHS)}
        gn = b'group("blink_engine") {}\n'
        self.inputs = {'revision': 'a' * 40, 'source_layout': str(self.base), 'layout_copies': {},
            'project_build_inputs': {'standalone/engine/BUILD.gn': sha(b'fixed')},
            'project_sources': {'engine': str(self.root / 'standalone/engine')}, 'gn': {'BUILD.gn': sha(gn)}}
        old_profile = {'name': 'dev', 'cache': cache, 'configuration': {'version': 'old'},
                       'args_gn': 'same args', 'overlays': {'compiler': sha(b'compiler')},
                       'compiler_cache': {'wrapper_sha256': sha(b'wrapper')} if cache else None}
        if profile == 'dev':
            current_profile = copy.deepcopy(old_profile)
            if opus:
                current_profile['configuration'] = {'version': 'Opus O1'}
                current_profile['overlays']['opus'] = sha(b'Opus O1')
            self.inputs['build_profile'] = current_profile
        old_key_inputs = {**self.inputs, 'recipe': self.old_recipe}
        self.before = key(old_key_inputs)
        self.physical_key = key({**old_key_inputs, 'build_profile': old_profile}) if opus else self.before
        self.view = self.work / 'views' / self.physical_key / 'src'
        self.view.mkdir(parents=True)
        for name in upgrade.PATHS:
            parent = self.view / Path(name).parent
            parent.parent.mkdir(parents=True, exist_ok=True)
            parent.symlink_to((self.base / name).parent, target_is_directory=True)
        self.write(self.view / 'standalone/gn/BUILD.gn', gn)
        self.manifest = {'schema': 1, 'chromium_revision': 'a' * 40, 'recipe': self.old_recipe,
            'key': self.physical_key, 'source_layout': str(self.base), 'layout_copies': {},
            'project_build_inputs': self.inputs['project_build_inputs'], 'project_sources': self.inputs['project_sources'],
            'owned_files': {'standalone/gn/BUILD.gn': sha(gn)}, 'split_sources': {}}
        if profile == 'dev':
            self.manifest['build_profile'] = copy.deepcopy(self.inputs['build_profile'])
        if opus:
            self.manifest['configuration_key'] = self.before
            self.manifest['profile_upgrade'] = {'schema': 1, 'from_configuration_key': self.physical_key,
                'to_configuration_key': self.before, 'from_profile': old_profile,
                'to_profile': copy.deepcopy(self.inputs['build_profile'])}
        self.manifest_path = self.view.parent / 'view.json'
        self.write_json(self.manifest_path, self.manifest)
        self.pointer = self.work / 'current.json'
        self.write_json(self.pointer, {'key': self.physical_key, 'view': str(self.view)})
        self.pointer_before = self.pointer.read_bytes()
        self.context = {'work_dir': self.work, 'current_path': self.pointer,
                        'profile': profile, 'cache': cache, 'isolated': profile == 'dev'}
        self.key_data = {**self.inputs, 'recipe': self.recipe}
        self.native = self.view / 'out/Engine'
        for name in ('.ninja_log', '.ninja_deps', 'obj/kept.o', 'libkept.so', 'gen/kept.pcm', 'gen/module.modulemap'):
            self.write(self.native / name, (name + ': keep bytes and metadata\n').encode())
        self.native_before = self.snapshot(self.native)
        self.base_before = self.snapshot(self.base)

    def check_files(self, view, manifest):
        self.assertEqual(view, self.view)
        for name, expected in manifest['owned_files'].items():
            path = view / name
            if path.is_symlink() or not path.is_file() or sha(path.read_bytes()) != expected:
                raise ValueError('Owned file changed: ' + name)

    def verify_previous(self, view, manifest):
        self.assertEqual(manifest, self.manifest)
        self.check_files(view, manifest)

    def verify_current(self, view, manifest):
        self.check_files(view, manifest)
        self.assertTrue(upgrade.verify_receipt(manifest, self.policy, self.key_data))

    def write_overlay(self, view, name, data, splits):
        self.assertEqual(view, self.view.parent / upgrade.STAGE / 'src')
        target = view / name
        self.assertTrue(target.is_symlink())
        target.unlink()
        target.write_bytes(data)

    def run_upgrade(self):
        return upgrade.select_view(self.context, self.key_data, key(self.key_data), self.policy, self.overlays,
            self.verify_previous, self.verify_current, self.write_overlay)

    def assert_preserved(self):
        self.assertEqual(self.snapshot(self.native), self.native_before)
        self.assertEqual(self.snapshot(self.base), self.base_before)
        self.assertEqual(self.pointer.read_bytes(), self.pointer_before)

    def test_release_preserves_physical_key_and_native_work(self):
        self.assertEqual(self.run_upgrade(), self.view)
        record = json.loads(self.manifest_path.read_text())
        self.assertEqual(record['key'], self.physical_key)
        self.assertEqual(record['configuration_key'], key(self.key_data))
        self.assertEqual(record[upgrade.RECEIPT]['from_configuration_key'], self.before)
        self.assertTrue(upgrade.verify_receipt(record, self.policy))
        for name in upgrade.PATHS:
            parent = self.view / Path(name).parent
            self.assertFalse(parent.is_symlink())
            self.assertFalse(os.path.lexists(parent / '.git'))
            self.assertEqual((parent / 'BUILD.gn').read_bytes(), self.adapted)
            self.assertEqual((parent / 'subdir').readlink(), (self.base / name).parent / 'subdir')
        self.assertFalse((self.view.parent / upgrade.JOURNAL).exists())
        self.assertFalse((self.view.parent / upgrade.STAGE).exists())
        self.assert_preserved()

    def test_dev_after_opus_keeps_both_receipts_and_three_continuous_keys(self):
        self.configure('dev', cache=True, opus=True)
        profile_receipt = copy.deepcopy(self.manifest['profile_upgrade'])
        self.assertNotEqual(self.physical_key, self.before)
        self.assertEqual(self.run_upgrade(), self.view)
        record = json.loads(self.manifest_path.read_text())
        self.assertEqual(record['profile_upgrade'], profile_receipt)
        self.assertEqual(record[upgrade.RECEIPT]['profile_upgrade'], profile_receipt)
        self.assertTrue(upgrade.verify_receipt(record, self.policy))
        self.assert_preserved()

    def test_pre_opus_profile_change_requires_a_fresh_view(self):
        self.configure('dev', cache=False)
        self.key_data['build_profile'] = copy.deepcopy(self.key_data['build_profile'])
        self.key_data['build_profile']['overlays']['opus'] = sha(b'Opus O1')
        self.assertIsNone(self.run_upgrade())
        self.assertEqual(json.loads(self.manifest_path.read_text()), self.manifest)
        self.assert_preserved()

    def test_dev_without_cache_after_opus_can_upgrade(self):
        self.configure('dev', cache=False, opus=True)
        self.assertEqual(self.run_upgrade(), self.view)
        self.assert_preserved()

    def test_unrelated_gn_or_cache_identity_change_requires_fresh_view(self):
        self.key_data['gn'] = {'BUILD.gn': sha(b'new GN')}
        self.assertIsNone(self.run_upgrade())
        self.configure('dev', cache=True, opus=True)
        self.key_data['build_profile'] = copy.deepcopy(self.key_data['build_profile'])
        self.key_data['build_profile']['compiler_cache']['wrapper_sha256'] = sha(b'new wrapper')
        self.assertIsNone(self.run_upgrade())
        self.assert_preserved()

    def test_pointer_wins_over_an_already_created_canonical_directory(self):
        alternative = self.work / 'views' / key(self.key_data) / 'src/user.txt'
        self.write(alternative, b'Preserve alternate candidate\n')
        self.assertEqual(self.run_upgrade(), self.view)
        self.assertEqual(alternative.read_bytes(), b'Preserve alternate candidate\n')
        self.assert_preserved()

    def test_completed_upgrade_is_idempotent_and_rejects_receipt_tampering(self):
        self.run_upgrade()
        before = self.snapshot(self.view.parent)
        self.assertEqual(self.run_upgrade(), self.view)
        self.assertEqual(self.snapshot(self.view.parent), before)
        record = json.loads(self.manifest_path.read_text())
        record[upgrade.RECEIPT]['key_inputs']['gn']['BUILD.gn'] = 'forged'
        self.write_json(self.manifest_path, record)
        with self.assertRaisesRegex(ValueError, 'receipt'):
            self.run_upgrade()
        self.assert_preserved()

    def test_dirty_existing_owned_or_original_source_is_preserved(self):
        self.write(self.view / 'standalone/gn/BUILD.gn', b'User edit\n')
        with self.assertRaisesRegex(ValueError, 'Owned file'):
            self.run_upgrade()
        self.assertEqual((self.view / 'standalone/gn/BUILD.gn').read_bytes(), b'User edit\n')
        self.assert_preserved()

    def test_changed_original_gn_or_wrong_overlay_is_rejected(self):
        self.overlays[upgrade.PATHS[0]] = b'Unreviewed changed TLS\n'
        with self.assertRaisesRegex(ValueError, 'reviewed recipe'):
            self.run_upgrade()
        self.assertFalse((self.view.parent / upgrade.JOURNAL).exists())
        self.assert_preserved()

    def test_locally_changed_pristine_gn_input_is_not_rehashed_or_repaired(self):
        source = self.base / upgrade.PATHS[0]
        self.write(source, b'User changed pristine GN input\n')
        self.base_before = self.snapshot(self.base)
        with self.assertRaisesRegex(ValueError, 'Original engine upgrade GN input'):
            self.run_upgrade()
        self.assertEqual(source.read_bytes(), b'User changed pristine GN input\n')
        self.assert_preserved()

    def test_completed_directory_source_alias_tampering_is_rejected(self):
        self.run_upgrade()
        alias = self.view / Path(upgrade.PATHS[0]).parent / 'LICENSE'
        alias.unlink()
        alias.symlink_to(self.root / 'other-notice')
        with self.assertRaisesRegex(ValueError, 'source alias'):
            self.run_upgrade()
        self.assertEqual(alias.readlink(), self.root / 'other-notice')
        self.assert_preserved()

    def test_unrecorded_local_gn_is_not_overwritten(self):
        parent = self.view / Path(upgrade.PATHS[0]).parent
        parent.unlink()
        self.write(parent / 'BUILD.gn', self.original)
        self.write(parent / 'manual.txt', b'User file\n')
        with self.assertRaisesRegex(ValueError, 'Unrecorded local'):
            self.run_upgrade()
        self.assertEqual((parent / 'manual.txt').read_bytes(), b'User file\n')
        self.assert_preserved()

    def test_atomic_leaf_overlay_in_an_already_split_directory(self):
        parent = self.view / Path(upgrade.PATHS[0]).parent
        source = parent.resolve()
        parent.unlink()
        parent.mkdir()
        for child in source.iterdir():
            if child.name != '.git':
                (parent / child.name).symlink_to(child, target_is_directory=child.is_dir())
        self.assertEqual(self.run_upgrade(), self.view)
        self.assertEqual((parent / 'BUILD.gn').read_bytes(), self.adapted)
        self.assert_preserved()

    def test_interruption_before_manifest_commit_resumes(self):
        original = upgrade.atomic_json
        def interrupted(path, value):
            if path == self.manifest_path:
                raise OSError('Stop before manifest commit')
            original(path, value)
        with mock.patch.object(upgrade, 'atomic_json', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.run_upgrade()
        self.assertEqual(json.loads(self.manifest_path.read_text()), self.manifest)
        self.assertEqual(self.run_upgrade(), self.view)
        self.assert_preserved()

    def test_interruption_between_original_alias_unlink_and_rename_resumes(self):
        original = Path.rename
        interrupted_path = self.view.parent / upgrade.STAGE / 'src/v8'
        def interrupted(path, destination):
            if path == interrupted_path:
                raise OSError('Stop in alias publication gap')
            return original(path, destination)
        with mock.patch.object(Path, 'rename', new=interrupted):
            with self.assertRaises(OSError):
                self.run_upgrade()
        self.assertFalse(os.path.lexists(self.view / 'v8'))
        self.assertEqual(self.run_upgrade(), self.view)
        self.assert_preserved()

    def test_interruption_after_manifest_commit_resumes_cleanup(self):
        with mock.patch.object(upgrade, 'finish', side_effect=OSError('Stop before cleanup')):
            with self.assertRaises(OSError):
                self.run_upgrade()
        self.assertEqual(self.run_upgrade(), self.view)
        self.assert_preserved()

    def test_partial_staging_write_keeps_original_alias_and_resumes(self):
        original = upgrade.tempfile.NamedTemporaryFile
        partial_files = []
        staged = self.view.parent / upgrade.STAGE / 'src' / upgrade.PATHS[0]
        test = self

        class InterruptedOutput:
            def __init__(self, stream):
                self.stream = stream

            def __getattr__(self, name):
                return getattr(self.stream, name)

            def __enter__(self):
                self.stream.__enter__()
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def write(self, data):
                if data == test.adapted:
                    self.stream.write(data[:3])
                    self.stream.flush()
                    temporary = Path(self.stream.name)
                    partial_files.append(temporary)
                    test.assertEqual(temporary.parent, test.view.parent)
                    test.assertEqual(temporary.read_bytes(), data[:3])
                    test.assertTrue(staged.is_symlink())
                    test.assertEqual(staged.read_bytes(), test.original)
                    raise OSError('Interrupted while writing temporary GN bytes')
                return self.stream.write(data)

        with mock.patch.object(upgrade.tempfile, 'NamedTemporaryFile',
                               side_effect=lambda *a, **kw: InterruptedOutput(original(*a, **kw))):
            with self.assertRaisesRegex(OSError, 'temporary GN bytes'):
                self.run_upgrade()
        self.assertEqual(len(partial_files), 1)
        self.assertFalse(partial_files[0].exists())
        self.assertTrue(staged.is_symlink())
        self.assertEqual(staged.read_bytes(), self.original)
        self.assertEqual(json.loads(self.manifest_path.read_text()), self.manifest)
        self.assert_preserved()
        self.assertEqual(self.run_upgrade(), self.view)
        self.assert_preserved()

    def test_staging_replace_interruption_keeps_original_alias_and_resumes(self):
        original = Path.replace
        staged = self.view.parent / upgrade.STAGE / 'src' / upgrade.PATHS[0]
        completed_files = []

        def interrupted(path, destination):
            if destination == staged:
                completed_files.append(path)
                self.assertEqual(path.parent, self.view.parent)
                self.assertEqual(path.read_bytes(), self.adapted)
                self.assertTrue(staged.is_symlink())
                self.assertEqual(staged.read_bytes(), self.original)
                raise OSError('Interrupted before atomic staged GN replacement')
            return original(path, destination)

        with mock.patch.object(Path, 'replace', new=interrupted):
            with self.assertRaisesRegex(OSError, 'atomic staged GN replacement'):
                self.run_upgrade()
        self.assertEqual(len(completed_files), 1)
        self.assertFalse(completed_files[0].exists())
        self.assertTrue(staged.is_symlink())
        self.assertEqual(staged.read_bytes(), self.original)
        self.assertEqual(json.loads(self.manifest_path.read_text()), self.manifest)
        self.assert_preserved()
        self.assertEqual(self.run_upgrade(), self.view)
        self.assert_preserved()

    def test_journal_and_staging_manual_changes_fail_closed(self):
        with mock.patch.object(upgrade, 'stage_and_publish', side_effect=OSError('Stop after journal')):
            with self.assertRaises(OSError):
                self.run_upgrade()
        journal = self.view.parent / upgrade.JOURNAL
        intent = json.loads(journal.read_text())
        intent['plans'][0]['link'] = str(self.root / 'foreign-source')
        self.write_json(journal, intent)
        with self.assertRaisesRegex(ValueError, 'different original alias'):
            self.run_upgrade()
        self.assert_preserved()

    def test_metadata_symlink_and_foreign_pointer_are_rejected(self):
        journal = self.view.parent / upgrade.JOURNAL
        journal.symlink_to(self.root / 'missing-user-file')
        with self.assertRaisesRegex(ValueError, 'local regular file'):
            self.run_upgrade()
        self.assertTrue(journal.is_symlink())
        journal.unlink()
        self.write_json(self.pointer, {'key': 'foreign', 'view': str(self.view)})
        with self.assertRaisesRegex(ValueError, 'physical view'):
            self.run_upgrade()
        self.write(self.pointer, self.pointer_before)
        self.assert_preserved()


if __name__ == '__main__':
    unittest.main()
