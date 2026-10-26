#!/usr/bin/env python3
"""Restore promised historical Git objects without changing source or refs.

The missing-object inventory must be produced offline, for example with
GIT_NO_LAZY_FETCH=1 git rev-list --objects --missing=print. This command accepts
one bare object ID per line (an optional leading '?' is also accepted). Reruns
inspect the object database again, so interrupted batches are safe to resume.
No completeness claim is made beyond the supplied inventory: use the offline
audit command again after restoration, since missing trees hide children.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
OID = re.compile(r"[0-9a-f]{40}")
REMOTE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
OFFICIAL = {
    ("github.com", "/chromium/chromium"),
    ("chromium.googlesource.com", "/chromium/src"),
    ("chromium.googlesource.com", "/chromium/blink"),
}
MAXIMUM_GITHUB_BLOB_BYTES = 100 * 1024 * 1024


def offline_environment():
    return {**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0"}


def git(repository, *arguments):
    return subprocess.check_output(["git", "-C", str(repository), *arguments],
                                   env=offline_environment(), text=True).strip()


def repository_paths(repository):
    repository = Path(repository).resolve(strict=True)
    if git(repository, "rev-parse", "--show-object-format") != "sha1":
        raise ValueError("This inventory format requires a SHA-1 Git repository")
    git_directory = Path(git(repository, "rev-parse", "--git-common-dir"))
    if not git_directory.is_absolute():
        git_directory = repository / git_directory
    object_directory = Path(git(repository, "rev-parse", "--git-path", "objects"))
    if not object_directory.is_absolute():
        object_directory = repository / object_directory
    return repository, git_directory.resolve(), object_directory.resolve()


def validate_repository(repository, remote):
    repository, git_directory, object_directory = repository_paths(repository)
    if not REMOTE.fullmatch(remote) or ".." in remote or remote.endswith("/"):
        raise ValueError("Use a configured remote name, not a URL or option")
    urls = git(repository, "remote", "get-url", "--all", remote).splitlines()
    if len(urls) != 1:
        raise ValueError("The promisor remote must have exactly one URL")
    parsed = urlsplit(urls[0])
    path = parsed.path.removesuffix(".git").rstrip("/")
    if (parsed.scheme != "https" or parsed.username or parsed.password or
            parsed.port not in (None, 443) or parsed.query or parsed.fragment or
            (parsed.hostname, path) not in OFFICIAL):
        raise ValueError("Expected an official Chromium or legacy Blink HTTPS remote")
    try:
        promisor = git(repository, "config", "--bool", "--get", f"remote.{remote}.promisor")
    except subprocess.CalledProcessError:
        promisor = ""
    if promisor != "true":
        raise ValueError("The selected official remote must already be configured as promisor")
    return repository, git_directory, object_directory, urls[0]


def object_ids(path):
    with Path(path).open("r", encoding="ascii") as stream:
        for number, line in enumerate(stream, 1):
            value = line.strip().removeprefix("?")
            if not OID.fullmatch(value):
                raise ValueError(f"Malformed object ID on inventory line {number}")
            yield value


def inventory_identity(path):
    count = sum(1 for _ in object_ids(path))
    with Path(path).open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return count, digest


def missing_objects(repository, objects):
    """Check only these IDs; never contact a promisor or enumerate full history."""
    unique = list(dict.fromkeys(objects))
    if not unique:
        return []
    output = subprocess.check_output(
        ["git", "-C", str(repository), "cat-file", "--batch-check=%(objectname) %(objecttype)"],
        input="".join(value + "\n" for value in unique), text=True,
        env=offline_environment()).splitlines()
    if len(output) != len(unique):
        raise ValueError("Incomplete offline Git object check")
    missing = []
    for expected, line in zip(unique, output):
        fields = line.split()
        if len(fields) != 2 or fields[0] != expected:
            raise ValueError("Malformed offline Git object check")
        if fields[1] == "missing":
            missing.append(expected)
        elif fields[1] not in {"blob", "tree", "commit", "tag"}:
            raise ValueError("Unexpected Git object type")
    return missing


def write_report(path, value):
    value["updated_at"] = datetime.now(timezone.utc).isoformat()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def fetch_objects(repository, remote, objects, log):
    command = ["git", "-c", "fetch.negotiationAlgorithm=noop", "-c", "maintenance.auto=false",
               "-c", "gc.auto=0", "-c", "fetch.writeCommitGraph=false", "-C", str(repository),
               "fetch", remote, "--no-tags", "--no-prune", "--no-prune-tags", "--refmap=",
               "--no-write-fetch-head",
               "--recurse-submodules=no", "--filter=blob:none", "--stdin"]
    with log.open("wb") as stream:
        stream.write(("Command: " + " ".join(command) + "\n").encode())
        stream.flush()
        result = subprocess.run(command, input="".join(value + "\n" for value in objects).encode(),
                                stdout=stream, stderr=subprocess.STDOUT, env=offline_environment(),
                                check=False)
    if result.returncode:
        raise RuntimeError(f"Git fetch failed with status {result.returncode}; see {log}")


def restore(repository, remote, missing, *, batch_size=5000, report=None,
            minimum_free_gib=20):
    if (type(batch_size) is not int or batch_size < 1 or
            not math.isfinite(minimum_free_gib) or minimum_free_gib < 0):
        raise ValueError("Batch size must be positive and minimum free space nonnegative")
    repository, git_directory, objects, url = validate_repository(repository, remote)
    missing = Path(missing).resolve(strict=True)
    entries, digest = inventory_identity(missing)  # Validate the ENTIRE list before any fetch.
    report = Path(report or ROOT / "standalone/work/publication/history-restore.json").resolve()
    if report == missing or git_directory == report or git_directory in report.parents:
        raise ValueError("The restoration report must not overwrite Git metadata or the inventory")
    report.parent.mkdir(parents=True, exist_ok=True)
    # A unique log directory keeps diagnostics from every resume attempt.
    logs = Path(tempfile.mkdtemp(prefix=report.stem + "-", dir=report.parent))
    state = {"schema": 1, "repository": str(repository), "remote": remote, "remote_url": url,
             "inventory": str(missing), "inventory_sha256": digest, "input_entries": entries,
             "batch_size": batch_size, "minimum_free_gib": minimum_free_gib,
             "log_directory": str(logs), "verified_entries": 0, "fetch_requested_objects": 0,
             "completed_batches": 0, "status": "running", "complete": False,
             "scope": "Supplied object inventory only; re-enumerate reachable history afterward"}
    write_report(report, state)
    try:
        stream = iter(object_ids(missing))
        batch_number = 0
        while True:
            batch = []
            for _ in range(batch_size):
                value = next(stream, None)
                if value is None:
                    break
                batch.append(value)
            if not batch:
                break
            batch_number += 1
            required = missing_objects(repository, batch)
            log = logs / f"batch-{batch_number:06d}.log"
            request = logs / f"batch-{batch_number:06d}.objects"
            request.write_text("".join(value + "\n" for value in required), encoding="ascii")
            state["last_batch"] = {"number": batch_number, "input_entries": len(batch),
                                   "requested_objects": len(required), "request_file": str(request),
                                   "log": str(log), "remaining_sample": required[:20]}
            write_report(report, state)
            print(f"Batch {batch_number}: {len(required)} missing of {len(batch)} entries; "
                  f"{state['verified_entries']}/{entries} verified", flush=True)
            if required:
                free = shutil.disk_usage(objects).free
                if free < minimum_free_gib * (1024 ** 3):
                    raise RuntimeError(f"Insufficient disk space: {free / 1024 ** 3:.1f} GiB available; "
                                       f"require {minimum_free_gib} GiB before fetching")
                state["fetch_requested_objects"] += len(required)
                write_report(report, state)
                fetch_objects(repository, remote, required, log)
            remaining = missing_objects(repository, batch)
            state["last_batch"]["remaining_sample"] = remaining[:20]
            if remaining:
                raise RuntimeError(f"Git fetch completed but {len(remaining)} requested objects are absent")
            state["verified_entries"] += len(batch)
            state["completed_batches"] += 1
            write_report(report, state)
        # Reject concurrent inventory edits even if all requested objects happened to exist.
        if inventory_identity(missing) != (entries, digest):
            raise ValueError("The missing-object inventory changed during restoration")
        state.update(status="complete", complete=True)
        write_report(report, state)
        print(f"Verified {entries} inventory entries; report: {report}", flush=True)
        return state
    except BaseException as error:
        state.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                     complete=False, error=f"{type(error).__name__}: {error}")
        write_report(report, state)
        raise


def pin_refs(repository, refs):
    if not refs or len(set(refs)) != len(refs):
        raise ValueError("Provide at least one explicit, nonduplicate history ref")
    result = {}
    for ref in refs:
        if not ref or ref.startswith("-") or any(character.isspace() for character in ref):
            raise ValueError("Invalid history ref")
        revision = git(repository, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}")
        if not OID.fullmatch(revision):
            raise ValueError("History refs must resolve to full immutable commit IDs")
        result[ref] = revision
    return result


def audit(repository, refs, *, report=None):
    """Prove the supplied histories are locally complete and fit GitHub's blob limit.

    This checks reachable object availability and unpacked blob sizes, without
    repairing, fetching, rewriting history, packing objects or changing any refs.
    It does not prove GitHub credentials, push pack sizing or server acceptance.
    """
    repository, git_directory, _ = repository_paths(repository)
    report = Path(report or ROOT / "standalone/work/publication/history-audit.json").resolve()
    if git_directory == report or git_directory in report.parents:
        raise ValueError("The history audit report must not overwrite Git metadata")
    report.parent.mkdir(parents=True, exist_ok=True)
    manifests = Path(tempfile.mkdtemp(prefix=report.stem + "-", dir=report.parent))
    enumeration = manifests / "reachable.objects"
    present = manifests / "present.objects"
    missing = manifests / "missing.objects"
    sizes = manifests / "present.types-sizes"
    log = manifests / "audit.log"
    state = {"schema": 1, "repository": str(repository), "requested_refs": list(refs),
             "manifest_directory": str(manifests), "present_manifest": str(present),
             "missing_manifest": str(missing), "type_size_manifest": str(sizes), "log": str(log),
             "status": "running", "complete": False, "publication_ready": False,
             "reachable_entries": 0, "missing_entries": 0, "present_entries": 0,
             "verified_present_entries": 0, "largest_reachable_blob": None,
             "oversized_blobs": [], "maximum_blob_bytes": MAXIMUM_GITHUB_BLOB_BYTES,
             "scope": "Pinned reachable object availability and GitHub blob size gate only"}
    write_report(report, state)
    try:
        if git(repository, "rev-parse", "--is-shallow-repository") == "true":
            raise ValueError("A shallow repository cannot prove full history completeness")
        pinned = pin_refs(repository, refs)
        state["pinned_refs"] = pinned
        state["phase"] = "enumerating_reachable_objects"
        write_report(report, state)
        command = ["git", "-C", str(repository), "rev-list", "--objects", "--missing=print",
                   "--no-object-names", *dict.fromkeys(pinned.values())]
        print(f"Enumerating {len(pinned)} pinned histories offline", flush=True)
        with log.open("wb") as diagnostics, enumeration.open("wb") as output:
            diagnostics.write(("Command: " + " ".join(command) + "\n").encode())
            diagnostics.flush()
            result = subprocess.run(command, stdout=output, stderr=diagnostics,
                                    env=offline_environment(), check=False)
        if result.returncode:
            raise RuntimeError(f"Offline history enumeration failed with status {result.returncode}; see {log}")
        with enumeration.open(encoding="ascii") as source, present.open("w", encoding="ascii") as found, \
                missing.open("w", encoding="ascii") as absent:
            for line in source:
                value = line.rstrip("\n")
                unavailable = value.startswith("?")
                oid = value.removeprefix("?")
                if not OID.fullmatch(oid):
                    raise ValueError("Malformed offline history object enumeration")
                state["reachable_entries"] += 1
                if unavailable:
                    absent.write(oid + "\n")
                    state["missing_entries"] += 1
                else:
                    found.write(oid + "\n")
                    state["present_entries"] += 1
                if state["reachable_entries"] % 100000 == 0:
                    print(f"Enumerated {state['reachable_entries']} entries; "
                          f"{state['missing_entries']} missing", flush=True)
        state["phase"] = "checking_reachable_object_sizes"
        write_report(report, state)
        # Files, rather than connected Python pipes, avoid stdin/stdout deadlock
        # and bound memory while examining millions of historical objects.
        command = ["git", "-C", str(repository), "cat-file",
                   "--batch-check=%(objectname) %(objecttype) %(objectsize)"]
        with present.open("rb") as source, sizes.open("wb") as output, log.open("ab") as diagnostics:
            diagnostics.write(("Command: " + " ".join(command) + "\n").encode())
            diagnostics.flush()
            result = subprocess.run(command, stdin=source, stdout=output, stderr=diagnostics,
                                    env=offline_environment(), check=False)
        if result.returncode:
            raise RuntimeError(f"Offline object size check failed with status {result.returncode}; see {log}")
        with present.open(encoding="ascii") as expected, sizes.open(encoding="ascii") as measured:
            for line in measured:
                fields = line.split()
                wanted = next(expected, "").strip()
                if (len(fields) != 3 or fields[0] != wanted or
                        fields[1] not in {"blob", "tree", "commit", "tag"} or not fields[2].isdecimal()):
                    raise ValueError("Reachable object disappeared or returned malformed size metadata")
                state["verified_present_entries"] += 1
                size = int(fields[2])
                if fields[1] == "blob":
                    record = {"oid": fields[0], "size_bytes": size}
                    largest = state["largest_reachable_blob"]
                    if largest is None or size > largest["size_bytes"]:
                        state["largest_reachable_blob"] = record
                    if size > MAXIMUM_GITHUB_BLOB_BYTES:
                        state["oversized_blobs"].append(record)
                if state["verified_present_entries"] % 100000 == 0:
                    print(f"Checked type and size of {state['verified_present_entries']} objects", flush=True)
            if next(expected, None) is not None:
                raise ValueError("Incomplete reachable object size check")
        state["refs_after_check"] = pin_refs(repository, refs)
        state["refs_unchanged"] = state["refs_after_check"] == pinned
        state["publication_ready"] = (state["missing_entries"] == 0 and not state["oversized_blobs"] and
                                      state["refs_unchanged"] and
                                      state["verified_present_entries"] == state["present_entries"])
        state.update(status="complete", complete=True, phase="complete")
        write_report(report, state)
        print(f"History audit: {state['missing_entries']} missing, {len(state['oversized_blobs'])} oversized "
              f"blobs; ready={state['publication_ready']}; report: {report}", flush=True)
        return state
    except BaseException as error:
        state.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                     complete=False, publication_ready=False, error=f"{type(error).__name__}: {error}")
        write_report(report, state)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("restore", help="Explicitly fetch missing historical objects")
    command.add_argument("--repository", type=Path, required=True)
    command.add_argument("--remote", required=True)
    command.add_argument("--missing", type=Path, required=True)
    command.add_argument("--batch-size", type=int, default=5000)
    command.add_argument("--report", type=Path)
    command.add_argument("--minimum-free-gib", type=float, default=20)
    command = commands.add_parser("audit", help="Check complete pinned histories offline")
    command.add_argument("--repository", type=Path, required=True)
    command.add_argument("--ref", dest="refs", action="append", required=True)
    command.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "restore":
            restore(args.repository, args.remote, args.missing, batch_size=args.batch_size,
                    report=args.report, minimum_free_gib=args.minimum_free_gib)
        else:
            result = audit(args.repository, args.refs, report=args.report)
            return 0 if result["publication_ready"] else 1
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
