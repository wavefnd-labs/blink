"""Profile overlays and ordered graph deltas never weaken the release checks."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('build_profiles', ROOT / 'standalone/tools/build_profiles.py')
profiles = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(profiles)
FIXTURES = Path(__file__).resolve().parent / 'fixtures/upstream-build'


class BuildProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ('engine-profiles.json', 'dependencies.lock.json', 'upstream.lock.json'):
            path = self.root / 'standalone/config' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((ROOT / 'standalone/config' / name).read_bytes())
        self.base = self.root / 'pristine'
        self.relative = 'build/config/compiler/BUILD.gn'
        entries = profiles.load('dev', self.root)['overlays']
        self.original = self.pristine(self.relative, entries[self.relative])
        source = self.base / self.relative
        source.parent.mkdir(parents=True)
        source.write_bytes(self.original)
        opus = self.base / 'third_party/opus/BUILD.gn'
        opus.parent.mkdir(parents=True)
        opus.write_bytes(self.pristine('third_party/opus/BUILD.gn', entries['third_party/opus/BUILD.gn']))
        self.label = '//third_party/blink/renderer/core:core'
        self.release = {self.label: {
            'type': 'source_set', 'sources': ['/release/src/renderer/core.cc'],
            'deps': ['//base:base'], 'defines': ['NDEBUG'],
            'configs': ['//build/config/compiler:optimize_max',
                        '//build/config/compiler:default_symbols'],
            'cflags': ['-fstack-protector', '-O2', '-fdata-sections',
                       '-ffunction-sections', '-fno-unique-section-names', '-g0'],
            'ldflags': ['-Wl,--gc-sections'], 'rustflags': ['-Copt-level=3']}}
        self.dev = copy.deepcopy(self.release)
        node = self.dev[self.label]
        node['sources'] = ['/dev/src/renderer/core.cc']
        node['cflags'][1] = '-O0'
        node['rustflags'] = ['-Copt-level=0']
        node['configs'][1] = '//build/config/compiler:no_symbols'

    def pristine(self, relative, entry):
        # The support suite must run on a clean clone, without downloading the
        # ignored Chromium workspace. Fixtures retain original upstream bytes;
        # the production profile pins remain authoritative for their identity.
        path = FIXTURES / relative
        self.assertFalse(path.is_symlink())
        data = path.read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), entry['sha256'])
        blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        self.assertEqual(blob, entry['git_blob'])
        return data

    def compare(self):
        return profiles.compare_dev(self.release, self.dev, '/release/src', '/dev/src')

    def test_release_arguments_and_pristine_source_remain_verbatim(self):
        dependency = json.loads((self.root / 'standalone/config/dependencies.lock.json').read_text())
        self.assertEqual(profiles.args_text('release', self.root), dependency['profile']['args_gn'])
        self.assertEqual(profiles.overlays('release', self.root / 'absent', self.root), {})
        self.assertEqual((self.base / self.relative).read_bytes(), self.original)
        self.assertEqual(profiles.identity('release', self.root)['overlays'], {})

    def test_dev_exact_overlay_selects_o0_for_default_max_and_speed(self):
        result = profiles.overlays('dev', self.base, self.root)
        self.assertEqual(set(result), {self.relative, 'third_party/opus/BUILD.gn'})
        adapted = result[self.relative].decode()
        self.assertEqual(adapted.count('cflags = [ "-O0" ] + common_optimize_on_cflags'), 3)
        self.assertEqual(adapted.count('rustflags = [ "-Copt-level=0" ]'), 3)
        self.assertIn('config("default_optimization") {\n  configs = [ ":optimize" ]\n}', adapted)
        self.assertEqual((self.base / self.relative).read_bytes(), self.original)
        args = profiles.args_text('dev', self.root)
        self.assertIn('is_debug = false\n', args)
        for name in ('symbol_level', 'v8_symbol_level', 'blink_symbol_level'):
            self.assertIn(name + ' = 0\n', args)
        self.assertEqual(profiles.identity('dev', self.root)['overlays'][self.relative],
                         profiles.digest(result[self.relative]))

    def test_overlay_source_drift_requires_review(self):
        (self.base / self.relative).write_bytes(self.original + b'# changed\n')
        with self.assertRaisesRegex(ValueError, 'input changed'):
            profiles.overlays('dev', self.base, self.root)

    def test_baseline_revision_arguments_and_profile_name_are_checked(self):
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            profiles.load('../dev', self.root)
        path = self.root / 'standalone/config/dependencies.lock.json'
        value = json.loads(path.read_text())
        value['profile']['args_gn'] += 'is_debug = true\n'
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'release baseline'):
            profiles.load('dev', self.root)

    def test_strict_ordered_optimization_deltas_are_accepted(self):
        report = self.compare()
        self.assertTrue(report['passed'], report['unreviewed_changes'])
        self.assertEqual(set(report['changed_targets'][self.label]),
                         {'cflags', 'rustflags', 'configs'})
        self.assertFalse(report['added_targets'])
        self.assertFalse(report['removed_targets'])

    def test_v8_o3_to_o0_keeps_sections_and_linker_gc(self):
        self.release[self.label]['cflags'][1] = '-O3'
        self.assertTrue(self.compare()['passed'])
        self.dev[self.label]['ldflags'] = []
        self.assertIn('ldflags', self.compare()['unreviewed_changes'][self.label])

    def test_flags_macros_sources_dependencies_and_other_fields_are_not_waived(self):
        changes = {'cflags': ['-fno-stack-protector', '-O0'],
                   'defines': ['_DEBUG'], 'sources': ['//chrome/browser/entry.cc'],
                   'deps': ['//content:content'], 'metadata': {'unreviewed': True}}
        for field, value in changes.items():
            with self.subTest(field=field):
                original = copy.deepcopy(self.dev)
                self.dev[self.label][field] = value
                self.assertIn(field, self.compare()['unreviewed_changes'][self.label])
                self.dev = original

    def test_target_addition_removal_and_unconfigured_flag_change_fail(self):
        self.dev['//browser:new'] = {}
        self.assertFalse(self.compare()['passed'])
        del self.dev['//browser:new']
        self.release['//library:missing'] = {}
        self.assertFalse(self.compare()['passed'])
        del self.release['//library:missing']
        self.release[self.label]['configs'] = []
        self.dev[self.label]['configs'] = []
        self.assertIn('cflags', self.compare()['unreviewed_changes'][self.label])

    def test_symbol_override_is_only_allowed_for_blink_targets(self):
        self.release['//base:base'] = self.release.pop(self.label)
        self.dev['//base:base'] = self.dev.pop(self.label)
        self.assertIn('configs', self.compare()['unreviewed_changes']['//base:base'])

    def test_all_repeated_or_mixed_tokens_are_mapped_in_order(self):
        for tokens in (['-O2', '-O2'], ['-O2', '-O3'], ['-O3', '-O3']):
            with self.subTest(tokens=tokens):
                self.release[self.label]['cflags'] = [tokens[0], '-g0', tokens[1], '-fdata-sections']
                self.dev[self.label]['cflags'] = ['-O0', '-g0', '-O0', '-fdata-sections']
                self.release[self.label]['rustflags'] = ['-Copt-level=3', '--cfg', 'test', '-Copt-level=3']
                self.dev[self.label]['rustflags'] = ['-Copt-level=0', '--cfg', 'test', '-Copt-level=0']
                self.assertTrue(self.compare()['passed'])

    def test_partially_mapped_duplicate_optimization_tokens_fail(self):
        self.release[self.label]['cflags'].append('-O2')
        self.dev[self.label]['cflags'].append('-O2')
        self.assertIn('cflags', self.compare()['unreviewed_changes'][self.label])

    def test_extra_removed_reordered_or_unreviewed_flags_fail(self):
        expected = self.dev[self.label]['cflags'][:]
        for value in (expected + ['-O0'], expected[:-1],
                      ['-O0', '-fstack-protector'] + expected[2:],
                      expected[:1] + ['-Oz'] + expected[2:]):
            with self.subTest(value=value):
                self.dev[self.label]['cflags'] = value
                self.assertIn('cflags', self.compare()['unreviewed_changes'][self.label])
        self.dev[self.label]['cflags'] = expected
        self.dev[self.label]['rustflags'] += ['-Copt-level=0']
        self.assertIn('rustflags', self.compare()['unreviewed_changes'][self.label])

    def test_nested_zstd_config_is_limited_to_reviewed_consumers(self):
        for graph in (self.release, self.dev):
            graph['//third_party/zstd:common'] = graph.pop(self.label)
            graph['//third_party/zstd:common']['configs'] = ['//third_party/zstd:zstd_internal_config']
        self.release['//third_party/zstd:common']['cflags'][1] = '-O3'
        self.assertTrue(self.compare()['passed'])
        for graph in (self.release, self.dev):
            graph['//unreviewed:target'] = graph.pop('//third_party/zstd:common')
        report = self.compare()
        self.assertIn('cflags', report['unreviewed_changes']['//unreviewed:target'])
        self.assertIn('rustflags', report['unreviewed_changes']['//unreviewed:target'])

    def test_nested_optimization_source_drift_fails_with_base(self):
        entry = next(iter(profiles.OPTIMIZATION_ALIASES.values()))
        source = self.base / entry['source']
        source.parent.mkdir(parents=True)
        source.write_bytes(self.pristine(entry['source'], entry))
        self.assertTrue(profiles.compare_dev(self.release, self.dev, '/release/src',
                                             '/dev/src', self.base)['passed'])
        source.write_bytes(source.read_bytes() + b'# changed\n')
        with self.assertRaisesRegex(ValueError, 'nested optimization config changed'):
            profiles.compare_dev(self.release, self.dev, '/release/src', '/dev/src', self.base)

    def test_opus_o1_is_private_and_applied_after_the_shared_speed_setting(self):
        original = (self.base / 'third_party/opus/BUILD.gn').read_bytes()
        adapted = profiles.overlays('dev', self.base, self.root)['third_party/opus/BUILD.gn']
        self.assertIn(b'cflags = [ "-O1" ]', adapted)
        self.assertIn(b'configs -= [ ":opus_private_config" ]\n    configs += [ ":opus_private_config" ]', adapted)
        self.assertNotIn(b'-Wno-error', adapted)
        self.assertEqual((self.base / 'third_party/opus/BUILD.gn').read_bytes(), original)

    def test_opus_exception_cannot_relax_other_flags_or_other_libraries(self):
        before = {'configs': ['//build/config/compiler:optimize_speed', profiles.OPUS_PRIVATE_CONFIG],
                  'cflags': ['-Werror', '-O3', '-fdata-sections']}
        after = {**before, 'cflags': ['-Werror', '-O0', '-fdata-sections', '-O1']}
        compare = lambda label: profiles.compare_dev({label: before}, {label: after}, '/old', '/new')
        self.assertTrue(compare('//third_party/opus:opus')['passed'])
        self.assertFalse(compare('//base:base')['passed'])
        for flags in (['-Werror', '-O0', '-fdata-sections', '-O2'],
                      ['-O0', '-fdata-sections', '-O1'],
                      ['-Werror', '-O1', '-fdata-sections', '-O0']):
            after['cflags'] = flags
            self.assertFalse(compare('//third_party/opus:opus')['passed'])

    def test_reviewed_upgrade_preserves_args_cache_and_every_other_overlay(self):
        config = profiles.load('dev', self.root)
        old = config['incremental_upgrades'][0]['from_configuration']
        previous = {'name': 'dev', 'cache': True, 'configuration': old,
                    'overlays': old['overlays'], 'args_gn': 'same pinned arguments',
                    'compiler_cache': {'wrapper_sha256': 'unchanged'}}
        current = {**previous, 'configuration': profiles.identity('dev', self.root),
                   'overlays': profiles.identity('dev', self.root)['overlays']}
        self.assertTrue(profiles.reviewed_upgrade(previous, current, self.root))
        for name, value in (('cache', False), ('args_gn', 'changed'),
                            ('compiler_cache', {'wrapper_sha256': 'changed'})):
            changed = {**current, name: value}
            self.assertFalse(profiles.reviewed_upgrade(previous, changed, self.root))
        previous['configuration'] = {**old, 'args_sha256': 'unreviewed'}
        self.assertFalse(profiles.reviewed_upgrade(previous, current, self.root))


if __name__ == '__main__':
    unittest.main()
