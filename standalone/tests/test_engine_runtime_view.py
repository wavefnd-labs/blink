from pathlib import Path
import importlib.util
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('runtime_engine_tools', Path(__file__).resolve().parents[1] / 'tools/engine.py')
engine = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(engine)


class RuntimeViewTests(unittest.TestCase):
    def test_project_sources_are_explicit_links_not_copied_into_blink(self):
        with tempfile.TemporaryDirectory() as directory:
            support = Path(directory) / 'standalone'
            (support / 'engine').mkdir(parents=True)
            source = support / 'engine/engine.cc'
            source.write_text('original integration')
            view = Path(directory) / 'view'
            with mock.patch.object(engine, 'SUPPORT', support):
                engine.link_project_sources(view, {'engine': str(support / 'engine')})
                self.assertEqual((view / 'standalone/engine/engine.cc').read_text(), 'original integration')
                (view / 'standalone/engine').unlink()
                (view / 'standalone/engine').mkdir()
                with self.assertRaisesRegex(ValueError, 'link changed'):
                    engine.link_project_sources(view, {'engine': str(support / 'engine')})
            self.assertEqual(source.read_text(), 'original integration')

    def test_only_named_integration_directories_can_be_linked(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'Invalid engine integration'):
                engine.link_project_sources(Path(directory), {'content': directory})

    def test_visibility_review_allows_only_exact_sdk_addition(self):
        label = '//third_party/blink/renderer/core:core'
        old = ['//third_party/blink/*']
        new = old + ['//standalone/engine:blink_embed']
        before = {label: {'visibility': old, 'defines': ['UNCHANGED=1']}}
        after = {label: {'visibility': new, 'defines': ['UNCHANGED=1']}}
        approved = {label: {'visibility': {'old': old, 'new': new}}}
        self.assertTrue(engine.compare_graphs(before, after, Path('/tmp/view'), approved)['matches_reviewed_adaptations'])
        after[label]['defines'] = ['UNCHANGED=0']
        self.assertFalse(engine.compare_graphs(before, after, Path('/tmp/view'), approved)['matches_reviewed_adaptations'])
        approved[label]['visibility']['new'] = ['*']
        with self.assertRaisesRegex(ValueError, 'exact reviewed'):
            engine.compare_graphs(before, after, Path('/tmp/view'), approved)
