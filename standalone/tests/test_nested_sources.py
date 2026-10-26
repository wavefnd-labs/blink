import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


TOOLS = Path(__file__).resolve().parents[1] / 'tools'
SPEC = importlib.util.spec_from_file_location('nested_source_tools', TOOLS / 'materialize_nested_sources.py')
nested = importlib.util.module_from_spec(SPEC)
with mock.patch.object(sys, 'path', [str(TOOLS), *sys.path]):
    SPEC.loader.exec_module(nested)


class NestedSourceTests(unittest.TestCase):
    def fixture(self, root, declared_revision=None, module_url=None, module_condition=None,
                registry='OpenGL-Registry', required='xml/gl.xml'):
        parent_revision, revision = 'b' * 40, 'c' * 40
        parent_path = 'third_party/dawn'
        relative = 'third_party/' + registry + '/src'
        repository = 'https://chromium.googlesource.com/external/github.com/KhronosGroup/' + registry
        deps = ("vars = {}\ndeps = {'" + relative + "': '" + repository + '@'
                + (declared_revision or revision) + "'}\nhooks = [{'action': ['must-not-run']}]\n").encode()
        modules = ('[submodule "registry"]\npath = ' + relative + '\nurl = '
                   + (module_url or repository) + '\n'
                   + ('gclient-condition = ' + module_condition + '\n' if module_condition else '')).encode()
        evidence, rows = {}, []
        for original, data in [('DEPS', deps), ('.gitmodules', modules)]:
            name = 'preserved-' + original
            (root / name).write_bytes(data)
            oid = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
            evidence[original] = {'path': name, 'original_path': original,
                                  'sha256': hashlib.sha256(data).hexdigest(), 'git_blob': oid}
            rows.append(b'100644 blob ' + oid.encode() + b'\t' + original.encode() + b'\0')
        rows.append(b'160000 commit ' + revision.encode() + b'\t' + relative.encode() + b'\0')
        parent_repository = 'https://dawn.googlesource.com/dawn.git'
        root_lock = {'dependencies': {parent_path: {'dep_type': 'git', 'repository': parent_repository,
                                                   'revision': parent_revision}}}
        entry = {'repository': repository, 'revision': revision, 'root_deps_declared': False,
                 'parent': {'path': parent_path, 'repository': parent_repository,
                            'revision': parent_revision, 'preserved_inputs': evidence},
                 'original_gitlink': {'path': relative, 'mode': '160000', 'type': 'commit', 'oid': revision},
                 'required_inputs': [required]}
        return parent_path + '/' + relative, entry, root_lock, {'dependencies': {}}, b''.join(rows)

    def validate(self, root, fixture):
        return nested.validate_entry(*fixture, root=root)

    def test_exact_parent_deps_modules_gitlink_and_root_pin_must_agree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self.fixture(root)
            result = self.validate(root, fixture)
            self.assertEqual(result['revision'], 'c' * 40)
            self.assertEqual(result['repository'], fixture[1]['repository'])
            self.assertEqual(result['dep_type'], 'git')

    def test_header_only_egl_dependency_uses_the_same_parent_provenance_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self.fixture(root, registry='EGL-Registry', required='api/KHR/khrplatform.h')
            result = self.validate(root, fixture)
            self.assertTrue(result['repository'].endswith('/EGL-Registry'))
            self.assertEqual(fixture[1]['required_inputs'], ['api/KHR/khrplatform.h'])
            fixture[1]['original_gitlink']['oid'] = 'd' * 40
            with self.assertRaisesRegex(ValueError, 'original parent gitlink'):
                self.validate(root, fixture)

    def test_changed_parent_pin_or_gitlink_cannot_silently_change_nested_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self.fixture(root)
            fixture[2]['dependencies']['third_party/dawn']['revision'] = 'd' * 40
            with self.assertRaisesRegex(ValueError, 'root dependency pin'):
                self.validate(root, fixture)
            fixture = self.fixture(root)
            fixture[1]['revision'] = 'd' * 40
            with self.assertRaisesRegex(ValueError, 'original parent gitlink'):
                self.validate(root, fixture)

    def test_changed_preserved_deps_bytes_fail_even_if_declared_revision_looks_right(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self.fixture(root)
            (root / fixture[1]['parent']['preserved_inputs']['DEPS']['path']).write_text('local edit')
            with self.assertRaisesRegex(ValueError, 'original Git blob'):
                self.validate(root, fixture)

    def test_parent_gitlink_is_insufficient_if_original_deps_declares_another_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self.fixture(root, declared_revision='d' * 40)
            with self.assertRaisesRegex(ValueError, 'preserved parent DEPS'):
                self.validate(root, fixture)

    def test_module_url_or_conditional_standalone_dependency_requires_review(self):
        for kwargs in [{'module_url': 'https://example.invalid/unrelated'},
                       {'module_condition': 'dawn_standalone'}]:
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixture = self.fixture(root, **kwargs)
                with self.assertRaisesRegex(ValueError, 'unconditional original .gitmodules'):
                    self.validate(root, fixture)

    def test_nested_lock_cannot_override_root_deps_or_escape_its_parent_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self.fixture(root)
            fixture[3]['dependencies']['src/' + fixture[0]] = {'revision': 'd' * 40}
            with self.assertRaisesRegex(ValueError, 'override a root DEPS entry'):
                self.validate(root, fixture)
            fixture = self.fixture(root)
            bad = ('third_party/elsewhere', *fixture[1:])
            with self.assertRaisesRegex(ValueError, 'original parent path'):
                self.validate(root, bad)
            fixture[1]['required_inputs'] = ['../outside.xml']
            with self.assertRaisesRegex(ValueError, 'Unsafe nested dependency path'):
                self.validate(root, fixture)
