#!/usr/bin/env python3
"""Extract Blink's native Git path history without changing source blobs.

Works in a disposable shared bare clone. Original subtree objects become the
root trees of derived commits. Only Git and standard-library Python are needed.
Historical blob bodies can remain promised by the original partial-clone remote.
"""

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

PATHS = ["third_party/WebKit", "third_party/blink"]
ZERO = b"0" * 40
TRANSITION_PATH = b".upstream-WebKit"


def git(repository, *args):
    return subprocess.check_output(
        ["git", "-c", "credential.helper=", "-C", str(repository), *args], text=True,
    ).strip()


class CommitReader:
    def __init__(self, repository):
        self.process = subprocess.Popen(
            ["git", "-C", str(repository), "cat-file", "--batch"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            env={**os.environ, "GIT_NO_LAZY_FETCH": "1"},
        )
        self.last_tree = None

    def read(self, revision, kind=b"commit"):
        self.process.stdin.write(revision + b"\n")
        self.process.stdin.flush()
        info = self.process.stdout.readline().split()
        if len(info) != 3 or info[0] != revision or info[1] != kind:
            raise ValueError(f"Missing original or extracted commit: {revision!r}")
        raw = self.process.stdout.read(int(info[2]))
        if self.process.stdout.read(1) != b"\n":
            raise ValueError("Malformed git cat-file batch output")
        return raw

    def metadata(self, revision):
        header, _, message = self.read(revision).partition(b"\n\n")
        self.last_tree = header.splitlines()[0].split()[1]
        fields = tuple(line for line in header.splitlines()
                       if line.startswith((b"author ", b"committer ", b"encoding ")))
        return fields, message

    def close(self):
        self.process.stdin.close()
        self.process.stdout.close()
        self.process.wait()


class TreeReader:
    def __init__(self, repository, empty_tree):
        self.empty_tree = empty_tree
        self.objects = CommitReader(repository)
        self.entry_cache = {}
        self.process = subprocess.Popen(
            ["git", "-C", str(repository), "cat-file", "--batch-check=%(objectname) %(objecttype)"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            env={**os.environ, "GIT_NO_LAZY_FETCH": "1"},
        )

    def read(self, revision):
        found = {}
        for path in reversed(PATHS):
            query = revision + b":" + path.encode()
            self.process.stdin.write(query + b"\n")
            self.process.stdin.flush()
            info = self.process.stdout.readline().split()
            if len(info) != 2:
                raise ValueError("Malformed tree lookup result")
            if info[1] == b"tree":
                found[path] = info[0]
        modern, legacy = found.get(PATHS[1]), found.get(PATHS[0])
        if modern and legacy:
            # Keep BOTH original trees during the staged directory migration.
            # Same-named files such as OWNERS differ and must not be discarded.
            if modern not in self.entry_cache:
                raw = self.objects.read(modern, b"tree")
                entries, position = [], 0
                while position < len(raw):
                    end = raw.index(b"\0", position)
                    mode, name = raw[position:end].split(b" ", 1)
                    if name == TRANSITION_PATH:
                        raise ValueError("Transition namespace collides with original Blink source")
                    key = name + (b"/" if mode == b"40000" else b"\0")
                    entries.append((key, raw[position:end + 21]))
                    position = end + 21
                self.entry_cache[modern] = entries
            entries = [*self.entry_cache[modern], (
                TRANSITION_PATH + b"/", b"40000 " + TRANSITION_PATH + b"\0" + bytes.fromhex(legacy.decode()),
            )]
            raw = b"".join(value for key, value in sorted(entries))
            expected = hashlib.sha1(b"tree " + str(len(raw)).encode() + b"\0" + raw).hexdigest().encode()
            return expected, modern, legacy
        # Before the monorepo import, directory rolls can have no embedded tree.
        # Keep their original metadata; the separate legacy ref has that source.
        tree = modern or legacy or self.empty_tree
        return tree, tree, None

    def close(self):
        self.objects.close()
        self.process.stdin.close()
        self.process.stdout.close()
        self.process.wait()


def verify_metadata(original, extracted, mapping):
    """Check every retained commit's metadata and exact original subtree."""
    before, after = CommitReader(original), CommitReader(extracted)
    count = 0
    try:
        with Path(mapping).open("rb") as stream:
            next(stream)
            for line in stream:
                old, new, *tree = line.split()
                if new == ZERO:
                    continue
                if before.metadata(old) != after.metadata(new):
                    raise ValueError(f"Author, committer, encoding or message changed: {old.decode()}")
                if tree and after.last_tree != tree[0]:
                    raise ValueError(f"Original source tree changed: {old.decode()}")
                count += 1
                if count % 20000 == 0:
                    print(f"Verified metadata and original trees for {count} commits", flush=True)
    finally:
        before.close()
        after.close()
    return count


def extract(cache, revision, output):
    cache, output = Path(cache).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError("Extraction output must be a new directory")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Use an exact 40-character original commit ID")
    if git(cache, "rev-parse", "--is-bare-repository") != "true":
        raise ValueError("Expected an original bare cache, not a working project")
    if git(cache, "rev-parse", "--is-shallow-repository") != "false":
        raise ValueError("Refusing to label shallow ancestry as full history")
    if git(cache, "rev-parse", "--verify", f"{revision}^{{commit}}") != revision:
        raise ValueError("Original commit not found")
    print("Creating a disposable shared clone; original cache stays unchanged", flush=True)
    subprocess.run(["git", "clone", "--bare", "--shared", str(cache), str(output)], check=True)
    remote = git(cache, "config", "remote.origin.url")
    git(output, "config", "remote.origin.url", remote)
    git(output, "config", "remote.origin.promisor", "true")
    git(output, "config", "remote.origin.partialclonefilter", "blob:none")
    empty_tree = subprocess.check_output(["git", "-C", str(output), "mktree"], input=b"").strip()
    marks_file = output / "extracted.marks"
    importer = subprocess.Popen(
        ["git", "-C", str(output), "fast-import", "--quiet", "--date-format=raw-permissive", f"--export-marks={marks_file}"],
        stdin=subprocess.PIPE, env={**os.environ, "GIT_NO_LAZY_FETCH": "1"},
    )
    selected = subprocess.Popen(
        ["git", "-C", str(cache), "rev-list", "--parents", "--reverse", "--topo-order",
         "--full-history", "--simplify-merges", revision, "--", *PATHS], stdout=subprocess.PIPE,
        env={**os.environ, "GIT_NO_LAZY_FETCH": "1"},
    )
    commits, trees = CommitReader(cache), TreeReader(cache, empty_tree)
    marks, records = {}, []
    transition_count = 0
    print("Selecting full path history and reusing original subtree objects", flush=True)
    try:
        for line in selected.stdout:
            old, *parents = line.split()
            raw = commits.read(old)
            header, _, message = raw.partition(b"\n\n")
            fields = [field for field in header.splitlines()
                      if field.startswith((b"author ", b"committer ", b"encoding "))]
            tree, root_tree, legacy_tree = trees.read(old)
            mark = str(len(records) + 1).encode()
            parent_marks = [marks[parent] for parent in parents]
            chunk = b"commit refs/heads/blink\nmark :" + mark + b"\n"
            chunk += b"\n".join(fields) + b"\ndata " + str(len(message)).encode() + b"\n" + message + b"\n"
            chunk += b"from " + (b":" + parent_marks[0] if parent_marks else ZERO) + b"\n"
            chunk += b"".join(b"merge :" + parent + b"\n" for parent in parent_marks[1:])
            chunk += b'deleteall\nM 040000 ' + root_tree + b' ""\n'
            if legacy_tree:
                chunk += b'M 040000 ' + legacy_tree + b' "' + TRANSITION_PATH + b'"\n'
                transition_count += 1
            chunk += b"\n"
            importer.stdin.write(chunk)
            marks[old] = mark
            records.append((old, mark, tree))
            if len(records) % 10000 == 0:
                print(f"Reused original trees for {len(records)} Blink commits", flush=True)
        if selected.wait() != 0:
            raise ValueError("Native Git path history traversal failed")
        importer.stdin.write(b"done\n")
        importer.stdin.close()
        if importer.wait() != 0:
            raise ValueError("Original-tree history import failed")
    finally:
        commits.close()
        trees.close()
        selected.stdout.close()
        if selected.poll() is None:
            selected.terminate()
            selected.wait()
        if importer.poll() is None:
            importer.terminate()
            importer.wait()
    if not records:
        raise ValueError("No Blink path history found")
    exported = dict(line.split() for line in marks_file.read_bytes().splitlines())
    mapping = output / "blink-commit-map.tsv"
    with mapping.open("wb") as stream:
        stream.write(b"old new projected_tree\n")
        for old, mark, tree in records:
            stream.write(b" ".join((old, exported[b":" + mark], tree)) + b"\n")
    with mapping.open("rb") as source, gzip.GzipFile(filename=str(mapping) + ".gz", mode="wb", mtime=0) as target:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            target.write(chunk)
    git(output, "symbolic-ref", "HEAD", "refs/heads/blink")
    expected_tree = git(cache, "rev-parse", f"{revision}:third_party/blink")
    actual_tree = git(output, "rev-parse", "refs/heads/blink^{tree}")
    if expected_tree != actual_tree:
        raise ValueError("Extracted tip tree differs from original Blink subtree")
    print("Checking all original authors, dates, messages and source trees", flush=True)
    verified = verify_metadata(cache, output, mapping)
    report = {
        "original_repository": remote, "original_revision": revision,
        "paths": PATHS, "method": "native Git full path history and original subtree reuse",
        "extracted_revision": git(output, "rev-parse", "refs/heads/blink"),
        "extracted_commit_count": int(git(output, "rev-list", "--count", "refs/heads/blink")),
        "retained_mapping_rows": len(records), "metadata_verified_commits": verified,
        "transition_commits": transition_count, "transition_namespace": TRANSITION_PATH.decode(),
        "tree_verified_commits": verified, "original_blink_tree": expected_tree,
        "extracted_tree": actual_tree, "source_bytes_changed": False,
        "git_version": subprocess.check_output(["git", "--version"], text=True).strip(),
        "history_blobs": "May be lazy-fetched from original remote; not an offline blob-complete archive",
    }
    (output / "blink-extraction.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        extract(args.cache, args.revision, args.output)
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
