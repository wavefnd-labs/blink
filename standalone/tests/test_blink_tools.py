import hashlib
import importlib.util
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("blink_tools", Path(__file__).resolve().parents[1] / "tools/blink.py")
blink = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(blink)

ROOT_TARGET = "//standalone:engine"
POLICY = {
    "forbidden_target_prefixes": ["//content", "//chrome", "//headless"],
    "reject_testonly": True,
    "dependency_fields": ["deps", "public_deps", "data_deps"],
}


def node(**kwargs):
    return {"type": "source_set", **kwargs}


class BoundaryTests(unittest.TestCase):
    def test_transitive_browser_dependency_includes_witness_chain(self):
        graph = {
            ROOT_TARGET: node(deps=["//adapter:host"]),
            "//adapter:host": node(public_deps=["//content/public:renderer"]),
            "//content/public:renderer": node(),
        }
        result = blink.boundary_check(graph, ROOT_TARGET, POLICY)
        self.assertFalse(result["passed"])
        self.assertEqual(result["violations"][0]["chain"], [ROOT_TARGET, "//adapter:host", "//content/public:renderer"])

    def test_data_dependencies_are_checked(self):
        graph = {ROOT_TARGET: node(data_deps=["//headless:shell"]), "//headless:shell": node()}
        self.assertFalse(blink.boundary_check(graph, ROOT_TARGET, POLICY)["passed"])

    def test_missing_graph_entries_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "Incomplete GN graph"):
            blink.boundary_check({ROOT_TARGET: node(deps=["//base:base"])}, ROOT_TARGET, POLICY)

    def test_testonly_dependency_is_rejected(self):
        graph = {ROOT_TARGET: node(deps=["//base/test:support"]), "//base/test:support": node(testonly=True)}
        self.assertFalse(blink.boundary_check(graph, ROOT_TARGET, POLICY)["passed"])

    def test_cycles_terminate_and_unreachable_nodes_do_not_count(self):
        graph = {ROOT_TARGET: node(deps=["//base:base"]), "//base:base": node(deps=[ROOT_TARGET]), "//content:content": node()}
        result = blink.boundary_check(graph, ROOT_TARGET, POLICY)
        self.assertTrue(result["passed"])
        self.assertEqual(result["target_count"], 2)

    def test_prefix_boundary_and_secondary_toolchains(self):
        safe = "//contentious:helper"
        forbidden = "//content:content(//build/toolchain:clang)"
        graph = {ROOT_TARGET: node(deps=[safe, forbidden]), safe: node(), forbidden: node()}
        result = blink.boundary_check(graph, ROOT_TARGET, POLICY)
        self.assertEqual([v["target"] for v in result["violations"]], [forbidden])

    def test_exact_browser_target_rule_preserves_shared_component_types(self):
        blocked = '//components/shared:browser_service(//build/toolchain:clang)'
        retained = '//components/shared:api_types'
        policy = {**POLICY, 'forbidden_targets': ['//components/shared:browser_service']}
        graph = {ROOT_TARGET: node(deps=[retained]), retained: node()}
        self.assertTrue(blink.boundary_check(graph, ROOT_TARGET, policy)['passed'])
        graph[retained]['deps'] = [blocked]
        graph[blocked] = node()
        result = blink.boundary_check(graph, ROOT_TARGET, policy)
        self.assertFalse(result['passed'])
        self.assertEqual(result['violations'][0]['chain'], [ROOT_TARGET, retained, blocked])

    def test_rejects_non_description_and_malformed_edges(self):
        for invalid in ({}, {"type": "group", "deps": "//content:content"}):
            with self.assertRaises(ValueError):
                blink.boundary_check({ROOT_TARGET: invalid}, ROOT_TARGET, POLICY)


class SnapshotTests(unittest.TestCase):
    def test_sync_uses_original_path_for_relocated_blink_sources(self):
        from types import SimpleNamespace
        data = b"original Blink header"
        lock = {
            "revision": "a" * 40,
            "files": {"public/header.h": {
                "upstream_path": "third_party/blink/public/header.h",
                "sha256": blink.sha256(data), "bytes": len(data),
            }},
            "protected_roots": ["public"],
        }
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(lock="unused", sources=directory)
            with patch.object(blink, "load_lock", return_value=lock), patch.object(blink, "fetch_file", return_value=data) as fetch:
                blink.sync(args)
            fetch.assert_called_once_with("a" * 40, "third_party/blink/public/header.h")
            self.assertEqual((Path(directory) / "public/header.h").read_bytes(), data)

    def test_capture_preserves_upstream_mapping_and_baseline_hashes(self):
        from types import SimpleNamespace
        data = b"next upstream version"
        lock = {
            "schema": 1, "origin": blink.ORIGIN, "scope": "test",
            "files": {"renderer/item.h": {"upstream_path": "third_party/blink/renderer/item.h"}},
            "protected_roots": ["renderer"],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "candidate"
            args = SimpleNamespace(lock="unused", sources=root, revision="b" * 40, output=output)
            with patch.object(blink, "load_lock", return_value=lock), patch.object(blink, "fetch_file", return_value=data) as fetch:
                blink.capture(args)
            fetch.assert_called_once_with("b" * 40, "third_party/blink/renderer/item.h")
            self.assertEqual((output / "upstream/renderer/item.h").read_bytes(), data)
            candidate = blink.read_json(output / "upstream.lock.json")
            self.assertEqual(candidate["files"]["renderer/item.h"]["upstream_path"], "third_party/blink/renderer/item.h")
            self.assertEqual(candidate["files"]["renderer/item.h"]["sha256"], blink.sha256(data))

    def test_lock_rejects_invalid_original_paths(self):
        lock = {
            "origin": blink.ORIGIN, "revision": "a" * 40,
            "files": {"public/item.h": {
                "upstream_path": "../escape", "sha256": "a" * 64, "bytes": 1,
            }},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lock.json"
            blink.write_json(path, lock)
            with self.assertRaisesRegex(ValueError, "Invalid source path"):
                blink.load_lock(path)

    def test_rejects_traversal_and_url_metacharacters(self):
        for path in (".", "../escape", "/absolute", "a/../../escape", "a//b", "a?format=JSON", "a\\b"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                blink.source_path(path)

    def test_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "snapshot"
            root.mkdir()
            (root / "link").symlink_to(Path(directory))
            with self.assertRaises(ValueError):
                blink.destination(root, "link/escape")

    def test_detects_local_upstream_edits_and_missing_files(self):
        data = b"original"
        lock = {"files": {"file.h": {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}}}
        with tempfile.TemporaryDirectory() as directory:
            self.assertIn("missing", blink.verify_files(lock, directory)[0])
            path = Path(directory) / "file.h"
            path.write_bytes(data)
            self.assertEqual(blink.verify_files(lock, directory), [])
            path.write_bytes(b"modified")
            self.assertIn("modified", blink.verify_files(lock, directory)[0])

    def test_rejects_project_files_added_inside_original_tree(self):
        lock = {"files": {}, "protected_roots": ["third_party/blink"]}
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "third_party/blink"
            source.mkdir(parents=True)
            (source / "local_helper.cc").write_text("// custom code")
            self.assertEqual(blink.verify_files(lock, directory), [
                "unexpected file in upstream tree: third_party/blink/local_helper.cc"
            ])

    def test_rejects_symlink_substitution_even_for_matching_bytes(self):
        data = b"upstream"
        lock = {"files": {"file.h": {"sha256": blink.sha256(data), "bytes": len(data)}}}
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "copy.h").write_bytes(data)
            (Path(directory) / "file.h").symlink_to("copy.h")
            self.assertIn("symlink replaces", blink.verify_files(lock, directory)[0])

    def test_auxiliary_output_cannot_write_original_sources(self):
        lock = {"files": {"LICENSE": {}}, "protected_roots": ["third_party/blink"]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ("third_party/blink/report.json", "LICENSE", "LICENSE/report.json"):
                with self.subTest(path=relative), self.assertRaisesRegex(ValueError, "upstream"):
                    blink.ensure_auxiliary_output(root / relative, lock, root)
            blink.ensure_auxiliary_output(root / "standalone/report.json", lock, root)

    def test_auxiliary_output_guard_resolves_symlinks(self):
        lock = {"files": {}, "protected_roots": ["third_party/blink"]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "third_party/blink"
            source.mkdir(parents=True)
            (root / "alias").symlink_to(source, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "protected upstream"):
                blink.ensure_auxiliary_output(root / "alias/report.json", lock, root)

    def test_capture_cannot_create_snapshot_within_source_tree(self):
        from types import SimpleNamespace
        lock = {"files": {}, "protected_roots": ["third_party/blink"]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "third_party/blink/candidate"
            args = SimpleNamespace(lock="unused", sources=root, revision="a" * 40, output=output)
            with patch.object(blink, "load_lock", return_value=lock), patch.object(blink, "fetch_file") as fetch:
                with self.assertRaisesRegex(ValueError, "protected upstream"):
                    blink.capture(args)
                fetch.assert_not_called()
            self.assertFalse(output.exists())

    def test_sync_does_not_overwrite_a_local_edit(self):
        from types import SimpleNamespace
        lock = {"revision": "a" * 40, "files": {"file.h": {"sha256": hashlib.sha256(b"upstream").hexdigest(), "bytes": 8}}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file.h"
            path.write_bytes(b"local edit")
            with patch.object(blink, "load_lock", return_value=lock), patch.object(blink, "fetch_file") as fetch:
                with self.assertRaisesRegex(ValueError, "Refusing to overwrite"):
                    blink.sync(SimpleNamespace(lock="unused", sources=directory))
                fetch.assert_not_called()
            self.assertEqual(path.read_bytes(), b"local edit")

    def test_audit_distinguishes_comments_imports_and_includes(self):
        sources = {
            "BUILD.gn": '# "//fake:comment"\nimport("//build/config.gni")\ndeps = [ "//base:base" ]\n',
            "testing/a.cc": '// #include "fake/comment.h"\n/* #include "fake/block.h" */\n#include "v8/include/v8.h"\n',
        }
        lock = {"revision": "a" * 40, "files": {}}
        with tempfile.TemporaryDirectory() as directory:
            for name, text in sources.items():
                path = Path(directory) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
                data = path.read_bytes()
                lock["files"][name] = {"sha256": blink.sha256(data), "bytes": len(data)}
            report = blink.audit(lock, directory)
        self.assertEqual(report["reference_count"], 3)
        self.assertEqual([r["kind"] for r in report["references"]], ["gn_import", "gn_string", "include"])
        self.assertNotIn("fake", report["groups"])
        self.assertEqual(report["references"][2]["line"], 3)


if __name__ == "__main__":
    unittest.main()
