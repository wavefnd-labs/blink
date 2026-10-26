#!/usr/bin/env python3
"""Restore pinned engine build inputs without compilation or general DEPS hooks."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
SUPPORT = ROOT / 'standalone'
WORK = SUPPORT / 'work/graph'
SRC = WORK / 'src'
BROWSER_ROOTS = {'chrome', 'content', 'headless', 'android_webview', 'ios'}


def read_json(path):
    return json.loads(path.read_text())


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def git(cache, *args, **kwargs):
    return subprocess.check_output(['git', '-c', 'credential.helper=', '--git-dir=' + str(cache), *args], **kwargs)


def tree_entries(cache, revision, *, no_lazy_fetch=False):
    result = []
    options = {'env': {**os.environ, 'GIT_NO_LAZY_FETCH': '1'}} if no_lazy_fetch else {}
    for row in git(cache, 'ls-tree', '-r', '-z', revision, **options).split(b'\0'):
        if not row:
            continue
        info, name = row.split(b'\t', 1)
        mode, kind, oid = info.split()
        if kind == b'blob':
            name = os.fsdecode(name)
            path = PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts or not path.parts:
                raise ValueError('Unsafe upstream tree path: ' + name)
            result.append((mode.decode(), oid.decode(), name))
    return result


def blob_digest(path):
    """Hash existing bytes using Git's blob framing, including symlink text."""
    if path.is_symlink():
        data = os.fsencode(os.readlink(path))
        return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
    length = path.stat().st_size
    digest = hashlib.sha1(b'blob ' + str(length).encode() + b'\0')
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def check_parents(destination, name):
    """Never materialize through a source/package link into another tree."""
    current = destination
    if current.is_symlink():
        raise ValueError('Source destination is a symlink: ' + str(current))
    for part in PurePosixPath(name).parts[:-1]:
        current /= part
        if current.is_symlink():
            raise ValueError('Source parent is a symlink: ' + str(current))


def validate_existing(path, mode, oid):
    if mode == '120000':
        valid_type = path.is_symlink()
    else:
        valid_type = path.is_file() and not path.is_symlink()
    if not valid_type or blob_digest(path) != oid:
        raise ValueError('Existing upstream input differs; preserve and review it: ' + str(path))
    if mode != '120000' and bool(path.stat().st_mode & stat.S_IXUSR) != (mode == '100755'):
        raise ValueError('Existing upstream executable mode differs: ' + str(path))


def install_file(target, mode, data=None, source=None):
    """Publish complete bytes atomically; interruption cannot leave a torn blob."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.blink-source-', dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            if data is not None:
                stream.write(data)
            else:
                import shutil
                with source.open('rb') as original:
                    shutil.copyfileobj(original, stream)
            stream.flush()
        temporary.chmod(0o755 if mode == '100755' else 0o644)
        os.link(temporary, target)  # Exclusive publication; never replace local bytes.
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def fetch_blobs(cache, oids, label):
    env = {**os.environ, 'GIT_NO_LAZY_FETCH': '1'}
    if not oids:
        return 0
    checks = git(cache, 'cat-file', '--batch-check=%(objectname) %(objecttype)',
                 input=('\n'.join(sorted(oids)) + '\n').encode(), env=env)
    missing = [row.split()[0] for row in checks.splitlines() if row.endswith(b' missing')]
    print(f'{label}: {len(oids)} distinct missing-file blobs, {len(missing)} need downloading', flush=True)
    for offset in range(0, len(missing), 1500):
        batch = missing[offset:offset + 1500]
        command = ['git', '-c', 'credential.helper=', '-c', 'fetch.negotiationAlgorithm=noop',
                   '--git-dir=' + str(cache), 'fetch', 'origin', '--no-tags', '--no-write-fetch-head',
                   '--recurse-submodules=no', '--filter=blob:none', '--stdin']
        for attempt in range(3):
            result = subprocess.run(command, input=b'\n'.join(batch) + b'\n',
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if not result.returncode:
                break
            if attempt == 2:
                raise RuntimeError(f'{label}: pinned blob download failed: ' + result.stderr.decode(errors='replace')[-3000:])
            time.sleep(2 * (attempt + 1))
        print(f'{label}: fetched {offset + len(batch)}/{len(missing)} blobs', flush=True)
    return len(missing)


def materialize_tree(cache, revision, destination, entries, label):
    missing, existing = [], 0
    for index, (mode, oid, name) in enumerate(entries, 1):
        check_parents(destination, name)
        path = destination / name
        if os.path.lexists(path):
            validate_existing(path, mode, oid)
            existing += 1
        else:
            missing.append((mode, oid, name))
        if index % 10000 == 0:
            print(f'{label}: checked {index}/{len(entries)} source paths', flush=True)
    downloaded = fetch_blobs(cache, {oid for _, oid, _ in missing}, label)
    env = {**os.environ, 'GIT_NO_LAZY_FETCH': '1'}
    process = subprocess.Popen(['git', '--git-dir=' + str(cache), 'cat-file', '--batch'],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
    restored_bytes = 0
    try:
        for mode, oid, name in missing:
            process.stdin.write(oid.encode() + b'\n')
            process.stdin.flush()
            header = process.stdout.readline().split()
            if len(header) != 3 or header[1] != b'blob' or header[0].decode() != oid:
                raise ValueError('Pinned blob unavailable: ' + name)
            data = process.stdout.read(int(header[2]))
            if process.stdout.read(1) != b'\n' or hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() != oid:
                raise ValueError('Pinned blob transfer failed: ' + name)
            check_parents(destination, name)
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if os.path.lexists(target):
                validate_existing(target, mode, oid)
            elif mode == '120000':
                target.symlink_to(os.fsdecode(data))
            else:
                install_file(target, mode, data=data)
            restored_bytes += len(data)
    finally:
        process.stdin.close()
        process.stdout.close()
        process.wait()
    # The inventory can be regenerated from the immutable commit; its digest
    # records exactly which files this conservative source closure selected.
    inventory = ''.join(f'{mode} {oid}\t{name}\n' for mode, oid, name in entries).encode()
    print(f'{label}: verified {existing}, restored {len(missing)} files ({restored_bytes} bytes)', flush=True)
    return {'revision': revision, 'tree_file_count': len(entries), 'verified_existing': existing,
            'restored_files': len(missing), 'restored_bytes': restored_bytes,
            'fetched_blobs': downloaded, 'inventory_sha256': hashlib.sha256(inventory).hexdigest()}


def source_tops(graph):
    tops = {'build', 'buildtools', 'build_overrides', 'third_party', 'tools'}
    for node in graph.values():
        for field in ('sources', 'inputs', 'data', 'script', 'include_dirs'):
            values = node.get(field, [])
            for value in [values] if isinstance(values, str) else values:
                if value.startswith('//') and value[2:]:
                    tops.add(value[2:].split('/', 1)[0])
    if tops & BROWSER_ROOTS:
        raise ValueError('Engine requests browser source directories: ' + repr(tops & BROWSER_ROOTS))
    return tops - {'standalone', 'out'}


def main_entries(entries, tops):
    # Whole library/support subtrees include headers and Python/resource imports
    # which GN does not enumerate. No browser roots and no pristine Blink writes.
    return [(mode, oid, name) for mode, oid, name in entries
            if (('/' not in name or name.split('/', 1)[0] in tops)
                and name.split('/', 1)[0] not in BROWSER_ROOTS
                and not name.startswith('third_party/blink/'))]


def dependency(name, entry, use_archives=True):
    if name.split('/', 1)[0] in BROWSER_ROOTS:
        raise ValueError('Browser dependency is forbidden: ' + name)
    cache = WORK / 'deps-cache' / (name.replace('/', '_') + '.git')
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['git', 'init', '--bare', '--quiet', str(cache)], check=True)
    git(cache, 'config', 'remote.origin.url', entry['repository'])
    git(cache, 'config', 'remote.origin.promisor', 'true')
    git(cache, 'config', 'remote.origin.partialclonefilter', 'blob:none')
    try:
        git(cache, 'cat-file', '-e', entry['revision'] + '^{commit}',
            env={**os.environ, 'GIT_NO_LAZY_FETCH': '1'}, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        print('Fetching dependency commit: ' + name, flush=True)
        git(cache, 'fetch', '--filter=blob:none', '--depth=1', '--no-tags', 'origin', entry['revision'])
    destination = SRC / name
    check_parents(SRC, name + '/.git')
    destination.mkdir(parents=True, exist_ok=True)
    git(cache, 'config', 'core.bare', 'false')
    git(cache, 'config', 'core.worktree', str(destination))
    git(cache, 'update-ref', 'refs/heads/pinned', entry['revision'])
    git(cache, 'symbolic-ref', 'HEAD', 'refs/heads/pinned')
    binding = destination / '.git'
    expected = 'gitdir: ' + str(cache) + '\n'
    if os.path.lexists(binding) and (binding.is_symlink() or not binding.is_file() or binding.read_text() != expected):
        raise ValueError('Unexpected source Git binding: ' + str(binding))
    binding.write_text(expected)
    # Root DEPS is authoritative. Standalone vendor gitlinks often use other
    # pins/layouts; never recursively check out those nested gitlinks.
    entries = tree_entries(cache, entry['revision'])
    # The immutable Git tree still verifies every byte. A single official
    # archive avoids thousands of round trips for large source repositories.
    result = None
    if use_archives and len(entries) > 1500 and any(not os.path.lexists(destination / item[2]) for item in entries):
        import source_archive
        try:
            result = source_archive.restore_archive(entry['repository'], entry['revision'],
                                                     destination, entries, WORK, name)
        except source_archive.ArchiveUnavailable as error:
            print(f'{name}: {error}; falling back to pinned Git blobs', flush=True)
    if result is None:
        result = materialize_tree(cache, entry['revision'], destination, entries, name)
    result['repository'] = entry['repository']
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs', type=int, default=3, choices=range(1, 9))
    parser.add_argument('--sources-only', action='store_true', help='Leave tool/resource package installation to a separate invocation')
    parser.add_argument('--git-blobs', action='store_true', help='Use Git blob transport even for large official dependency trees')
    parser.add_argument('--plan', action='store_true', help='Print the pinned source plan without downloads or writes')
    args = parser.parse_args()
    lock = read_json(SUPPORT / 'config/dependencies.lock.json')
    upstream = read_json(SUPPORT / 'config/upstream.lock.json')
    if lock['chromium_revision'] != upstream['revision']:
        raise ValueError('Source and dependency revisions differ')
    if args.plan:
        import gzip
        evidence = read_json(SUPPORT / 'reports/linux-x64-engine-entry.json')
        graph_bytes = gzip.decompress((SUPPORT / 'reports/linux-x64-engine-graph.json.gz').read_bytes())
        if hashlib.sha256(graph_bytes).hexdigest() != evidence['graph_sha256']:
            raise ValueError('Recorded engine graph changed')
        graph = json.loads(graph_bytes)
        import materialize_implicit_sources
        implicit_lock, _, _, _ = materialize_implicit_sources.load_selection()
        print(json.dumps({'chromium_revision': upstream['revision'], 'main_source_tops': sorted(source_tops(graph)),
                          'full_git_dependencies': sorted(name for name, dep in lock['dependencies'].items() if dep['dep_type'] == 'git'),
                          'implicit_header_git_dependencies': sorted(implicit_lock['dependencies']),
                          'implicit_root_headers': sorted(implicit_lock.get('main_headers', {})),
                          'additional_build_infrastructure': ['third_party/depot_tools'],
                          'excluded_browser_roots': sorted(BROWSER_ROOTS)}, indent=2))
        return 0
    subprocess.run([sys.executable, '-B', str(SUPPORT / 'tools/engine.py'), 'gen'], check=True)
    view = Path(read_json(SUPPORT / 'work/engine/current.json')['view'])
    graph = read_json(view.parent / 'graph.json')
    spec = importlib.util.spec_from_file_location('pinned_hydration', SUPPORT / 'tools/graph/hydrate.py')
    hydration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hydration)
    declared = hydration.DEPS
    deps = {name: dep for name, dep in lock['dependencies'].items() if dep['dep_type'] == 'git'}
    # The pinned build scripts import depot_tools Python modules, not separately
    # evaluated target labels. Keep its exact root-DEPS pin explicit in evidence.
    deps['third_party/depot_tools'] = declared['src/third_party/depot_tools']
    for name, dep in deps.items():
        original = declared.get('src/' + name, {})
        if dep['revision'] != original.get('revision') or dep['repository'] != original.get('repository'):
            raise ValueError('Dependency differs from original root DEPS: ' + name)
    tops = source_tops(graph)
    entries = main_entries(tree_entries(hydration.CACHE, upstream['revision']), tops)
    report_path = SUPPORT / 'reports/linux-x64-materialized-sources.json'
    report = {'schema': 1, 'chromium_revision': upstream['revision'], 'complete': False,
              'compiled': False, 'main_source_tops': sorted(tops), 'excluded_browser_roots': sorted(BROWSER_ROOTS),
              'policy': 'Full selected Git trees and conservative main-library/support subtrees; root DEPS pins only; original Git blobs verify source bytes even with official archive transport; no recursive vendor gitlinks or hooks',
              'git_dependencies': {}, 'errors': {}}
    write_json(report_path, report)
    work = {'chromium': lambda: materialize_tree(hydration.CACHE, upstream['revision'], SRC, entries, 'chromium')}
    work.update({name: (lambda name=name, dep=dep: dependency(name, dep, not args.git_blobs)) for name, dep in deps.items()})
    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = {executor.submit(function): name for name, function in work.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                value = future.result()
                if name == 'chromium':
                    report['chromium'] = value
                else:
                    report['git_dependencies'][name] = value
            except Exception as error:
                report['errors'][name] = str(error)
                print(f'{name}: ERROR: {error}', flush=True)
            write_json(report_path, report)
    report['complete'] = not report['errors'] and len(report['git_dependencies']) == len(deps) and 'chromium' in report
    write_json(report_path, report)
    if not report['complete']:
        raise ValueError('Source materialization incomplete; see ' + str(report_path))
    subprocess.run([sys.executable, '-B', str(SUPPORT / 'tools/materialize_implicit_sources.py'),
                    *(['--git-blobs'] if args.git_blobs else [])], check=True)
    subprocess.run([sys.executable, '-B', str(SUPPORT / 'tools/materialize_nested_sources.py')], check=True)
    if not args.sources_only:
        subprocess.run([sys.executable, '-B', str(SUPPORT / 'tools/materialize_packages.py')], check=True)
    subprocess.run([sys.executable, '-B', str(SUPPORT / 'tools/prepare_metadata.py')], check=True)
    subprocess.run([sys.executable, '-B', str(SUPPORT / 'tools/engine.py'), 'prepare'], check=True)
    print('Pinned source inputs restored. No compilation has been run.', flush=True)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print('error:', error, file=sys.stderr)
        sys.exit(2)
