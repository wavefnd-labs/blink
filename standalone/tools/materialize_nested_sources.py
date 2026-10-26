#!/usr/bin/env python3
"""Restore only reviewed nested Git inputs required by the engine build graph."""
import argparse
import configparser
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys

import dependencies
import materialize

ROOT = Path(__file__).resolve().parents[2]
SUPPORT = ROOT / 'standalone'
LOCK = SUPPORT / 'config/nested-source-dependencies.lock.json'
REPORT = SUPPORT / 'reports/linux-x64-materialized-nested-sources.json'


def safe_relative(name):
    path = PurePosixPath(name)
    if not name or not path.parts or path.is_absolute() or '..' in path.parts or '\\' in name:
        raise ValueError('Unsafe nested dependency path: ' + name)
    return name


def git_rows(tree):
    rows = {}
    for row in tree.split(b'\0'):
        if row:
            metadata, name = row.split(b'\t', 1)
            mode, kind, oid = metadata.decode().split()
            rows[os.fsdecode(name)] = {'mode': mode, 'type': kind, 'oid': oid}
    return rows


def validate_entry(name, entry, root_lock, root_deps, parent_tree, root=ROOT):
    """Require agreement between root pin, preserved DEPS/modules and gitlink."""
    safe_relative(name)
    if name.split('/', 1)[0] in materialize.BROWSER_ROOTS:
        raise ValueError('Browser nested dependency is forbidden')
    parent = entry['parent']
    parent_path = safe_relative(parent['path'])
    locked_parent = root_lock['dependencies'].get(parent_path, {})
    if (locked_parent.get('dep_type') != 'git'
            or parent['revision'] != locked_parent.get('revision')
            or parent['repository'] != locked_parent.get('repository')):
        raise ValueError('Nested dependency parent differs from the root dependency pin')
    link = entry['original_gitlink']
    relative = safe_relative(link['path'])
    if name != parent_path + '/' + relative:
        raise ValueError('Nested dependency destination differs from its original parent path')
    if 'src/' + name in root_deps['dependencies'] or entry.get('root_deps_declared') is not False:
        raise ValueError('Nested dependency must not duplicate or override a root DEPS entry')
    rows = git_rows(parent_tree)
    expected_link = {'mode': '160000', 'type': 'commit', 'oid': entry['revision']}
    if ({key: link[key] for key in ('mode', 'type', 'oid')} != expected_link
            or rows.get(relative) != expected_link):
        raise ValueError('Nested Git revision differs from the original parent gitlink')
    texts = {}
    for original in ('DEPS', '.gitmodules'):
        evidence = parent['preserved_inputs'][original]
        data = (root / safe_relative(evidence['path'])).read_bytes()
        if (evidence['original_path'] != original
                or hashlib.sha256(data).hexdigest() != evidence['sha256']
                or hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() != evidence['git_blob']
                or rows.get(original) != {'mode': '100644', 'type': 'blob', 'oid': evidence['git_blob']}):
            raise ValueError('Preserved parent provenance differs from its original Git blob: ' + original)
        texts[original] = data.decode()
    parsed = dependencies.inventory(root / parent['preserved_inputs']['DEPS']['path'], parent['revision'])
    declared = parsed['dependencies'].get(relative, {})
    if (declared.get('dep_type') != 'git' or declared.get('revision') != entry['revision']
            or declared.get('repository') != entry['repository']):
        raise ValueError('Nested dependency differs from the preserved parent DEPS')
    modules = configparser.ConfigParser(interpolation=None)
    modules.read_string(texts['.gitmodules'])
    matches = [section for section in modules.sections() if modules.get(section, 'path', fallback=None) == relative]
    if (len(matches) != 1
            or modules.get(matches[0], 'url').removesuffix('.git') != entry['repository'].removesuffix('.git')
            or modules.has_option(matches[0], 'gclient-condition')
            or declared.get('condition')):
        raise ValueError('Nested source must match an unconditional original .gitmodules declaration')
    for required in entry['required_inputs']:
        safe_relative(required)
    return {'repository': entry['repository'], 'revision': entry['revision'], 'dep_type': 'git'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', action='store_true', help='Validate provenance and print the reviewed nested source plan')
    parser.add_argument('--verify', action='store_true', help='Verify all restored nested source files without downloading')
    parser.add_argument('--report-path', type=Path, help='Write verification evidence to an isolated profile path')
    args = parser.parse_args(argv)
    report_path = args.report_path or REPORT
    lock_bytes = LOCK.read_bytes()
    lock = json.loads(lock_bytes)
    root_lock = json.loads((SUPPORT / 'config/dependencies.lock.json').read_text())
    if lock['chromium_revision'] != root_lock['chromium_revision'] or lock['profile'] != root_lock['profile']['name']:
        raise ValueError('Nested source profile/revision differs from the root lock')
    root_deps_path = SUPPORT / 'upstream/chromium/DEPS'
    root_deps = dependencies.inventory(root_deps_path, lock['chromium_revision'])
    if root_deps['deps_sha256'] != lock['root_deps_sha256'] or lock['root_deps_sha256'] != root_lock['root_deps_sha256']:
        raise ValueError('Original root DEPS provenance differs')
    report = {'schema': 1, 'chromium_revision': lock['chromium_revision'],
              'lock_sha256': hashlib.sha256(lock_bytes).hexdigest(), 'compiled': False,
              'hooks_executed': False, 'recursive_sync': False, 'dependencies': {}, 'complete': False}
    if not args.plan:
        materialize.write_json(report_path, report)
    for name, entry in lock['dependencies'].items():
        parent = entry['parent']
        cache = materialize.WORK / 'deps-cache' / (parent['path'].replace('/', '_') + '.git')
        tree = materialize.git(cache, 'ls-tree', '-r', '-z', parent['revision'], '--', 'DEPS', '.gitmodules', entry['original_gitlink']['path'],
                               env={**os.environ, 'GIT_NO_LAZY_FETCH': '1'})
        selected = validate_entry(name, entry, root_lock, root_deps, tree)
        if args.plan:
            report['dependencies'][name] = selected
            continue
        if not args.verify:
            result = materialize.dependency(name, selected)
        else:
            result = {'revision': selected['revision']}
        nested_cache = materialize.WORK / 'deps-cache' / (name.replace('/', '_') + '.git')
        entries = materialize.tree_entries(nested_cache, selected['revision'])
        destination = materialize.SRC / name
        blobs = {path: (mode, oid) for mode, oid, path in entries}
        for mode, oid, path in entries:
            materialize.check_parents(destination, path)
            materialize.validate_existing(destination / path, mode, oid)
        required = {}
        for path in entry['required_inputs']:
            if path not in blobs:
                raise ValueError('Required engine input is absent from the original nested Git tree: ' + path)
            required[path] = {'git_blob': blobs[path][1], 'sha256': hashlib.sha256((destination / path).read_bytes()).hexdigest()}
        report['dependencies'][name] = {**result, 'required_inputs': required,
                                        'verified_file_count': len(entries), 'parent_revision': parent['revision']}
    report['complete'] = not args.plan
    if not args.plan:
        materialize.write_json(report_path, report)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print('error:', error, file=sys.stderr)
        sys.exit(2)
