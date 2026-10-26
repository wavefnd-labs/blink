"""Guard pristine entry copies, view upgrades and retained build outputs."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1] / 'tools'


def module(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


layout, engine = module('generator_layout'), module('engine')


class GeneratorLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name) / 'base'
        self.view = Path(self.temporary.name) / 'view'
        self.name = 'third_party/dawn/generator/entry.py'
        self.source = self.base / self.name
        self.source.parent.mkdir(parents=True)
        self.data = b'print("unchanged generator")\n'
        self.source.write_bytes(self.data)
        (self.source.parent / 'helper.py').write_text('# original helper\n')
        self.view.mkdir()
        (self.view / 'third_party').symlink_to(self.base / 'third_party')
        self.output = self.view / 'out/Engine/obj/completed.o'
        self.output.parent.mkdir(parents=True)
        self.output.write_bytes(b'existing user object cache')
        self.output_stat = self.output.stat()
        self.manifest = {'owned_files': {}, 'split_sources': {}}
        self.recipe = {'schema': 1, 'chromium_revision': 'chromium-pin',
                       'dependency': 'third_party/dawn', 'repository': 'official-repository',
                       'dependency_revision': 'dawn-pin', 'files': {self.name: {
                           'mode': 'copy', 'original_mode': '100644',
                           'original_sha256': layout.digest(self.data),
                           'git_blob': hashlib.sha1(b'blob ' + str(len(self.data)).encode() + b'\0' + self.data).hexdigest()}}}
        self.dependencies = {'chromium_revision': 'chromium-pin', 'dependencies': {
            'third_party/dawn': {'dep_type': 'git', 'selected_for_profile': True,
                                 'repository': 'official-repository', 'revision': 'dawn-pin'}}}

    def inputs(self):
        return layout.configuration_inputs(self.base, 'chromium-pin',
                                            self.dependencies, self.recipe)

    def stage(self):
        return layout.stage(self.view, self.base, self.manifest, self.inputs(),
                            engine.write_overlay, set(), self.recipe)

    def test_upgrade_preserves_objects_originals_helpers_and_view_location(self):
        self.assertTrue(self.stage())
        target = self.view / self.name
        self.assertEqual(target.read_bytes(), self.data)
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.resolve(), target)
        self.assertEqual(self.source.read_bytes(), self.data)
        self.assertTrue((target.parent / 'helper.py').is_symlink())
        self.assertEqual(self.output.stat(), self.output_stat)
        self.assertEqual(self.manifest['owned_files'][self.name], layout.digest(self.data))

    def test_repeat_upgrade_is_read_only(self):
        self.stage()
        target = self.view / self.name
        before = target.stat()
        self.assertFalse(self.stage())
        self.assertEqual(target.stat(), before)
        self.assertEqual(self.output.stat(), self.output_stat)

    def test_source_drift_is_rejected_before_any_upgrade(self):
        self.source.write_bytes(b'local source modification')
        with self.assertRaisesRegex(ValueError, 'Pristine generator entry changed'):
            self.stage()
        self.assertTrue((self.view / 'third_party').is_symlink())

    def test_recorded_copy_drift_is_not_overwritten(self):
        self.stage()
        target = self.view / self.name
        target.write_text('local view modification')
        with self.assertRaisesRegex(ValueError, 'unchanged local copy'):
            self.stage()
        self.assertEqual(target.read_text(), 'local view modification')

    def test_symlinked_entry_or_parent_fails_verification(self):
        with self.assertRaisesRegex(ValueError, 'unchanged local copy'):
            layout.verify_view(self.view, self.inputs(), self.recipe)
        self.stage()
        target = self.view / self.name
        target.unlink()
        target.symlink_to(self.source)
        with self.assertRaisesRegex(ValueError, 'unchanged local copy'):
            layout.verify_view(self.view, self.inputs(), self.recipe)

    def test_dependency_or_revision_changes_require_review(self):
        self.dependencies['dependencies']['third_party/dawn']['revision'] = 'other-pin'
        with self.assertRaisesRegex(ValueError, 'dependency pin differ'):
            self.inputs()
        with self.assertRaisesRegex(ValueError, 'Chromium revision differ'):
            layout.configuration_inputs(self.base, 'other-chromium-pin', self.dependencies, self.recipe)

    def test_entry_mode_and_path_are_checked(self):
        self.source.chmod(0o755)
        with self.assertRaisesRegex(ValueError, 'Pristine generator entry changed'):
            self.inputs()
        entry = self.recipe['files'].pop(self.name)
        self.recipe['files']['third_party/dawn/generator/../outside.py'] = entry
        with self.assertRaisesRegex(ValueError, 'Invalid generator entry'):
            self.inputs()

    def test_only_the_explicit_tint_tools_wrapper_is_an_allowed_entry(self):
        wrapper = 'third_party/dawn/tools/generate-sources-gn.py'
        source = self.base / wrapper
        source.parent.mkdir(parents=True)
        source.write_bytes(self.data)
        self.recipe['files'][wrapper] = dict(self.recipe['files'][self.name])
        self.assertIn(wrapper, self.inputs())
        self.assertTrue(self.stage())
        self.assertFalse((self.view / wrapper).is_symlink())
        self.assertEqual((self.view / wrapper).read_bytes(), self.data)
        self.assertEqual(self.output.stat(), self.output_stat)
        self.recipe['files']['third_party/dawn/tools/unreviewed.py'] = self.recipe['files'].pop(wrapper)
        with self.assertRaisesRegex(ValueError, 'Invalid generator entry'):
            self.inputs()

    def test_unrecorded_local_file_is_not_overwritten(self):
        (self.view / 'third_party').unlink()
        target = self.view / self.name
        target.parent.mkdir(parents=True)
        target.write_bytes(self.data)
        with self.assertRaisesRegex(ValueError, 'Unexpected local generator entry'):
            self.stage()

    def interrupted_stage(self):
        second = 'third_party/dawn/generator/second.py'
        (self.base / second).write_bytes(self.data)
        self.recipe['files'][second] = dict(self.recipe['files'][self.name])
        saved = self.view.parent / 'view.json'
        saved.write_text(json.dumps(self.manifest))
        calls = []

        def write(*args):
            calls.append(args[1])
            if len(calls) == 2:
                raise OSError('injected failure between two overlay writes')
            engine.write_overlay(*args)

        with self.assertRaisesRegex(OSError, 'injected failure'):
            layout.stage(self.view, self.base, self.manifest, self.inputs(),
                         write, set(), self.recipe)
        self.assertEqual(json.loads(saved.read_text()),
                         {'owned_files': {}, 'split_sources': {}})
        self.assertTrue(layout.intent_path(self.view).is_file())
        self.assertEqual(self.output.stat(), self.output_stat)
        return second, saved

    def test_interrupted_upgrade_resumes_exact_copies_and_retains_objects(self):
        second, saved = self.interrupted_stage()
        target = self.view / self.name

        def retained_metadata():
            info = target.stat()
            # Hash verification reads the file and may legitimately update
            # atime. Retention must preserve identity and write metadata.
            return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                    info.st_size, info.st_mtime_ns, info.st_ctime_ns)

        first_stat = retained_metadata()
        manifest = json.loads(saved.read_text())
        writes = []

        def write(*args):
            writes.append(args[1])
            engine.write_overlay(*args)

        self.assertTrue(layout.stage(self.view, self.base, manifest, self.inputs(),
                                     write, set(), self.recipe))
        self.assertEqual(writes, [second])
        self.assertEqual(retained_metadata(), first_stat)
        self.assertEqual(self.output.stat(), self.output_stat)
        self.assertEqual(self.source.read_bytes(), self.data)
        self.assertEqual((self.base / second).read_bytes(), self.data)
        self.assertTrue((target.parent / 'helper.py').is_symlink())
        # The completed in-memory record alone cannot remove the journal.
        with self.assertRaisesRegex(ValueError, 'has not been saved'):
            layout.finish_stage(self.view, manifest, self.recipe)
        self.assertTrue(layout.intent_path(self.view).is_file())
        saved.write_text(json.dumps(manifest))
        self.assertTrue(layout.finish_stage(self.view, manifest, self.recipe))
        self.assertFalse(layout.intent_path(self.view).exists())
        self.assertFalse(layout.finish_stage(self.view, manifest, self.recipe))
        self.assertFalse(layout.stage(self.view, self.base, manifest, self.inputs(),
                                      engine.write_overlay, set(), self.recipe))
        self.assertEqual(retained_metadata(), first_stat)

    def test_saved_completed_upgrade_can_finish_after_caller_interruption(self):
        self.stage()
        saved = self.view.parent / 'view.json'
        saved.write_text(json.dumps(self.manifest))
        before = (self.view / self.name).stat()
        # The caller saved its manifest but was interrupted before finish_stage.
        self.assertFalse(self.stage())
        self.assertTrue(layout.finish_stage(self.view, self.manifest, self.recipe))
        self.assertEqual((self.view / self.name).stat(), before)
        self.assertEqual(self.output.stat(), self.output_stat)

    def test_interrupted_upgrade_does_not_adopt_unrelated_local_helpers(self):
        _, saved = self.interrupted_stage()
        helper = self.view / 'third_party/dawn/generator/helper.py'
        helper.unlink()
        helper.write_text('preserve unrelated local helper')
        with self.assertRaisesRegex(ValueError, 'upgrade alias changed'):
            layout.stage(self.view, self.base, json.loads(saved.read_text()),
                         self.inputs(), engine.write_overlay, set(), self.recipe)
        self.assertEqual(helper.read_text(), 'preserve unrelated local helper')
        self.assertEqual(self.output.stat(), self.output_stat)

    def test_changed_intent_manifest_or_intermediate_copy_fails_closed(self):
        _, saved = self.interrupted_stage()
        original = json.loads(saved.read_text())
        altered = {**original, 'unexpected': 'changed during upgrade'}
        with self.assertRaisesRegex(ValueError, 'upgrade manifest changed'):
            layout.stage(self.view, self.base, altered, self.inputs(),
                         engine.write_overlay, set(), self.recipe)
        target = self.view / self.name
        target.chmod(0o755)
        with self.assertRaisesRegex(ValueError, 'unchanged local copy'):
            layout.stage(self.view, self.base, original, self.inputs(),
                         engine.write_overlay, set(), self.recipe)
        target.chmod(0o644)
        target.write_text('preserve changed intermediate copy')
        with self.assertRaisesRegex(ValueError, 'unchanged local copy'):
            layout.stage(self.view, self.base, original, self.inputs(),
                         engine.write_overlay, set(), self.recipe)
        self.assertEqual(target.read_text(), 'preserve changed intermediate copy')

    def test_journal_recipe_source_and_view_identity_cannot_drift(self):
        _, saved = self.interrupted_stage()
        path = layout.intent_path(self.view)
        original = json.loads(path.read_text())
        for field, changed in (('recipe_sha256', 'other recipe'),
                               ('source_layout', str(self.view)),
                               ('view', str(self.base)),
                               ('original_manifest_sha256', 'other manifest')):
            with self.subTest(field=field):
                path.write_text(json.dumps({**original, field: changed}))
                with self.assertRaisesRegex(ValueError, 'intent differs'):
                    layout.stage(self.view, self.base, json.loads(saved.read_text()),
                                 self.inputs(), engine.write_overlay, set(), self.recipe)
        path.write_text(json.dumps(original))
        self.assertTrue(layout.stage(self.view, self.base, json.loads(saved.read_text()),
                                     self.inputs(), engine.write_overlay, set(), self.recipe))

    def test_stale_hydration_metadata_cannot_fetch_another_dependency(self):
        import json
        self.source.unlink()
        support = Path(self.temporary.name) / 'standalone'
        path = support / 'work/graph/declared-deps.json'
        path.parent.mkdir(parents=True)
        self.dependencies['root_deps_sha256'] = 'reviewed-deps'
        path.write_text(json.dumps({'chromium_revision': 'chromium-pin',
                                    'deps_sha256': 'reviewed-deps',
                                    'dependencies': {'src/third_party/dawn': {
                                        'repository': 'official-repository',
                                        'revision': 'unreviewed-pin'}}}))
        with mock.patch.object(engine, 'BASE', self.base), \
                mock.patch.object(engine, 'SUPPORT', support), \
                mock.patch.object(engine, 'generator_layout_module', return_value=layout), \
                mock.patch.object(layout, 'read_recipe', return_value=self.recipe), \
                mock.patch.object(engine.subprocess, 'run') as fetch:
            with self.assertRaisesRegex(ValueError, 'hydration metadata'):
                engine.generator_layout_inputs('chromium-pin', self.dependencies,
                                               hydrate_missing=True)
            fetch.assert_not_called()
