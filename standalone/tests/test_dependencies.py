from pathlib import Path
import importlib.util
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('dependency_tools', Path(__file__).resolve().parents[1] / 'tools/dependencies.py')
deps = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deps)


class DepsTests(unittest.TestCase):
    def test_resolves_pins_and_retains_conditions_without_executing_hooks(self):
        text = """
vars = {'host': 'https://example.invalid', 'rev': Str('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')}
deps = {'src/library': {'url': Var('host') + '/library.git@' + Var('rev'), 'condition': 'checkout_linux'}}
hooks = [{'action': ['this-command-must-not-run']}]
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'DEPS'
            path.write_text(text)
            result = deps.inventory(path, 'b' * 40)
        library = result['dependencies']['src/library']
        self.assertEqual(library['revision'], 'a' * 40)
        self.assertEqual(library['condition'], 'checkout_linux')
        self.assertEqual(library['repository'], 'https://example.invalid/library.git')
        self.assertFalse(result['hooks_executed'])

    def test_rejects_executable_python(self):
        for text in ("import os", "vars = __import__('os')", "vars = {'a': (lambda: 1)()}"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                deps.parse_deps(text)

    def test_rejects_floating_git_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'DEPS'
            path.write_text("vars = {}\ndeps = {'src/library': 'https://example.invalid/lib.git@main'}\n")
            with self.assertRaisesRegex(ValueError, 'Unpinned'):
                deps.inventory(path, 'b' * 40)

    def test_keeps_pinned_package_metadata_and_disabled_entries(self):
        result = deps.parse_deps("vars = {}\ndeps = {'src/unused': None, 'src/tool': {'dep_type': 'cipd', 'packages': [{'package': 'tools/gn', 'version': 'instance-id'}]}}")
        self.assertIsNone(result['deps']['src/unused'])
        self.assertEqual(result['deps']['src/tool']['packages'][0]['version'], 'instance-id')

    def test_resolves_literal_url_templates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'DEPS'
            path.write_text("vars = {'host': 'https://example.invalid'}\ndeps = {'src/library': '{host}/lib.git@" + 'a' * 40 + "'}\n")
            result = deps.inventory(path, 'b' * 40)
        self.assertEqual(result['dependencies']['src/library']['repository'], 'https://example.invalid/lib.git')

    def test_rejects_mismatched_gitlink_pin(self):
        declared = {'dependencies': {'src/library': {'dep_type': 'git', 'revision': 'a' * 40}}}
        tree = b'160000 commit ' + b'a' * 40 + b'\tlibrary\0'
        self.assertEqual(deps.verify_gitlinks(declared, tree), 1)
        with self.assertRaisesRegex(ValueError, 'library'):
            deps.verify_gitlinks(declared, tree.replace(b'a' * 40, b'b' * 40))
