from contextlib import redirect_stdout
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


TOOLS = Path(__file__).resolve().parents[1] / 'tools'
SPEC = importlib.util.spec_from_file_location('implicit_source_tools', TOOLS / 'materialize_implicit_sources.py')
implicit = importlib.util.module_from_spec(SPEC)
with mock.patch.object(sys, 'path', [str(TOOLS), *sys.path]):
    SPEC.loader.exec_module(implicit)


class ImplicitSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.name = 'third_party/googletest/src'
        self.repository = 'https://chromium.googlesource.com/external/github.com/google/googletest.git'
        self.revision = 'a' * 40
        self.required = 'include/gtest/gtest_prod.h'
        self.forward = 'base/forward.h'
        self.data = ('#include "' + self.name + '/' + self.required + '"\n').encode()
        path = self.source / self.forward
        path.parent.mkdir()
        path.write_bytes(self.data)
        self.oid = implicit.materialize.blob_digest(path)
        self.entry = {
            'dep_type': 'git', 'repository': self.repository, 'revision': self.revision,
            'root_deps_declared': True,
            'original_gitlink': {'path': self.name, 'mode': '160000',
                                 'type': 'commit', 'oid': self.revision},
            'required_inputs': [self.required],
            'required_by_chain': [self.forward, self.name + '/' + self.required],
            'original_include_inputs': {self.forward: {
                'sha256': hashlib.sha256(self.data).hexdigest(), 'git_blob': self.oid}}}
        self.baseline = {'dependencies': {}}
        self.declared = {'dependencies': {'src/' + self.name: {
            'dep_type': 'git', 'repository': self.repository, 'revision': self.revision}}}
        self.tree = (b'160000 commit ' + self.revision.encode() + b'\t' + self.name.encode() + b'\0'
                     + b'100644 blob ' + self.oid.encode() + b'\t' + self.forward.encode() + b'\0')

    def validate(self):
        implicit.validate_selection(self.name, self.entry, self.baseline, self.declared)
        implicit.validate_provenance(self.name, self.entry, self.tree, self.source)

    def test_root_deps_gitlink_and_production_forwarding_header_agree(self):
        original = copy.deepcopy(self.baseline)
        self.validate()
        self.assertEqual(self.baseline, original)
        self.assertEqual(self.entry['required_inputs'], [self.required])

    def test_original_declared_pin_repository_and_gitlink_cannot_drift(self):
        self.entry['revision'] = 'b' * 40
        with self.assertRaisesRegex(ValueError, 'original official root DEPS pin'):
            self.validate()
        self.entry['revision'] = self.revision
        self.entry['repository'] = 'https://example.invalid/googletest'
        with self.assertRaisesRegex(ValueError, 'original official root DEPS pin'):
            self.validate()
        self.entry['repository'] = self.repository
        self.entry['original_gitlink']['oid'] = 'b' * 40
        with self.assertRaisesRegex(ValueError, 'original Chromium Gitlink'):
            self.validate()

    def test_selected_baseline_dependency_cannot_be_overridden(self):
        self.baseline['dependencies'][self.name] = {'revision': 'b' * 40}
        with self.assertRaisesRegex(ValueError, 'override the selected baseline'):
            self.validate()

    def test_conditional_dependency_needs_separate_reviewed_condition_selection(self):
        self.declared['dependencies']['src/' + self.name]['condition'] = 'testonly'
        with self.assertRaisesRegex(ValueError, 'reviewed Linux profile selection'):
            self.validate()

    def test_linux_condition_is_bound_to_original_expression_and_fixed_profile(self):
        expression = 'checkout_android or checkout_linux'
        self.declared['dependencies']['src/' + self.name]['condition'] = expression
        self.entry['condition_selection'] = {
            'expression': expression, 'profile': 'linux-x64-release-static',
            'variables': dict(implicit.PROFILE_CONDITIONS),
            'selected': True}
        self.validate()
        selection = self.entry['condition_selection']
        for field, value in (('expression', 'checkout_android'),
                             ('profile', 'android'), ('selected', False),
                             ('variables', {**implicit.PROFILE_CONDITIONS, 'checkout_android': True}),
                             ('variables', {**implicit.PROFILE_CONDITIONS, 'checkout_linux': 1})):
            with self.subTest(field=field, value=value):
                original = selection[field]
                selection[field] = value
                with self.assertRaisesRegex(ValueError, 'reviewed Linux profile selection'):
                    self.validate()
                selection[field] = original

    def test_false_unknown_or_executable_conditions_fail_closed(self):
        for expression, message in (
                ('checkout_android', 'condition is false'),
                ('checkout_other', 'Unsupported'),
                ('__import__("os").system("true")', 'Unsupported'),
                ('checkout_linux or', 'Invalid')):
            with self.subTest(expression=expression):
                self.declared['dependencies']['src/' + self.name]['condition'] = expression
                self.entry['condition_selection'] = {
                    'expression': expression, 'profile': 'linux-x64-release-static',
                    'variables': dict(implicit.PROFILE_CONDITIONS),
                    'selected': True}
                with self.assertRaisesRegex(ValueError, message):
                    self.validate()

    def test_original_forwarding_header_drift_is_rejected(self):
        (self.source / self.forward).write_text('#define FRIEND_TEST(a,b)\n')
        with self.assertRaisesRegex(ValueError, 'original Git blob'):
            self.validate()

    def test_reviewed_include_chain_cannot_be_invented_or_changed(self):
        self.entry['required_by_chain'][-1] = self.name + '/include/unrelated.h'
        with self.assertRaisesRegex(ValueError, 'reviewed production include chain'):
            self.validate()
        self.entry['required_by_chain'][-1] = self.name + '/' + self.required
        # Even a byte-preserved source cannot justify an include it does not use.
        data = b'#pragma once\n'
        path = self.source / self.forward
        path.write_bytes(data)
        oid = implicit.materialize.blob_digest(path)
        self.entry['original_include_inputs'][self.forward] = {
            'sha256': hashlib.sha256(data).hexdigest(), 'git_blob': oid}
        self.tree = self.tree.replace(self.oid.encode(), oid.encode())
        with self.assertRaisesRegex(ValueError, 'include edge is absent'):
            self.validate()

    def test_browser_paths_traversal_and_source_symlink_parents_fail_closed(self):
        for value in ('chrome/header.h', '../outside', '/absolute', 'a/../b', './a', 'a\\b'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'Unsafe implicit'):
                implicit.safe_relative(value)
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'forward.h').write_bytes(self.data)
        (self.source / self.forward).unlink()
        (self.source / 'base').rmdir()
        (self.source / 'base').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'parent is a symlink'):
            self.validate()

    def test_read_only_plan_does_not_download_or_publish_verification_success(self):
        lock = {'chromium_revision': 'b' * 40, 'dependencies': {self.name: self.entry}}
        output = io.StringIO()
        with mock.patch.object(implicit, 'load_selection', return_value=(lock, {}, {}, 'digest')):
            with mock.patch.object(implicit.materialize, 'dependency') as restore:
                with mock.patch.object(implicit.materialize, 'write_json') as write:
                    with redirect_stdout(output):
                        self.assertEqual(implicit.main(['--plan']), 0)
                    restore.assert_not_called()
                    write.assert_not_called()
        report = json.loads(output.getvalue())
        self.assertFalse(report['complete'])
        self.assertFalse(report['targets_added'])
        self.assertFalse(report['compiled'])

    def test_full_original_tree_verification_includes_notices_and_rejects_drift(self):
        graph = self.root / 'graph'
        cache = graph / 'deps-cache' / (self.name.replace('/', '_') + '.git')
        cache.mkdir(parents=True)
        subprocess.run(['git', 'init', '--quiet', str(cache)], check=True)
        (cache / 'include/gtest').mkdir(parents=True)
        (cache / self.required).write_text('#define FRIEND_TEST(a,b) friend class a##_##b\n')
        (cache / 'LICENSE').write_text('Original upstream license bytes\n')
        subprocess.run(['git', '-C', str(cache), 'add', '.'], check=True)
        revision = subprocess.check_output(['git', '-C', str(cache), 'write-tree'], text=True).strip()
        entry = {**self.entry, 'revision': revision}
        destination = graph / 'src' / self.name
        with mock.patch.object(implicit.materialize, 'WORK', graph):
            with mock.patch.object(implicit.materialize, 'SRC', graph / 'src'):
                entries = implicit.materialize.tree_entries(cache / '.git', revision)
                # The production cache uses a bare repository layout. The test
                # fixture exposes its object directory at the expected location.
                with mock.patch.object(implicit.materialize, 'tree_entries', return_value=entries):
                    with redirect_stdout(io.StringIO()):
                        implicit.materialize.materialize_tree(cache / '.git', revision, destination, entries, 'fixture')
                    result = implicit.verify_dependency(self.name, entry)
                    self.assertEqual(result['verified_file_count'], 2)
                    self.assertEqual((destination / 'LICENSE').read_text(), 'Original upstream license bytes\n')
                    self.assertIn(self.required, result['required_inputs'])
                    (destination / self.required).write_text('unreviewed replacement\n')
                    with self.assertRaisesRegex(ValueError, 'Existing upstream input differs'):
                        implicit.verify_dependency(self.name, entry)

    def main_header_fixture(self):
        name = 'chromeos/constants/chromeos_features.h'
        data = b'#include "base/feature_list.h"\n'
        oid = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        forward_data = ('#include "' + name + '"\n').encode()
        forward = self.source / self.forward
        forward.write_bytes(forward_data)
        forward_oid = implicit.materialize.blob_digest(forward)
        entry = {'original_mode': '100644', 'git_blob': oid,
                 'sha256': hashlib.sha256(data).hexdigest(),
                 'required_by_chain': [self.forward, name],
                 'original_include_inputs': {self.forward: {
                     'sha256': hashlib.sha256(forward_data).hexdigest(), 'git_blob': forward_oid}}}
        tree = (b'100644 blob ' + oid.encode() + b'\t' + name.encode() + b'\0'
                + b'100644 blob ' + forward_oid.encode() + b'\t' + self.forward.encode() + b'\0')
        return name, data, entry, tree

    def test_root_header_requires_exact_original_tree_and_production_include(self):
        name, data, entry, tree = self.main_header_fixture()
        selected = implicit.validate_main_header(name, entry, tree, self.source)
        self.assertEqual(selected, ('100644', entry['git_blob'], name))
        entry['git_blob'] = 'b' * 40
        with self.assertRaisesRegex(ValueError, 'pinned Git tree'):
            implicit.validate_main_header(name, entry, tree, self.source)
        entry['git_blob'] = selected[1]
        (self.source / self.forward).write_text('#pragma once\n')
        with self.assertRaisesRegex(ValueError, 'original Git blob'):
            implicit.validate_main_header(name, entry, tree, self.source)

    def test_root_header_cannot_import_blink_browser_implementations_or_invent_include_chain(self):
        name, data, entry, tree = self.main_header_fixture()
        for unsafe in ('chrome/features.h', 'third_party/blink/renderer/header.h', 'chromeos/feature.cc'):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                implicit.validate_main_header(unsafe, entry, tree, self.source)
        entry['required_by_chain'][-1] = 'chromeos/constants/unrelated.h'
        with self.assertRaisesRegex(ValueError, 'exact reviewed production include chain'):
            implicit.validate_main_header(name, entry, tree, self.source)

    def test_root_header_read_only_verification_rejects_drift_without_restoration(self):
        name, data, entry, tree = self.main_header_fixture()
        target = self.source / name
        target.parent.mkdir(parents=True)
        target.write_bytes(data)
        with mock.patch.object(implicit.materialize, 'git', return_value=tree), \
                mock.patch.object(implicit.materialize, 'materialize_tree') as restore:
            result = implicit.restore_main_headers({name: entry}, self.root / 'cache',
                                                    self.revision, self.source, verify=True)
            self.assertEqual(result[name]['sha256'], entry['sha256'])
            restore.assert_not_called()
            target.write_bytes(b'local edit must survive\n')
            with self.assertRaisesRegex(ValueError, 'Existing upstream input differs'):
                implicit.restore_main_headers({name: entry}, self.root / 'cache',
                                               self.revision, self.source, verify=True)
            self.assertEqual(target.read_bytes(), b'local edit must survive\n')
            restore.assert_not_called()

    def test_self_contained_pdf_constants_need_only_the_exact_root_header(self):
        old_name, _, entry, _ = self.main_header_fixture()
        name = 'pdf/pdf_accessibility_constants.h'
        data = b'namespace chrome_pdf { inline constexpr char kPDFStructureTypeDocument[] = "Document"; }\n'
        entry['git_blob'] = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        entry['sha256'] = hashlib.sha256(data).hexdigest()
        entry['required_by_chain'][-1] = name
        forward = self.source / self.forward
        forward.write_text('#include "' + name + '"\n')
        forward_data = forward.read_bytes()
        entry['original_include_inputs'][self.forward] = {
            'sha256': hashlib.sha256(forward_data).hexdigest(),
            'git_blob': implicit.materialize.blob_digest(forward)}
        tree = (b'100644 blob ' + entry['git_blob'].encode() + b'\t' + name.encode() + b'\0'
                + b'100644 blob ' + entry['original_include_inputs'][self.forward]['git_blob'].encode()
                + b'\t' + self.forward.encode() + b'\0')
        selected = implicit.validate_main_header(name, entry, tree, self.source)
        self.assertEqual(selected[2], name)
        self.assertFalse((self.source / 'pdf').exists())
        self.assertNotEqual(name, old_name)


if __name__ == '__main__':
    unittest.main()
