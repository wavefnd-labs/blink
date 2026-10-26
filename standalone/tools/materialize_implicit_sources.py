#!/usr/bin/env python3
"""Restore reviewed root-DEPS repositories and root-tree implicit headers.

GN/Ninja declared source leaves do not enumerate every transitive C++ include.
These explicit supplements preserve that graph baseline and never add targets,
rewrite upstream headers, execute hooks or synchronize vendor child DEPS.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

import dependencies
import materialize

ROOT = Path(__file__).resolve().parents[2]
SUPPORT = ROOT / 'standalone'
LOCK = SUPPORT / 'config/implicit-source-dependencies.lock.json'
REPORT = SUPPORT / 'reports/linux-x64-materialized-implicit-sources.json'
PROFILE_CONDITIONS = {'checkout_android': False, 'checkout_chromeos': False,
                      'checkout_linux': True}


def safe_relative(name):
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or not path.parts or '..' in path.parts
            or str(path) != name or '\\' in name
            or path.parts[0] in materialize.BROWSER_ROOTS):
        raise ValueError('Unsafe implicit source path: ' + name)
    return name


def git_rows(tree):
    rows = {}
    for row in tree.split(b'\0'):
        if row:
            metadata, name = row.split(b'\t', 1)
            mode, kind, oid = metadata.decode().split()
            rows[os.fsdecode(name)] = {'mode': mode, 'type': kind, 'oid': oid}
    return rows


def load_selection(root=ROOT):
    """Validate exact root-DEPS selection without fetching a commit or blob."""
    support = root / 'standalone'
    lock_path = support / 'config/implicit-source-dependencies.lock.json'
    lock_bytes = lock_path.read_bytes()
    lock = json.loads(lock_bytes)
    baseline = materialize.read_json(support / 'config/dependencies.lock.json')
    upstream = materialize.read_json(support / 'config/upstream.lock.json')
    if (lock.get('schema') != 1 or lock['chromium_revision'] != upstream['revision']
            or lock['chromium_revision'] != baseline['chromium_revision']
            or lock['profile'] != baseline['profile']['name']):
        raise ValueError('Implicit source revision/profile differs from the baseline')
    original_deps = dependencies.inventory(support / 'upstream/chromium/DEPS',
                                           lock['chromium_revision'])
    if (lock['root_deps_sha256'] != original_deps['deps_sha256']
            or lock['root_deps_sha256'] != baseline['root_deps_sha256']):
        raise ValueError('Implicit source original root DEPS hash differs')
    for name, entry in lock['dependencies'].items():
        validate_selection(name, entry, baseline, original_deps)
    return lock, baseline, original_deps, hashlib.sha256(lock_bytes).hexdigest()


def validate_selection(name, entry, baseline, original_deps):
    safe_relative(name)
    if name in baseline['dependencies']:
        raise ValueError('Implicit supplement must not override the selected baseline: ' + name)
    declared = original_deps['dependencies'].get('src/' + name, {})
    if (entry.get('root_deps_declared') is not True or entry.get('dep_type') != 'git'
            or declared.get('dep_type') != 'git'
            or entry['revision'] != declared.get('revision')
            or entry['repository'] != declared.get('repository')
            or not re.fullmatch(r'[0-9a-f]{40}', entry['revision'])
            or not entry['repository'].startswith('https://chromium.googlesource.com/')):
        raise ValueError('Implicit source differs from the original official root DEPS pin: ' + name)
    condition = declared.get('condition')
    selection = entry.get('condition_selection')
    if not condition:
        if selection is not None:
            raise ValueError('Unconditional implicit source has an invented condition selection')
        return
    if (not isinstance(selection, dict) or selection.get('expression') != condition
            or selection.get('profile') != 'linux-x64-release-static'
            or selection.get('variables') != PROFILE_CONDITIONS
            or any(type(value) is not bool for value in selection.get('variables', {}).values())
            or selection.get('selected') is not True):
        raise ValueError('Conditional implicit source needs the reviewed Linux profile selection: ' + name)

    def evaluate(node):
        if isinstance(node, ast.Name) and node.id in PROFILE_CONDITIONS:
            return PROFILE_CONDITIONS[node.id]
        if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            values = [evaluate(value) for value in node.values]
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not evaluate(node.operand)
        raise ValueError('Unsupported implicit source profile condition: ' + condition)

    try:
        expression = ast.parse(condition, mode='eval').body
    except SyntaxError as error:
        raise ValueError('Invalid implicit source profile condition: ' + condition) from error
    if not evaluate(expression):
        raise ValueError('Implicit source condition is false for the Linux profile: ' + name)


def validate_provenance(name, entry, root_tree, source):
    """Check original Gitlink and the byte-preserved production include chain."""
    safe_relative(name)
    rows = git_rows(root_tree)
    expected = {'mode': '160000', 'type': 'commit', 'oid': entry['revision']}
    link = entry['original_gitlink']
    if (link.get('path') != name or rows.get(name) != expected
            or {key: link.get(key) for key in expected} != expected):
        raise ValueError('Implicit source differs from the original Chromium Gitlink')
    required = entry['required_inputs']
    for relative in required:
        safe_relative(relative)
    chain = entry['required_by_chain']
    if (len(chain) < 2 or set(chain[:-1]) != set(entry['original_include_inputs'])
            or not chain[-1].startswith(name + '/')
            or chain[-1][len(name) + 1:] not in required):
        raise ValueError('Implicit source needs an exact reviewed production include chain')
    validate_include_chain(chain, entry['original_include_inputs'], rows, source)


def validate_include_chain(chain, originals, rows, source):
    """Bind each explicit include edge to the pinned Chromium Git tree."""
    for index, relative in enumerate(chain[:-1]):
        safe_relative(relative)
        safe_relative(chain[index + 1])
        original = originals[relative]
        path = source / relative
        materialize.check_parents(source, relative)
        if (not path.is_file() or path.is_symlink()
                or hashlib.sha256(path.read_bytes()).hexdigest() != original['sha256']
                or materialize.blob_digest(path) != original['git_blob']
                or rows.get(relative) != {'mode': '100644', 'type': 'blob',
                                          'oid': original['git_blob']}):
            raise ValueError('Implicit include provenance differs from the original Git blob: ' + relative)
        include = re.compile(r'^\s*#\s*include\s*[<"]' + re.escape(chain[index + 1]) + r'[>"]', re.MULTILINE)
        if not include.search(path.read_text()):
            raise ValueError('Reviewed production include edge is absent: ' + relative)


def validate_main_header(name, entry, root_tree, source):
    """Select one original root-tree header without syncing its whole subtree."""
    safe_relative(name)
    if name.startswith('third_party/blink/') or PurePosixPath(name).suffix != '.h':
        raise ValueError('Implicit root file must be an auxiliary upstream header: ' + name)
    rows = git_rows(root_tree)
    expected = {'mode': '100644', 'type': 'blob', 'oid': entry.get('git_blob')}
    if (entry.get('original_mode') != '100644' or rows.get(name) != expected
            or not re.fullmatch(r'[0-9a-f]{40}', entry.get('git_blob', ''))
            or not re.fullmatch(r'[0-9a-f]{64}', entry.get('sha256', ''))):
        raise ValueError('Implicit root header differs from its pinned Git tree: ' + name)
    chain = entry['required_by_chain']
    if (len(chain) < 2 or chain[-1] != name
            or set(chain[:-1]) != set(entry['original_include_inputs'])):
        raise ValueError('Implicit root header needs an exact reviewed production include chain')
    validate_include_chain(chain, entry['original_include_inputs'], rows, source)
    return '100644', entry['git_blob'], name


def restore_main_headers(entries, cache, revision, source, verify=False):
    report = {}
    for name, entry in entries.items():
        tree = materialize.git(cache, 'ls-tree', '-r', '-z', revision, '--',
                               name, *entry['original_include_inputs'],
                               env={**os.environ, 'GIT_NO_LAZY_FETCH': '1'})
        selected = validate_main_header(name, entry, tree, source)
        restored = None
        if not verify:
            restored = materialize.materialize_tree(cache, revision, source,
                                                     [selected], 'Chromium implicit header ' + name)
        materialize.check_parents(source, name)
        path = source / name
        materialize.validate_existing(path, selected[0], selected[1])
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
            raise ValueError('Implicit root header checksum differs: ' + name)
        report[name] = {**(restored or {}), 'git_blob': selected[1],
                        'sha256': entry['sha256'], 'required_by_chain': entry['required_by_chain']}
    return report


def verify_dependency(name, entry, result=None):
    cache = materialize.WORK / 'deps-cache' / (name.replace('/', '_') + '.git')
    destination = materialize.SRC / name
    if not cache.exists():
        raise ValueError('Implicit source is missing; restore with standalone/tools/materialize_implicit_sources.py: ' + name)
    entries = materialize.tree_entries(cache, entry['revision'], no_lazy_fetch=True)
    blobs = {path: (mode, oid) for mode, oid, path in entries}
    for mode, oid, path in entries:
        materialize.check_parents(destination, path)
        materialize.validate_existing(destination / path, mode, oid)
    required = {}
    for path in entry['required_inputs']:
        if path not in blobs:
            raise ValueError('Required implicit header is absent from its pinned Git tree: ' + path)
        required[path] = {'git_blob': blobs[path][1],
                          'sha256': hashlib.sha256((destination / path).read_bytes()).hexdigest()}
    inventory = ''.join(f'{mode} {oid}\t{path}\n' for mode, oid, path in entries).encode()
    return {**(result or {}), 'repository': entry['repository'], 'revision': entry['revision'],
            'inventory_sha256': hashlib.sha256(inventory).hexdigest(),
            'verified_file_count': len(entries), 'required_inputs': required}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', action='store_true', help='Print the root-DEPS source supplement without downloads or writes')
    parser.add_argument('--verify', action='store_true', help='Verify restored original inputs without downloading')
    parser.add_argument('--git-blobs', action='store_true', help='Use original Git blobs even for large supplements')
    parser.add_argument('--report-path', type=Path, help='Write verification evidence to an isolated profile path')
    args = parser.parse_args(argv)
    report_path = args.report_path or REPORT
    lock, baseline, original_deps, lock_sha256 = load_selection()
    report = {'schema': 1, 'chromium_revision': lock['chromium_revision'],
              'lock_sha256': lock_sha256, 'compiled': False, 'targets_added': False,
              'test_binaries_built': False, 'hooks_executed': False,
              'recursive_sync': False, 'dependencies': {}, 'main_headers': {}, 'complete': False,
              'scope': 'Explicit compiler-implicit production source supplements; no claim of complete compiler include closure'}
    if args.plan:
        report['dependencies'] = {name: {'repository': entry['repository'],
                                         'revision': entry['revision'],
                                         'required_inputs': entry['required_inputs']}
                                  for name, entry in lock['dependencies'].items()}
        report['main_headers'] = lock.get('main_headers', {})
        print(json.dumps(report, indent=2))
        return 0
    history = ROOT / 'standalone/work/history/chromium-mirror.git'
    cache = history if history.exists() else materialize.WORK / 'chromium.git'
    if not cache.exists():
        raise ValueError('Prepare the pinned Chromium source metadata with standalone/tools/materialize.py first')
    materialize.write_json(report_path, report)
    for name, entry in lock['dependencies'].items():
        paths = ['DEPS', name, *entry['original_include_inputs']]
        tree = materialize.git(cache, 'ls-tree', '-r', '-z', lock['chromium_revision'], '--', *paths,
                               env={**os.environ, 'GIT_NO_LAZY_FETCH': '1'})
        # A matching string pin alone is insufficient: bind the preserved DEPS
        # bytes and production forwarding headers to the original commit tree.
        deps_path = SUPPORT / 'upstream/chromium/DEPS'
        if git_rows(tree).get('DEPS') != {'mode': '100644', 'type': 'blob',
                                          'oid': materialize.blob_digest(deps_path)}:
            raise ValueError('Preserved root DEPS is not the original pinned Git blob')
        validate_provenance(name, entry, tree, materialize.SRC)
        result = None if args.verify else materialize.dependency(name, entry, not args.git_blobs)
        report['dependencies'][name] = verify_dependency(name, entry, result)
    report['main_headers'] = restore_main_headers(lock.get('main_headers', {}), cache,
                                                   lock['chromium_revision'], materialize.SRC,
                                                   verify=args.verify)
    report['complete'] = True
    materialize.write_json(report_path, report)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print('error:', error, file=sys.stderr)
        sys.exit(2)
