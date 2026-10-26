#!/usr/bin/env python3
"""Read pinned Chromium DEPS without executing hooks or arbitrary Python."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import string


class LiteralString(str):
    """DEPS Str() marks data strings rather than condition expressions."""


def parse_deps(text):
    scope = {}

    def value(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, bool, type(None))):
            return node.value
        if isinstance(node, (ast.List, ast.Tuple)):
            return [value(item) for item in node.elts]
        if isinstance(node, ast.Dict):
            return {value(key): value(item) for key, item in zip(node.keys, node.values)}
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return value(node.left) + value(node.right)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords and len(node.args) == 1:
            if node.func.id == 'Str':
                return LiteralString(value(node.args[0]))
            if node.func.id == 'Var':
                return scope['vars'][value(node.args[0])]
        raise ValueError(f'Unsupported DEPS expression: {ast.dump(node)[:160]}')

    for statement in ast.parse(text).body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
            raise ValueError('Only literal DEPS assignments are supported; no code is executed')
        scope[statement.targets[0].id] = value(statement.value)
    return scope


def inventory(path, revision):
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('Use an exact Chromium commit ID')
    data = Path(path).read_bytes()
    parsed = parse_deps(data.decode())
    dependencies = {}
    for name, original in sorted(parsed['deps'].items()):
        entry = {'url': original} if isinstance(original, str) else dict(original or {})
        if entry.get('url'):
            for _, field, spec, conversion in string.Formatter().parse(entry['url']):
                if field is not None and (not re.fullmatch(r'[A-Za-z_]\w*', field) or spec or conversion):
                    raise ValueError(f'Unsupported DEPS URL substitution: {name}')
            entry['url'] = entry['url'].format_map(parsed['vars'])
            repository, separator, commit = entry['url'].rpartition('@')
            if not separator or len(commit) != 40 or any(c not in '0123456789abcdef' for c in commit):
                raise ValueError(f'Unpinned Git dependency: {name}')
            entry.update(repository=repository, revision=commit, dep_type='git')
        dependencies[name] = entry
    return {
        'schema': 1, 'chromium_revision': revision,
        'deps_sha256': hashlib.sha256(data).hexdigest(),
        'scope': 'All declared root DEPS entries; conditions retained, not a selected engine dependency closure',
        'variables': parsed['vars'], 'dependencies': dependencies,
        'recursedeps': parsed.get('recursedeps', []),
        'hooks_executed': False,
    }


def verify_gitlinks(declared, tree):
    links = {}
    for row in tree.split(b'\0'):
        if row.startswith(b'160000 '):
            metadata, name = row.split(b'\t', 1)
            links['src/' + name.decode()] = metadata.split()[2].decode()
    pins = {name: entry['revision'] for name, entry in declared['dependencies'].items()
            if entry.get('dep_type') == 'git'}
    if pins != links:
        mismatches = sorted(name for name in set(pins) | set(links) if pins.get(name) != links.get(name))
        raise ValueError('DEPS pins differ from original Gitlinks: ' + ', '.join(mismatches))
    return len(links)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deps', required=True, type=Path)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    from blink import ROOT, SUPPORT, ensure_auxiliary_output, load_lock
    ensure_auxiliary_output(args.output, load_lock(SUPPORT / 'config/upstream.lock.json'), ROOT)
    result = inventory(args.deps, args.revision)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(f"Recorded {len(result['dependencies'])} declared dependencies; platform and target selection still required")


if __name__ == '__main__':
    main()
