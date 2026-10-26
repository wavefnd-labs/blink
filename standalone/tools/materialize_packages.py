#!/usr/bin/env python3
"""Install the selected, immutable Linux build packages; never compile sources.

Reads dependencies.lock.json and explicitly reviewed child-DEPS package pins.
GCS selected_objects have already had the profile conditions evaluated. CIPD
tags are provenance only: downloads use the frozen ObjectRef, and the complete
archive is checked before extraction.
"""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tempfile
import urllib.parse
import urllib.request
import zipfile
import tarfile

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "standalone/work/graph"
LOCK = ROOT / "standalone/config/dependencies.lock.json"
REPORT = ROOT / "standalone/reports/linux-x64-materialized-packages.json"
TOOLS_REPORT = ROOT / "standalone/reports/linux-x64-build-tools.json"
NESTED_LOCK = ROOT / "standalone/config/nested-build-packages.lock.json"


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_relative(name, allow_backslash=False):
    path = PurePosixPath(name)
    if (not name or not path.parts or ("\\" in name and not allow_backslash) or path.is_absolute()
            or ".." in path.parts or ":" in path.parts[0]):
        raise ValueError("Unsafe package path: " + name)
    return path


def contained(path, root):
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Package path escapes its destination: " + str(path))


def cipd_instance_id(instance):
    if instance.get("hashAlgo") != "SHA256":
        raise ValueError("Only frozen SHA256 CIPD instances are supported")
    digest = bytes.fromhex(instance["hexDigest"])
    if len(digest) != 32:
        raise ValueError("Invalid CIPD SHA256")
    # CIPD's modern instance ID is a raw hash followed by the SHA256 algorithm ID.
    return base64.urlsafe_b64encode(digest + b"\x02").decode().rstrip("=")


def cipd_url(package):
    request = urllib.request.Request(
        "https://chrome-infra-packages.appspot.com/prpc/cipd.Repository/GetInstanceURL",
        data=json.dumps({"package": package["resolved_package"],
                         "instance": package["instance"]}).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        result = json.loads(response.read().decode().removeprefix(")]}'\n"))
    url = result["signedUrl"]
    if urllib.parse.urlparse(url).scheme != "https":
        raise ValueError("CIPD returned an insecure URL")
    return url


def fetch(digest, url_factory, cache, size=None):
    archive = cache / digest
    cache.mkdir(parents=True, exist_ok=True)
    metadata_path = cache / (digest + ".headers.json")
    if not archive.exists():
        temporary = archive.with_suffix(".partial")
        try:
            with urllib.request.urlopen(url_factory(), timeout=90) as response:
                headers = {k.lower(): v for k, v in response.headers.items()}
                with temporary.open("wb") as output:
                    shutil.copyfileobj(response, output, 1024 * 1024)
            if size is not None and temporary.stat().st_size != size:
                raise ValueError("Package size mismatch: " + digest)
            if sha256(temporary) != digest:
                raise ValueError("Package checksum mismatch: " + digest)
            temporary.replace(archive)
            metadata_path.write_text(json.dumps(headers, sort_keys=True) + "\n")
        finally:
            temporary.unlink(missing_ok=True)
    if size is not None and archive.stat().st_size != size:
        raise ValueError("Cached package size mismatch: " + digest)
    if sha256(archive) != digest:
        raise ValueError("Cached package checksum mismatch: " + digest)
    return archive, json.loads(metadata_path.read_text()) if metadata_path.exists() else {}


def unpack_zip(archive, stage, cipd=False):
    with zipfile.ZipFile(archive) as zipped:
        for info in zipped.infolist():
            relative = safe_relative(info.filename)
            if cipd and relative.parts[0] == ".cipdpkg":
                continue
            destination = stage / str(relative)
            contained(destination, stage)
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                target = zipped.read(info).decode()
                contained(destination.parent / target, stage)
                if PurePosixPath(target).is_absolute():
                    raise ValueError("Absolute symlink in package")
                destination.symlink_to(target)
            else:
                with zipped.open(info) as source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output)
                destination.chmod(0o755 if mode & 0o111 else 0o644)


def write_cipd_version(archive, stage, package):
    # Match CIPD's manifest-declared version file, without installing client state.
    with zipfile.ZipFile(archive) as zipped:
        manifest = json.loads(zipped.read(".cipdpkg/manifest.json"))
    if manifest.get("package_name") != package["resolved_package"]:
        raise ValueError("CIPD manifest package name mismatch")
    if manifest.get("version_file"):
        output = stage / str(safe_relative(manifest["version_file"]))
        contained(output, stage)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"package_name": package["resolved_package"],
                                     "instance_id": cipd_instance_id(package["instance"])},
                                    sort_keys=True) + "\n")


def unpack(archive, stage, output_file=None, executable=False, cipd=False):
    if tarfile.is_tarfile(archive):
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                if member.isdir() and PurePosixPath(member.name) == PurePosixPath("."):
                    continue
                safe_relative(member.name, allow_backslash=True)
            tar.extractall(stage, filter="data")
        kind = "tar"
    elif zipfile.is_zipfile(archive):
        unpack_zip(archive, stage, cipd)
        kind = "zip"
    elif output_file:
        output = stage / str(safe_relative(output_file))
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(archive, output)
        output.chmod(0o755 if executable else 0o644)
        kind = "file"
    else:
        raise ValueError("Raw GCS object requires output_file")
    # gclient retains the named download as well as extracting archive members.
    if output_file and kind != "file":
        output = stage / str(safe_relative(output_file))
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists() and sha256(output) != sha256(archive):
            raise ValueError("Named artifact collides with extracted member")
        shutil.copyfile(archive, output)
        output.chmod(0o644)
    return kind


def install(stage, destination, boundary=None):
    """Merge exact package bytes without following destination symlinks."""
    boundary = destination.parent if boundary is None else boundary
    if not destination.absolute().is_relative_to(boundary.absolute()):
        raise ValueError("Package destination is outside its source workspace")
    for ancestor in (destination, *destination.parents):
        if ancestor.is_symlink():
            raise ValueError("Refusing package destination symlink: " + str(ancestor))
        if ancestor == boundary:
            break
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    records = []
    # Directories precede files, and symlink directories are never traversed.
    paths = sorted(stage.rglob("*"), key=lambda p: (len(p.relative_to(stage).parts), str(p)))
    for source in paths:
        relative = source.relative_to(stage)
        target = destination / relative
        contained(target.parent, root)
        if source.is_symlink():
            link = os.readlink(source)
            contained(target.parent / link, root)
            if target.is_symlink():
                if os.readlink(target) != link:
                    raise ValueError("Installed package symlink differs: " + str(target))
            elif target.exists():
                raise ValueError("Installed package path differs: " + str(target))
            else:
                target.symlink_to(link)
            records.append({"path": relative.as_posix(), "symlink": link})
        elif source.is_dir():
            if target.is_symlink():
                raise ValueError("Refusing package directory symlink: " + str(target))
            target.mkdir(exist_ok=True)
        elif source.is_file():
            digest = sha256(source)
            mode = 0o755 if source.stat().st_mode & 0o111 else 0o644
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise ValueError("Installed package path differs: " + str(target))
            if target.exists():
                if sha256(target) != digest:
                    raise ValueError("Installed package file differs: " + str(target))
            else:
                shutil.copyfile(source, target)
            target.chmod(mode)
            records.append({"path": relative.as_posix(), "sha256": digest,
                            "executable": bool(mode & 0o111)})
        else:
            raise ValueError("Unsupported package member: " + str(source))
    return records


def materialize(name, entry, work=WORK):
    safe_relative(name)
    destination = work / "src" / name
    cache = work / "packages"
    artifacts = []
    selected = entry.get("selected_objects") if entry["dep_type"] == "gcs" else entry["packages"]
    if not selected:
        raise ValueError("Package has no frozen selected artifacts: " + name)
    for obj in selected:
        if entry["dep_type"] == "gcs":
            if obj not in entry["objects"]:
                raise ValueError("Selected object differs from declared object")
            digest = obj["sha256sum"]
            url = ("https://storage.googleapis.com/" + urllib.parse.quote(entry["bucket"], safe="")
                   + "/" + urllib.parse.quote(obj["object_name"], safe="/")
                   + "?generation=" + str(int(obj["generation"])))
            archive, headers = fetch(digest, lambda: url, cache, obj["size_bytes"])
            if (not headers and obj.get("output_file")
                    and not tarfile.is_tarfile(archive) and not zipfile.is_zipfile(archive)):
                # A cached raw download must not lose GCS's executable metadata.
                request = urllib.request.Request(url, method="HEAD")
                with urllib.request.urlopen(request, timeout=60) as response:
                    headers = {key.lower(): value for key, value in response.headers.items()}
                (cache / (digest + ".headers.json")).write_text(json.dumps(headers, sort_keys=True) + "\n")
            identity = {"bucket": entry["bucket"], "object": obj}
        else:
            digest = obj["instance"]["hexDigest"]
            cipd_instance_id(obj["instance"])
            archive, headers = fetch(digest, lambda: cipd_url(obj), cache)
            identity = {"package": obj["resolved_package"], "instance": obj["instance"]}
        print("Verified package: " + name + " " + digest, flush=True)
        with tempfile.TemporaryDirectory(prefix="package-", dir=cache) as temporary:
            stage = Path(temporary)
            kind = unpack(archive, stage, obj.get("output_file"),
                          headers.get("x-goog-meta-executable") == "1", entry["dep_type"] == "cipd")
            if entry["dep_type"] == "cipd":
                write_cipd_version(archive, stage, obj)
            records = install(stage, destination, work / "src")
        manifest = json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
        manifest_path = cache / (digest + ".installed.json")
        manifest_path.write_bytes(manifest)
        artifacts.append({**identity, "sha256": digest, "size_bytes": archive.stat().st_size,
                          "archive_kind": kind, "installed_entries": len(records),
                          "installed_manifest_sha256": hashlib.sha256(manifest).hexdigest()})
        print("Installed and verified: " + name + " (" + str(len(records)) + " entries)", flush=True)
    result = {"dep_type": entry["dep_type"], "destination": "standalone/work/graph/src/" + name,
              "artifacts": artifacts}
    if entry.get("parent_repository"):
        result["provenance"] = {key: entry[key] for key in ("parent_repository", "parent_deps_key", "required_by")}
    return name, result


def selected_packages(lock, lock_bytes, nested_path=None):
    """Merge explicit nested package pins after checking their original DEPS."""
    entries = {name: entry for name, entry in lock["dependencies"].items()
               if entry.get("selected_for_profile") and entry["dep_type"] in ("gcs", "cipd")}
    nested_path = NESTED_LOCK if nested_path is None else nested_path
    if not nested_path.exists():
        return entries, {}
    nested_bytes = nested_path.read_bytes()
    nested = json.loads(nested_bytes)
    if (nested.get("schema") != 1 or nested.get("chromium_revision") != lock["chromium_revision"]
            or nested.get("root_lock_sha256") != hashlib.sha256(lock_bytes).hexdigest()
            or nested.get("profile") != lock["profile"]["name"] or nested.get("hooks_executed") is not False):
        raise ValueError("Nested package lock does not match the pinned root/profile")
    spec = importlib.util.spec_from_file_location("nested_package_deps", ROOT / "standalone/tools/dependencies.py")
    dependency_parser = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dependency_parser)
    parents = {}
    for name, parent in nested["parents"].items():
        original = lock["dependencies"].get(name, {})
        if (original.get("dep_type") != "git" or original.get("repository") != parent["repository"]
                or original.get("revision") != parent["revision"]):
            raise ValueError("Nested package parent Git pin differs: " + name)
        preserved = lock["preserved_upstream_inputs"].get(parent["deps_file"], {})
        if (preserved.get("revision") != parent["revision"]
                or preserved.get("sha256") != parent["deps_sha256"]
                or preserved.get("original_path") != "DEPS"):
            raise ValueError("Nested parent DEPS provenance differs: " + name)
        deps_data = (ROOT / str(safe_relative(parent["deps_file"]))).read_bytes()
        if hashlib.sha256(deps_data).hexdigest() != parent["deps_sha256"]:
            raise ValueError("Nested parent DEPS bytes changed: " + name)
        parsed = dependency_parser.parse_deps(deps_data.decode())
        if parsed.get("use_relative_paths") is not True:
            raise ValueError("Nested DEPS must declare relative paths: " + name)
        parents[name] = parsed
    for name, entry in nested["dependencies"].items():
        parent = entry["parent_repository"]
        key = entry["parent_deps_key"]
        safe_relative(key)
        if name != parent + "/" + key or name in entries:
            raise ValueError("Nested package destination collides or differs: " + name)
        original = parents[parent]["deps"][key]
        if (not entry.get("selected_for_profile") or entry["dep_type"] not in ("cipd", "gcs")
                or any(entry.get(field) != original.get(field)
                       for field in ("dep_type", "condition", "bucket", "objects"))):
            raise ValueError("Nested package differs from its original DEPS: " + name)
        if entry["dep_type"] == "cipd":
            if len(entry["packages"]) != len(original["packages"]):
                raise ValueError("Nested package count differs from DEPS: " + name)
            for frozen, declared in zip(entry["packages"], original["packages"]):
                expanded = declared["package"].replace("${{platform}}", "linux-amd64").replace("${{arch}}", "amd64")
                if (frozen["package"] != declared["package"] or frozen["version"] != declared["version"]
                        or frozen["resolved_package"] != expanded or "$" in expanded):
                    raise ValueError("Nested package reference differs from DEPS: " + name)
                cipd_instance_id(frozen["instance"])
        elif not entry.get("selected_objects") or any(obj not in original["objects"] for obj in entry["selected_objects"]):
            raise ValueError("Nested GCS selection differs from DEPS: " + name)
        entries[name] = entry
    return entries, {"lock_path": "standalone/config/nested-build-packages.lock.json",
                     "lock_sha256": hashlib.sha256(nested_bytes).hexdigest(),
                     "parents": nested["parents"], "package_roots": sorted(nested["dependencies"])}


def check_tools(view=None, report_path=None):
    """Run version probes only; inspect generated paths without executing builds."""
    lock_bytes = LOCK.read_bytes()
    lock = json.loads(lock_bytes)
    source = WORK / "src"
    tools = {
        "clang": source / "third_party/llvm-build/Release+Asserts/bin/clang",
        "clang++": source / "third_party/llvm-build/Release+Asserts/bin/clang++",
        "ld.lld": source / "third_party/llvm-build/Release+Asserts/bin/ld.lld",
        "rustc": source / "third_party/rust-toolchain/bin/rustc",
        "cargo": source / "third_party/rust-toolchain/bin/cargo",
        "node": source / "third_party/node/linux/node-linux-x64/bin/node",
        "ninja": source / "third_party/ninja/ninja",
        "mold": source / "buildtools/third_party/mold/cipd/mold",
        "gperf": source / "third_party/gperf/cipd/bin/gperf",
        "esbuild": source / "third_party/devtools-frontend/src/third_party/esbuild/esbuild",
        "go": source / "third_party/dawn/tools/golang/linux-amd64/bin/go",
        "gn": WORK / "tools/gn",
        "python": Path(lock["profile"]["script_executable"]),
        "pkg-config": Path(shutil.which("pkg-config") or "/usr/bin/pkg-config"),
    }
    # All probes only report versions, never compile a source or write output.
    results = {}
    for name, path in tools.items():
        path = Path(os.path.normpath(path))
        item = {"path": str(path), "command": [str(path), "version" if name == "go" else "--version"],
                "required_for_profile": name != "cargo"}
        try:
            item["sha256"] = sha256(path)
            process = subprocess.run(item["command"], capture_output=True, text=True,
                                     timeout=30, check=False)
            item.update(exit_code=process.returncode, stdout=process.stdout.strip(),
                        stderr=process.stderr.strip(), executable_verified=process.returncode == 0)
        except (OSError, subprocess.TimeoutExpired) as error:
            item.update(error=str(error), executable_verified=False)
        results[name] = item
    current = ROOT / "standalone/work/engine/current.json"
    references = {}
    if view is None and current.exists():
        view = Path(json.loads(current.read_text())["view"])
    if view is not None:
        view = Path(view)
        output = view / "out/Engine"
        main_ninja = (output / "build.ninja").read_text(errors="replace")
        toolchain = (output / "toolchain.ninja").read_text(errors="replace")
        needles = {
            "clang": "../../third_party/llvm-build/Release+Asserts/bin/clang",
            "clang++": "../../third_party/llvm-build/Release+Asserts/bin/clang++",
            "rustc": "../../third_party/rust-toolchain/bin/rustc",
            "node": "../../third_party/node/linux/node-linux-x64/bin/node",
            "gperf": "../../third_party/gperf/cipd/bin/gperf",
            "esbuild": "../../third_party/devtools-frontend/src/third_party/esbuild/esbuild",
            "mold": "../../buildtools/third_party/mold/cipd/mold",
            "python": lock["profile"]["script_executable"],
        }
        for name, needle in needles.items():
            path = Path(needle) if Path(needle).is_absolute() else output / needle
            references[name] = {"expected_reference": needle,
                                "found": needle in toolchain,
                                "resolves_to_probed_tool": path.resolve() == tools[name].resolve()}
        gn_commands = [line.strip() for line in main_ninja.splitlines()
                       if "command = " in line and "graph/tools/gn " in line]
        references["gn"] = {"commands": gn_commands, "found": bool(gn_commands),
                            "resolves_to_probed_tool": bool(gn_commands) and all(
                                (output / command.split()[2]).resolve() == tools["gn"].resolve()
                                for command in gn_commands)}
        cargo_references = [str(path.relative_to(output)) for path in output.rglob("*.ninja")
                            if b"rust-toolchain/bin/cargo" in path.read_bytes()]
        results["cargo"]["required_for_profile"] = bool(cargo_references)
        results["cargo"]["ninja_reference_files"] = cargo_references
        results["cargo"]["scope"] = "Optional development tool when absent from generated Ninja; GN invokes rustc directly"
        tint_script = view / "third_party/dawn/tools/generate-sources-gn.py"
        tint_reference = "../../third_party/dawn/tools/generate-sources-gn.py"
        references["go"] = {
            "expected_reference": tint_reference,
            "found": tint_reference in toolchain,
            "scope": "Tint's Python action selects the Dawn-pinned Go executable relative to its real Dawn root",
            "resolves_to_probed_tool": (
                tint_script.resolve().parents[1] / "tools/golang/linux-amd64/bin/go"
            ).resolve() == tools["go"].resolve(),
        }
    else:
        references["error"] = "Engine view must be generated before tool-path checks"
    report = {"schema": 1, "chromium_revision": lock["chromium_revision"],
              "profile": lock["profile"]["name"],
              "lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
              "compiled": False, "linked": False,
              "probe_scope": "Executable version probes and generated Ninja path inspection only",
              "tools": results, "ninja_tool_references": references,
              "all_tools_executable": all(x["executable_verified"] for x in results.values()),
              "required_tools_executable": all(x["executable_verified"] for x in results.values()
                                               if x["required_for_profile"]),
              "optional_tools_unavailable": [name for name, item in results.items()
                                             if not item["required_for_profile"] and not item["executable_verified"]],
              "gn_hash_matches_profile": results["gn"].get("sha256") == lock["profile"]["gn_sha256"],
              "limitations": ["Version probes do not compile or link engine sources.",
                              "Cargo availability is reported separately when the selected Ninja graph does not invoke it."],
              "ninja_paths_verified": "error" not in references and all(
                  x.get("found") and x.get("resolves_to_probed_tool", True) for x in references.values())}
    report_path = Path(report_path or TOOLS_REPORT)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="*", help="Selected package roots; default: all")
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--check-tools", action="store_true", help="Only run version probes and inspect Ninja tool paths")
    parser.add_argument("--view", type=Path, help="Inspect this generated view for --check-tools")
    parser.add_argument("--tools-report", type=Path, help="Write --check-tools evidence to this isolated path")
    args = parser.parse_args(argv)
    if args.check_tools:
        report = check_tools(view=args.view, report_path=args.tools_report)
        print(json.dumps({"all_tools_executable": report["all_tools_executable"],
                          "required_tools_executable": report["required_tools_executable"],
                          "optional_tools_unavailable": report["optional_tools_unavailable"],
                          "ninja_paths_verified": report["ninja_paths_verified"],
                          "report": str(args.tools_report or TOOLS_REPORT)}, indent=2))
        return 0 if (report["required_tools_executable"] and report["ninja_paths_verified"]
                     and report["gn_hash_matches_profile"]) else 1
    if args.view is not None or args.tools_report is not None:
        parser.error("--view and --tools-report are only valid with --check-tools")
    lock_bytes = LOCK.read_bytes()
    lock = json.loads(lock_bytes)
    entries, supplemental = selected_packages(lock, lock_bytes)
    for name in args.names:
        if name not in entries:
            parser.error("Not a selected package: " + name)
    all_selected_names = set(entries)
    if args.names:
        entries = {name: entries[name] for name in args.names}
    report = {"schema": 1, "chromium_revision": lock["chromium_revision"],
              "profile": lock["profile"]["name"], "lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
              "compiled": False, "packages": {}, "supplemental": supplemental}
    if args.names and REPORT.exists():
        previous = json.loads(REPORT.read_text())
        if previous.get("lock_sha256") == report["lock_sha256"]:
            same_nested = previous.get("supplemental", {}).get("lock_sha256") == supplemental.get("lock_sha256")
            report["packages"] = {name: value for name, value in previous["packages"].items()
                                  if name in all_selected_names and (same_nested or name in lock["dependencies"])}
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {name: pool.submit(materialize, name, entry) for name, entry in entries.items()}
        for name, future in futures.items():
            try:
                _, result = future.result()
                report["packages"][name] = result
            except Exception as error:
                report["packages"][name] = {"error": str(error)}
    report["complete"] = (set(report["packages"]) == all_selected_names
                          and all("error" not in e for e in report["packages"].values()))
    REPORT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"complete": report["complete"], "packages": len(report["packages"]),
                      "report": str(REPORT)}, indent=2))
    return 0 if all("error" not in e for e in report["packages"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
