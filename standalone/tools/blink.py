#!/usr/bin/env python3
"""Pinned source reconnaissance and GN boundary checks for standalone Blink.

This tool does not build or run Blink. A lexical audit is NOT a resolved GN graph.
Requires standard-library Python and Git for full-tree verification.
"""

import argparse
import base64
import collections
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import subprocess
import urllib.request

SUPPORT = Path(__file__).resolve().parents[1]
ROOT = SUPPORT.parent
ORIGIN = "https://chromium.googlesource.com/chromium/src"
REVISION = re.compile(r"[0-9a-f]{40}")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def source_path(value):
    path = PurePosixPath(value)
    if (not value or not path.parts or path.is_absolute() or ".." in path.parts
            or str(path) != value or "\\" in value
            or not re.fullmatch(r"[A-Za-z0-9_./+-]+", value)):
        raise ValueError(f"Invalid source path: {value!r}")
    return path


def destination(root, relative):
    result = Path(root) / str(source_path(relative))
    if not result.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError(f"Source path escapes destination: {relative}")
    return result


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def load_lock(path):
    lock = read_json(path)
    if lock.get("origin") != ORIGIN or not REVISION.fullmatch(lock.get("revision", "")):
        raise ValueError("Expected official Chromium origin and immutable 40-character revision")
    if not lock.get("files") or not isinstance(lock["files"], dict):
        raise ValueError("Lock must contain a nonempty file inventory")
    for name, entry in lock["files"].items():
        source_path(name)
        source_path(entry.get("upstream_path", name))
        if (not re.fullmatch(r"[0-9a-f]{64}", entry.get("sha256", ""))
                or type(entry.get("bytes")) is not int or entry["bytes"] < 0):
            raise ValueError(f"Invalid lock entry: {name}")
    for protected in lock.get("protected_roots", []):
        source_path(protected)
    if "git_source_tree" in lock and not REVISION.fullmatch(lock["git_source_tree"].get("tree", "")):
        raise ValueError("Invalid original Git source tree")
    return lock


def fetch_file(revision, name):
    source_path(name)
    if not REVISION.fullmatch(revision):
        raise ValueError("Revision must be an immutable 40-character commit ID")
    request = urllib.request.Request(
        f"{ORIGIN}/+/{revision}/{name}?format=TEXT",
        headers={"User-Agent": "standalone-blink-source-audit/1"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        encoded = response.read(32 * 1024 * 1024 + 1)
    if len(encoded) > 32 * 1024 * 1024:
        raise ValueError(f"Audit file exceeds 32 MiB encoded limit: {name}")
    return base64.b64decode(encoded, validate=True)


def verify_files(lock, root):
    errors = []
    for name, expected in lock["files"].items():
        path = destination(root, name)
        if path.is_symlink():
            errors.append(f"symlink replaces upstream file: {name}")
            continue
        if not path.is_file():
            errors.append(f"missing: {name}")
            continue
        data = path.read_bytes()
        if len(data) != expected["bytes"] or sha256(data) != expected["sha256"]:
            errors.append(f"modified: {name}")
    if "git_source_tree" in lock:
        return errors + verify_source_tree(lock, root)
    for protected in lock.get("protected_roots", []):
        directory = destination(root, protected)
        if directory.is_symlink():
            errors.append(f"symlink replaces protected source tree: {protected}")
            continue
        for path in sorted(directory.rglob("*")):
            if path.is_file() or path.is_symlink():
                relative = path.relative_to(root).as_posix()
                if relative not in lock["files"]:
                    errors.append(f"unexpected file in upstream tree: {relative}")
    return errors


def verify_source_tree(lock, root):
    """Check the full imported source tree against an immutable Git baseline."""
    root = Path(root).resolve()
    def git(*args):
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(root), *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "GIT_NO_LAZY_FETCH": "1"},
        )
        if result.returncode:
            raise ValueError(result.stderr.decode(errors="replace").strip() or "Git source verification failed")
        return result.stdout
    if Path(git("rev-parse", "--show-toplevel").decode().strip()).resolve() != root:
        return ["source tree is not at the expected Git repository root"]
    tree = lock["git_source_tree"]["tree"]
    names = [name.decode() for name in git("ls-tree", "--name-only", "-z", tree).split(b"\0") if name]
    paths = [f":(literal){name}" for name in names]
    changes = git("diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-status", "-z", tree, "--", *paths).split(b"\0")
    errors = []
    for index in range(0, len(changes) - 1, 2):
        errors.append(f"upstream source differs ({changes[index].decode()}): {changes[index + 1].decode(errors='replace')}")
    unknown = git("ls-files", "--others", "-z", "--", *paths)
    errors.extend(f"unexpected file in upstream tree: {name.decode(errors='replace')}"
                  for name in unknown.split(b"\0") if name)
    return errors


def ensure_auxiliary_output(path, lock, sources):
    """Do not place helper output inside original sources, including via symlinks."""
    output = Path(path).resolve()
    for root in {ROOT.resolve(), Path(sources).resolve()}:
        for protected in lock.get("protected_roots", []):
            original = (root / str(source_path(protected))).resolve()
            if output == original or output.is_relative_to(original):
                raise ValueError(f"Helper output would modify protected upstream sources: {path}")
        for name in lock["files"]:
            original = (root / str(source_path(name))).resolve()
            if output == original or output.is_relative_to(original):
                raise ValueError(f"Helper output would overwrite an upstream file: {path}")


def sync(args):
    lock = load_lock(args.lock)
    # Missing original files can be restored; edits/additions must be reported.
    violations = [error for error in verify_files(lock, args.sources)
                  if not error.startswith("missing: ")]
    if violations:
        raise ValueError("Refusing to overwrite or accept modified upstream sources:\n" + "\n".join(violations))

    def get(name):
        path = destination(args.sources, name)
        expected = lock["files"][name]
        if path.exists():
            data = path.read_bytes()
            if sha256(data) != expected["sha256"] or len(data) != expected["bytes"]:
                raise ValueError(f"Refusing to overwrite modified upstream source: {name}")
            return
        data = fetch_file(lock["revision"], expected.get("upstream_path", name))
        if sha256(data) != expected["sha256"] or len(data) != expected["bytes"]:
            raise ValueError(f"Downloaded source does not match lock: {name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation prevents concurrent sync from overwriting edits.
        with path.open("xb") as stream:
            stream.write(data)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(get, lock["files"]))
    errors = verify_files(lock, args.sources)
    if errors:
        raise ValueError("\n".join(errors))
    print(f"Verified {len(lock['files'])} pinned upstream files")


def capture(args):
    """Capture the SAME audit inventory at a new revision, separately for review."""
    old = load_lock(args.lock)
    if not REVISION.fullmatch(args.revision):
        raise ValueError("--revision must be a full commit ID")
    out = Path(args.output)
    ensure_auxiliary_output(out, old, args.sources)
    if out.exists():
        raise ValueError("Capture output must be a new directory; existing sources are never replaced")
    out.mkdir(parents=True)
    lock = {key: old[key] for key in ("schema", "origin", "scope")}
    if "git_source_tree" in old:
        lock["scope"] = "Reconnaissance files only; full-tree updates require Git history import"
    lock["protected_roots"] = old.get("protected_roots", [])
    lock["revision"] = args.revision
    lock["files"] = {}
    # Write a candidate lock only after every requested file is fetched.
    # An incomplete directory has no lock and cannot pass audit/verify.
    for name in sorted(old["files"]):
        upstream_path = old["files"][name].get("upstream_path", name)
        data = fetch_file(args.revision, upstream_path)
        path = destination(out / "upstream", name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        lock["files"][name] = {
            "upstream_path": upstream_path,
            "sha256": sha256(data), "bytes": len(data),
        }
    write_json(out / "upstream.lock.json", lock)
    print(f"Candidate snapshot: {out} (not promoted to the project lock)")


def group_for(path):
    path = path.removeprefix("//").split(":", 1)[0]
    if not path:
        return "(root)"
    if path.startswith("$"):
        return "(dynamic)"
    parts = path.split("/")
    return "/".join(parts[:2]) if parts[0] == "third_party" else parts[0]


def audit(lock, root):
    errors = verify_files(lock, root)
    if errors:
        raise ValueError("\n".join(errors))
    refs = []
    for name in sorted(lock["files"]):
        if not name.endswith((".h", ".cc", ".gn", ".gni")):
            continue
        is_gn = name.endswith((".gn", ".gni"))
        # Minimal lexical scanner: discard comments before collecting strings.
        token = re.compile(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"')
        if is_gn:
            token = re.compile(r'\#[^\n]*|"(?:\\.|[^"\\])*"')
        contents = destination(root, name).read_text(encoding="utf-8")
        for match in token.finditer(contents):
            value = match.group()
            if not value.startswith('"'):
                continue
            value = value[1:-1]
            line_start = contents.rfind("\n", 0, match.start()) + 1
            prefix = contents[line_start:match.start()]
            if is_gn and value.startswith("//"):
                kind = "gn_import" if re.search(r"\bimport\s*\(\s*$", prefix) else "gn_string"
            elif not is_gn and re.match(r"\s*#\s*include\s*$", prefix):
                kind = "include"
            else:
                continue
            if value.startswith(("//third_party/blink/", "third_party/blink/")):
                continue
            refs.append({
                "file": name,
                "upstream_file": lock["files"][name].get("upstream_path", name),
                "line": contents.count("\n", 0, match.start()) + 1,
                "kind": kind,
                "reference": value,
                "group": group_for(value),
                "context": "test_support" if "/testing/" in name else "production_or_mixed",
            })
    return {
        "schema": 1,
        "revision": lock["revision"],
        "scope": "Pinned reconnaissance subset, not all Blink sources",
        "method": "Lexical references only; conditions, target ownership and transitive deps are unresolved",
        "file_count": len(lock["files"]),
        "reference_count": len(refs),
        "groups": dict(sorted(collections.Counter(ref["group"] for ref in refs).items())),
        "references": refs,
    }


def label_path(label):
    return label.split("(", 1)[0].split(":", 1)[0]


def boundary_check(graph, target, policy):
    """Traverse a resolved `gn desc OUT '*' --format=json` target graph.

    This checks declared target edges, not all binary/runtime behaviour.
    Config/import/toolchain contents still require separate review.
    """
    if target not in graph:
        raise ValueError(f"Root target absent from GN graph: {target}")
    queue = collections.deque([target])
    parents = {target: None}
    violations = []
    while queue:
        label = queue.popleft()
        if label not in graph:
            raise ValueError(f"Incomplete GN graph; missing dependency: {label}")
        node = graph[label]
        if not isinstance(node, dict) or not isinstance(node.get("type"), str):
            raise ValueError(f"Expected a full GN target description: {label}")
        reasons = []
        path = label_path(label)
        if label.split("(", 1)[0] in policy.get("forbidden_targets", []):
            reasons.append("excluded browser implementation target")
        for prefix in policy["forbidden_target_prefixes"]:
            if path == prefix or path.startswith(prefix + "/"):
                reasons.append(f"forbidden browser layer: {prefix}")
        if policy.get("reject_testonly") and node.get("testonly", False):
            reasons.append("testonly target in engine closure")
        if reasons:
            chain, current = [], label
            while current is not None:
                chain.append(current)
                current = parents[current]
            violations.append({"target": label, "reasons": reasons, "chain": chain[::-1]})
        for field in policy["dependency_fields"]:
            edges = node.get(field, [])
            if not isinstance(edges, list) or any(not isinstance(edge, str) for edge in edges):
                raise ValueError(f"Malformed dependency field {field} in {label}")
            for edge in edges:
                if edge not in parents:
                    parents[edge] = label
                    queue.append(edge)
    return {
        "root_target": target,
        "target_count": len(parents),
        "groups": dict(sorted(collections.Counter(group_for(label_path(label)) for label in parents).items())),
        "violations": violations,
        "passed": not violations,
        "limitation": "Declared target edges only; does not prove standalone buildability or runtime independence",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=SUPPORT / "config/upstream.lock.json")
    parser.add_argument("--sources", type=Path, default=ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sync", help="Fetch missing locked audit files without changing upstream sources")
    sub.add_parser("verify", help="Check pristine upstream equality offline; adaptations are reported as differences")
    cap = sub.add_parser("capture", help="Capture same audit inventory at a candidate commit")
    cap.add_argument("--revision", required=True)
    cap.add_argument("--output", required=True, type=Path)
    aud = sub.add_parser("audit", help="Inventory lexical external references; not a GN closure")
    aud.add_argument("--output", type=Path, default=SUPPORT / "reports/dependency-audit.json")
    graph = sub.add_parser("check-graph", help="Check a full resolved GN JSON graph")
    graph.add_argument("--graph", type=Path, required=True)
    graph.add_argument("--policy", type=Path, default=SUPPORT / "config/boundary.json")
    graph.add_argument("--target")
    graph.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "sync":
            sync(args)
        elif args.command == "capture":
            capture(args)
        elif args.command == "verify":
            lock = load_lock(args.lock)
            errors = verify_files(lock, args.sources)
            if errors:
                raise ValueError("\n".join(errors))
            print(f"Verified {len(lock['files'])} pinned upstream files")
            if "git_source_tree" in lock:
                print(f"Full upstream tree matches {lock['git_source_tree']['tree']}")
        elif args.command == "audit":
            lock = load_lock(args.lock)
            ensure_auxiliary_output(args.output, lock, args.sources)
            report = audit(lock, args.sources)
            write_json(args.output, report)
            print(f"{report['reference_count']} lexical references in {report['file_count']} audit files")
        elif args.command == "check-graph":
            ensure_auxiliary_output(args.output, load_lock(args.lock), args.sources)
            policy = read_json(args.policy)
            report = boundary_check(read_json(args.graph), args.target or policy["root_target"], policy)
            write_json(args.output, report)
            print(f"{report['target_count']} targets, {len(report['violations'])} boundary violations")
            return 0 if report["passed"] else 1
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
