from pathlib import Path
import importlib.util
import json
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('engine_tools', Path(__file__).resolve().parents[1] / 'tools/engine.py')
engine = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(engine)


class EngineTests(unittest.TestCase):
    def test_upstream_drift_stops_adaptation(self):
        original = b'import("//content/features.gni")\n'
        adapted = b'import("//standalone/features.gni")\n'
        entry = {'sha256': engine.digest(original), 'adapted_sha256': engine.digest(adapted),
                 'replacements': [{'old': '//content/features.gni', 'new': '//standalone/features.gni'}]}
        self.assertEqual(engine.adapt_bytes(original, entry), adapted)
        with self.assertRaisesRegex(ValueError, 'Upstream'):
            engine.adapt_bytes(original + b'# upstream changed\n', entry)

    def test_duplicate_import_requires_review_instead_of_broad_rewrite(self):
        original = b'import("old.gni")\nimport("old.gni")\n'
        entry = {'sha256': engine.digest(original), 'adapted_sha256': 'unused',
                 'replacements': [{'old': 'old.gni', 'new': 'new.gni'}]}
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            engine.adapt_bytes(original, entry)

    def test_overlay_and_later_hydration_preserve_originals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, view = root / 'source', root / 'view'
            source.mkdir(); view.mkdir()
            (source / '.git').mkdir()
            (source / 'BUILD.gn').write_bytes(b'original')
            (source / 'existing.h').write_bytes(b'header')
            (view / 'shared').symlink_to(source, target_is_directory=True)
            split = {}
            engine.write_overlay(view, 'shared/BUILD.gn', b'adapted', set(), split)
            self.assertEqual((source / 'BUILD.gn').read_bytes(), b'original')
            self.assertEqual((view / 'shared/existing.h').read_bytes(), b'header')
            self.assertFalse((view / 'shared/.git').exists())
            (source / 'later.cc').write_bytes(b'newly hydrated source')
            manifest = {'owned_files': {'shared/BUILD.gn': engine.digest(b'adapted')}, 'split_sources': split}
            engine.refresh_sources(view, manifest, set())
            self.assertEqual((view / 'shared/later.cc').read_bytes(), b'newly hydrated source')
            self.assertEqual((view / 'shared/BUILD.gn').read_bytes(), b'adapted')

    def test_dangling_browser_alias_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            view = Path(directory)
            (view / 'content').symlink_to(view / 'not-present')
            with self.assertRaisesRegex(ValueError, 'Browser source'):
                engine.verify_view(view, {'owned_files': {}, 'recipe': {'files': {}}})

    def test_edited_adaptation_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            view = Path(directory)
            (view / 'BUILD.gn').write_bytes(b'modified')
            with self.assertRaisesRegex(ValueError, 'adaptation changed'):
                engine.verify_view(view, {'owned_files': {'BUILD.gn': engine.digest(b'expected')}, 'recipe': {'files': {}}})

    def test_parity_permits_source_relocation_but_rejects_feature_changes(self):
        view = Path('/tmp/engine-view')
        before = {'//library:core': {'type': 'source_set', 'defines': ['FEATURE=1'], 'sources': [str(engine.BASE) + '/core.cc'], 'outputs': ['//out/BlinkGraph/core.o']}}
        after = {'//library:core': {'type': 'source_set', 'defines': ['FEATURE=1'], 'sources': [str(view) + '/core.cc'], 'outputs': ['//out/Engine/core.o']}}
        self.assertTrue(engine.compare_graphs(before, after, view)['baseline_targets_unchanged'])
        after['//library:core']['defines'] = ['FEATURE=0']
        self.assertEqual(engine.compare_graphs(before, after, view)['changed_targets'], {'//library:core': ['defines']})

    def test_parity_rejects_a_missing_required_library(self):
        report = engine.compare_graphs({'//library:core': {'type': 'static_library'}}, {}, Path('/tmp/view'))
        self.assertFalse(report['baseline_targets_unchanged'])
        self.assertEqual(report['removed_targets'], ['//library:core'])

    def test_missing_browser_input_is_never_fetched_as_a_workaround(self):
        with self.assertRaisesRegex(ValueError, 'Refusing to restore browser'):
            engine.fetch_original_input('content/public/common/features.gni')

    def test_browser_files_are_rejected_even_in_allowed_library_actions(self):
        graph = {'//base:version': {'type': 'action', 'inputs': ['//chrome/VERSION'],
                                  'args': ['-f', '../../base/../chrome/VERSION'],
                                  'visibility': ['//content/*']}}
        found = engine.browser_file_references(graph, Path('/tmp/view'))
        self.assertEqual({item['field'] for item in found}, {'inputs', 'args'})

    def test_reviewed_metadata_paths_do_not_allow_unrelated_feature_changes(self):
        label = '//base:version'
        before = {label: {'inputs': ['//chrome/VERSION'], 'defines': ['FEATURE=1']}}
        after = {label: {'inputs': ['//standalone/gn/VERSION'], 'defines': ['FEATURE=1']}}
        approved = {label: {'inputs': [{'old': '//chrome/VERSION', 'new': '//standalone/gn/VERSION'}]}}
        report = engine.compare_graphs(before, after, Path('/tmp/view'), approved)
        self.assertTrue(report['matches_reviewed_adaptations'])
        self.assertFalse(report['baseline_targets_unchanged'])
        after[label]['defines'] = ['FEATURE=0']
        report = engine.compare_graphs(before, after, Path('/tmp/view'), approved)
        self.assertFalse(report['matches_reviewed_adaptations'])
        self.assertEqual(report['unreviewed_changes'], {label: ['defines']})

    def test_webui_cut_requires_exact_target_and_field_review(self):
        view = Path('/tmp/view')
        library, resource = '//ui/base:base', '//ui/webui/resources:resources_grit'
        before = {library: {'sources': ['native.cc', 'webui.cc'],
                            'deps': ['//native:core', resource], 'defines': ['KEEP=1']},
                  resource: {'type': 'action', 'script': '//browser_resource.py'}}
        after = {library: {'sources': ['native.cc'], 'deps': ['//native:core'],
                           'defines': ['KEEP=1']}}
        rewrites = {library: {'sources': {'remove': ['webui.cc']},
                              'deps': {'remove': [resource]}}}
        removals = {resource: engine.digest(json.dumps(before[resource], sort_keys=True).encode())}
        checked = engine.compare_graphs(before, after, view, rewrites, removals)
        self.assertTrue(checked['matches_reviewed_adaptations'])
        self.assertFalse(checked['baseline_targets_unchanged'])
        self.assertFalse(engine.compare_graphs(before, after, view, rewrites)['matches_reviewed_adaptations'])
        after[library]['defines'] = ['KEEP=0']
        self.assertFalse(engine.compare_graphs(before, after, view, rewrites, removals)['matches_reviewed_adaptations'])

    def test_missing_native_library_and_stale_removal_review_fail(self):
        native, resource = '//native:core', '//browser:resource'
        before = {native: {'type': 'static_library'}, resource: {'type': 'action'}}
        removals = {resource: engine.digest(json.dumps(before[resource], sort_keys=True).encode())}
        report = engine.compare_graphs(before, {}, Path('/tmp/view'), {}, removals)
        self.assertEqual(report['unreviewed_removed_targets'], [native])
        self.assertFalse(report['matches_reviewed_adaptations'])
        report = engine.compare_graphs(before, before, Path('/tmp/view'), {}, removals)
        self.assertEqual(report['stale_reviewed_removals'], [resource])
        self.assertFalse(report['matches_reviewed_adaptations'])
        altered = {**before, resource: {'type': 'action', 'args': ['changed upstream']}}
        report = engine.compare_graphs(altered, {native: before[native]}, Path('/tmp/view'), {}, removals)
        self.assertEqual(report['unreviewed_removed_targets'], [resource])

    def test_source_cut_cannot_add_or_silently_drop_other_native_sources(self):
        label = '//ui/base:base'
        before = {label: {'sources': ['native.cc', 'webui.cc']}}
        approved = {label: {'sources': {'remove': ['webui.cc']}}}
        for sources in ([], ['native.cc', 'extra.cc']):
            after = {label: {'sources': sources}}
            self.assertFalse(engine.compare_graphs(before, after, Path('/tmp/view'), approved)['matches_reviewed_adaptations'])
        with self.assertRaisesRegex(ValueError, 'no longer matches'):
            engine.compare_graphs(before, before, Path('/tmp/view'),
                                  {label: {'sources': {'remove': ['unknown.cc']}}})

    def test_webui_source_and_action_paths_are_rejected(self):
        graph = {'//library:core': {'type': 'action',
                                   'sources': ['//ui/webui/resources/browser.cc'],
                                   'args': ['--input=../../ui/webui/resources/browser.html'],
                                   'visibility': ['//ui/webui/resources/*']}}
        found = engine.browser_file_references(graph, Path('/tmp/view'))
        self.assertEqual({item['field'] for item in found}, {'sources', 'args'})

    def test_removed_browser_implementation_cannot_reenter_as_shared_source(self):
        policy = {'forbidden_source_files': ['services/shared/browser_service.cc'],
                  'forbidden_source_prefixes': ['components/profile/browser']}
        view = Path('/tmp/view')
        graph = {'//shared:api': {'sources': ['//services/shared/api_types.h',
                                              '//services/shared/browser_service.cc'],
                                  'args': ['--input=../../components/profile/browser/key.cc'],
                                  'inputs': [str(view) + '/components/profile/browser_extra/key.h']}}
        found = engine.browser_file_references(graph, view, policy)
        self.assertEqual({item['field'] for item in found}, {'sources', 'args'})
        self.assertFalse(engine.browser_source_path('services/shared/api_types.h', policy))

    def test_inherited_config_effects_require_complete_ordered_before_and_after(self):
        label = '//credential:types'
        before = {label: {'sources': ['credential.cc'], 'defines': ['KEEP=1', 'UNUSED_SKIA=1'],
                          'libs': ['native', 'browser', 'other']}}
        after = {label: {'sources': ['credential.cc'], 'defines': ['KEEP=1'],
                         'libs': ['other', 'native']}}
        effects = {label: {'defines': {'old': before[label]['defines'], 'new': after[label]['defines']},
                           'libs': {'old': before[label]['libs'], 'new': after[label]['libs']}}}
        self.assertTrue(engine.compare_graphs(before, after, Path('/tmp/view'),
                                             approved_config_effects=effects)['matches_reviewed_adaptations'])
        after[label]['sources'] = []
        self.assertFalse(engine.compare_graphs(before, after, Path('/tmp/view'),
                                              approved_config_effects=effects)['matches_reviewed_adaptations'])
        after[label]['libs'] = ['native', 'other']
        with self.assertRaisesRegex(ValueError, 'differs'):
            engine.compare_graphs(before, after, Path('/tmp/view'), approved_config_effects=effects)

    def test_config_effects_cannot_introduce_values_or_change_sources(self):
        label = '//credential:types'
        before = {label: {'defines': ['KEEP=1'], 'sources': ['keep.cc']}}
        for field, old, new in (('defines', ['KEEP=1'], ['KEEP=0']),
                                ('sources', ['keep.cc'], []),
                                ('defines', ['KEEP=1'], ['KEEP=1', 'KEEP=1'])):
            effects = {label: {field: {'old': old, 'new': new}}}
            with self.assertRaisesRegex(ValueError, 'only exact inherited'):
                engine.compare_graphs(before, before, Path('/tmp/view'), approved_config_effects=effects)

    def test_browser_source_alias_is_checked_by_canonical_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, view = root / 'cache', root / 'view'
            (source / 'chrome').mkdir(parents=True)
            (source / 'chrome/browser.cc').write_text('// browser implementation')
            view.mkdir()
            (view / 'shared-helper').symlink_to(source / 'chrome', target_is_directory=True)
            graph = {'//library:shared': {'sources': ['//shared-helper/browser.cc'],
                                          'args': ['--file=../../shared-helper/browser.cc']}}
            with mock.patch.object(engine, 'BASE', source):
                found = engine.browser_file_references(graph, view)
            self.assertEqual({item['field'] for item in found}, {'sources', 'args'})

    def test_prepare_can_only_extend_reviewed_generator_layout(self):
        copies = {'old.py': b'old', 'new.py': b'new'}
        layout = mock.Mock()
        manifest = {'generator_layout_copies': {'old.py': engine.digest(copies['old.py'])},
                    'chromium_revision': 'pinned', 'owned_files': {}, 'recipe': {'files': {}}}
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(engine, 'generator_layout_inputs', return_value=(layout, copies, {})), \
                mock.patch.object(engine, 'read_json', return_value={}):
            view = Path(directory)
            with self.assertRaisesRegex(ValueError, 'layout differs'):
                engine.verify_view(view, manifest)
            engine.verify_view(view, manifest, allow_layout_upgrade=True)
            layout.verify_view.assert_called_once_with(view, {'old.py': b'old'}, recipe={})
            manifest['generator_layout_copies']['old.py'] = 'unexpected hash'
            with self.assertRaisesRegex(ValueError, 'layout differs'):
                engine.verify_view(view, manifest, allow_layout_upgrade=True)

    def test_build_children_keep_python_cache_outside_pristine_sources(self):
        env = engine.build_environment()
        self.assertEqual(env['PYTHONDONTWRITEBYTECODE'], '1')
        self.assertEqual(Path(env['PYTHONPYCACHEPREFIX']), engine.WORK / 'python-cache')
