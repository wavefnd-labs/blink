"""Keep library TLS graph review narrower than a generic macro waiver."""
import copy
import importlib.util
from pathlib import Path
import unittest

TOOL = Path(__file__).resolve().parents[1] / 'tools/engine.py'
SPEC = importlib.util.spec_from_file_location('tls_graph_engine', TOOL)
engine = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(engine)


class SharedLibraryTlsGraphTests(unittest.TestCase):
    def setUp(self):
        self.blink = '//third_party/blink/renderer/platform/heap:blink_heap_buildflags'
        self.v8 = '//v8:v8_base_without_compiler'
        self.before = {
            self.blink: {'response_file_contents': [
                '--flags', 'BLINK_HEAP_INSIDE_SHARED_LIBRARY=false']},
            self.v8: {'configs': ['//v8:features'], 'defines': ['KEEP=1', 'LAST=1'],
                      'sources': ['//v8/src/heap/local-heap.cc']}}
        self.after = copy.deepcopy(self.before)
        self.after[self.blink]['response_file_contents'][1] = 'BLINK_HEAP_INSIDE_SHARED_LIBRARY=true'
        self.after[self.v8]['defines'].insert(1, 'V8_TLS_USED_IN_LIBRARY')
        self.policy = {
            'blink_buildflags': {'target': self.blink,
                                'old': self.before[self.blink]['response_file_contents'],
                                'new': self.after[self.blink]['response_file_contents']},
            'v8_defines': {self.v8: {'old': self.before[self.v8]['defines'],
                                   'new': self.after[self.v8]['defines']}}}

    def compare(self):
        return engine.compare_graphs(self.before, self.after, Path('/tmp/view'),
                                     approved_shared_library_tls=self.policy)

    def test_exact_library_modes_pass_without_waiving_other_changes(self):
        self.assertTrue(self.compare()['matches_reviewed_adaptations'])
        self.after[self.v8]['sources'].append('//v8/src/unreviewed.cc')
        self.assertEqual(self.compare()['unreviewed_changes'], {self.v8: ['sources']})

    def test_macro_review_requires_features_consumer_and_exact_order(self):
        self.after[self.v8]['defines'].append('UNREVIEWED=1')
        with self.assertRaisesRegex(ValueError, 'V8 internal'):
            self.compare()
        self.setUp()
        self.before[self.v8]['configs'] = []
        with self.assertRaisesRegex(ValueError, 'V8 internal'):
            self.compare()
        self.setUp()
        self.after[self.v8]['defines'] = list(reversed(self.after[self.v8]['defines']))
        self.policy['v8_defines'][self.v8]['new'] = self.after[self.v8]['defines']
        with self.assertRaisesRegex(ValueError, 'V8 internal'):
            self.compare()

    def test_buildflag_and_recorded_macro_cannot_drift(self):
        self.after[self.blink]['response_file_contents'][1] = 'BLINK_HEAP_INSIDE_SHARED_LIBRARY=false'
        with self.assertRaisesRegex(ValueError, 'heap flag'):
            self.compare()
        self.setUp()
        self.after[self.v8]['defines'] = self.before[self.v8]['defines'][:]
        with self.assertRaisesRegex(ValueError, 'TLS config differs'):
            self.compare()


if __name__ == '__main__':
    unittest.main()
