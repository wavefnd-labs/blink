"""Only the pinned snapshot config correction can retain a TLS view's output."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('project_upgrade_test',
    Path(__file__).resolve().parents[1] / 'tools/project_build_upgrade.py')
upgrade = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upgrade)


class ProjectUpgradeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.policy = {'schema': 1, 'id': 'snapshot-startup-config-v1',
            'chromium_revision': 'pin', 'path': 'standalone/engine/BUILD.gn',
            'old_sha256': 'old', 'new_sha256': 'new'}
        self.manifest = {'chromium_revision': 'pin', 'recipe': {'files': {}},
            'source_layout': str(root / 'base'), 'layout_copies': {}, 'project_sources': {},
            'project_build_inputs': {'standalone/engine/BUILD.gn': 'old', 'other.gni': 'keep'},
            'owned_files': {'standalone/gn/BUILD.gn': 'keep'}, 'key': 'physical'}
        old = upgrade.key(upgrade.inputs(self.manifest))
        self.manifest.update(configuration_key=old, engine_view_upgrade={'to_configuration_key': old})
        self.live = upgrade.inputs(self.manifest)
        self.live['project_build_inputs'] = {**self.live['project_build_inputs'], 'standalone/engine/BUILD.gn': 'new'}
        self.work = root / 'work'
        self.view = self.work / 'views/physical/src'
        self.view.mkdir(parents=True)
        self.record = self.view.parent / 'view.json'
        self.record.write_text(json.dumps(self.manifest))
        self.pointer = self.work / 'current.json'
        self.pointer.write_text(json.dumps({'view': str(self.view), 'key': 'physical'}))
        self.native = self.view / 'out/Engine/kept.o'
        self.native.parent.mkdir(parents=True)
        self.native.write_bytes(b'Native output sentinel')
        self.before = self.native.stat()

    def run_upgrade(self):
        return upgrade.select_view({'current_path': self.pointer, 'work_dir': self.work},
            self.live, upgrade.key(self.live), self.policy,
            verify_previous=lambda v, m, old: self.assertEqual(m, self.manifest),
            verify_current=lambda v, m: upgrade.verify(m, self.policy, self.live))

    def test_upgrade_and_replay_keep_physical_output_and_prior_receipt(self):
        self.assertEqual(self.run_upgrade(), self.view)
        current = json.loads(self.record.read_text())
        self.assertEqual(current['engine_view_upgrade'], self.manifest['engine_view_upgrade'])
        self.assertEqual(current['project_build_inputs'], self.manifest['project_build_inputs'])
        self.assertEqual(self.run_upgrade(), self.view)
        after = self.native.stat()
        self.assertEqual((after.st_ino, after.st_mtime_ns, self.native.read_bytes()),
                         (self.before.st_ino, self.before.st_mtime_ns, b'Native output sentinel'))

    def test_unrelated_input_or_profile_change_uses_new_view(self):
        for field, value in [('project_build_inputs', {'different': 'unreviewed'}), ('build_profile', {'name': 'changed'})]:
            original = copy.deepcopy(self.live)
            self.live[field] = value
            self.assertIsNone(self.run_upgrade())
            self.assertEqual(json.loads(self.record.read_text()), self.manifest)
            self.live = original

    def test_recorded_key_chain_cannot_be_forged(self):
        self.run_upgrade()
        current = json.loads(self.record.read_text())
        current['project_build_upgrade']['from_configuration_key'] = 'forged'
        with self.assertRaisesRegex(ValueError, 'key chain'):
            upgrade.verify(current, self.policy)

    def test_symlink_pointer_is_rejected(self):
        target = self.pointer.with_suffix('.original')
        self.pointer.rename(target)
        self.pointer.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'regular file'):
            self.run_upgrade()

    def test_interrupted_manifest_publication_can_retry(self):
        original = Path.replace
        def stop(path, target):
            if target == self.record:
                raise OSError('Interrupted before manifest publication')
            return original(path, target)
        with mock.patch.object(Path, 'replace', new=stop):
            with self.assertRaisesRegex(OSError, 'Interrupted'):
                self.run_upgrade()
        self.assertEqual(json.loads(self.record.read_text()), self.manifest)
        self.assertEqual(self.run_upgrade(), self.view)
        self.assertFalse(list(self.view.parent.glob('.project-upgrade-*')))

    def test_build_policy_must_name_only_the_reviewed_file(self):
        self.policy['path'] = 'renderer/BUILD.gn'
        with self.assertRaisesRegex(ValueError, 'Unreviewed'):
            self.run_upgrade()

    def test_reviewed_second_stage_retains_the_original_baseline(self):
        self.run_upgrade()
        first = json.loads(self.record.read_text())
        self.policy['intermediate_sha256'] = ['new']
        self.policy['new_sha256'] = 'exports'
        self.live['project_build_inputs']['standalone/engine/BUILD.gn'] = 'exports'
        selected = upgrade.select_view({'current_path': self.pointer, 'work_dir': self.work},
            self.live, upgrade.key(self.live), self.policy,
            verify_previous=lambda v, m, old: upgrade.verify(m, self.policy),
            verify_current=lambda v, m: upgrade.verify(m, self.policy, self.live))
        self.assertEqual(selected, self.view)
        final = json.loads(self.record.read_text())
        self.assertEqual(final['project_build_inputs'], first['project_build_inputs'])
        self.assertEqual(final['engine_view_upgrade'], first['engine_view_upgrade'])
        self.assertEqual(final['project_build_upgrade']['from_configuration_key'],
                         first['project_build_upgrade']['from_configuration_key'])
        self.assertEqual(self.native.read_bytes(), b'Native output sentinel')

    def test_intermediate_stage_cannot_be_downgraded_or_changed_elsewhere(self):
        self.policy['intermediate_sha256'] = ['middle']
        before = {'standalone/engine/BUILD.gn': 'middle', 'other.gni': 'keep'}
        self.assertFalse(upgrade.reviewed(before, {**before, 'standalone/engine/BUILD.gn': 'old'}, self.policy))
        self.assertFalse(upgrade.reviewed(before, {'standalone/engine/BUILD.gn': 'new', 'other.gni': 'changed'}, self.policy))
        self.assertFalse(upgrade.reviewed(before, {**before, 'standalone/engine/BUILD.gn': 'unreviewed'}, self.policy))


if __name__ == '__main__':
    unittest.main()
