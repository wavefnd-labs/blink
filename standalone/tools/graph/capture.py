"""Capture an evaluated GN graph and its exact input provenance; never compile."""
from collections import Counter, deque
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import posixpath
import shlex
import shutil
import subprocess
from hydrate import CACHE, DEPS, REV, ROOT, SRC, WORK

SUPPORT = ROOT / 'standalone'
OUT = SRC / 'out/BlinkGraph'
TARGET = '//third_party/blink/public:blink'
FLAGS = ['--root=' + str(SRC), '--root-target=' + TARGET, '--root-pattern=' + TARGET, '--script-executable=/usr/bin/python3']
GN = str(WORK / 'tools/gn')
FIELDS = ('sources', 'inputs', 'data', 'script', 'public', 'include_dirs', 'lib_dirs')


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, indent=2) + '\n')


def owner(path, owners):
    parts = path.split('/')
    for length in range(len(parts), 0, -1):
        candidate = '/'.join(parts[:length])
        if candidate in owners:
            return candidate
    return None


def file_path(value):
    if value.startswith('//'):
        return posixpath.normpath(value[2:])
    if value.startswith(str(SRC) + '/'):
        return value[len(str(SRC)) + 1:]
    return None


def blob_index(cache, revision):
    raw = subprocess.check_output(['git', '--git-dir=' + str(cache), 'ls-tree', '-r', '-z', revision])
    result = {}
    for row in raw.split(b'\0'):
        if row:
            metadata, path = row.split(b'\t', 1)
            mode, kind, oid = metadata.split()
            if kind == b'blob':
                result[os.fsdecode(path)] = oid.decode()
    return result


def capture():
    if not (OUT / 'build.ninja').is_file():
        raise ValueError('Run GN generation successfully before capture')
    calls = {
        'gn-graph.json': ['desc', str(OUT), '*', '--all-toolchains', '--format=json'],
        'runtime-deps.txt': ['desc', str(OUT), TARGET, 'runtime_deps'],
        'evaluated-args.json': ['args', str(OUT), '--list', '--json'],
    }
    for filename, arguments in calls.items():
        result = subprocess.check_output([GN, *arguments, *FLAGS])
        (WORK / filename).write_bytes(result)
    graph_bytes = (WORK / 'gn-graph.json').read_bytes()
    graph = json.loads(graph_bytes)
    spec = importlib.util.spec_from_file_location('blink_tools', SUPPORT / 'tools/blink.py')
    blink = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(blink)
    policy = json.loads((SUPPORT / 'config/boundary.json').read_text())
    boundary = blink.boundary_check(graph, TARGET, policy)
    write_json(SUPPORT / 'reports/linux-x64-boundary.json', boundary)
    # Fail closed on any incomplete closure, including a dependency not exported.
    queue, closure = deque([TARGET]), set()
    while queue:
        label = queue.popleft()
        if label in closure:
            continue
        closure.add(label)
        for field in policy['dependency_fields']:
            queue.extend(graph[label].get(field, []))
    owners = {key.removeprefix('src/'): entry for key, entry in DEPS.items()}
    roles = {}
    paths = set()
    for label in sorted(closure):
        path = label[2:].split('(', 1)[0].split(':', 1)[0]
        key = owner(path, owners)
        if key:
            roles.setdefault(key, set()).add('target')
        for field in FIELDS:
            values = graph[label].get(field, [])
            for val in [values] if isinstance(values, str) else values:
                normalized = file_path(val)
                if normalized is not None:
                    paths.add(normalized)
                    key = owner(normalized, owners)
                    if key:
                        roles.setdefault(key, set()).add('file_or_include_directory')
    source_paths = sorted(p for p in paths if not p.startswith('out/BlinkGraph/'))
    generated_paths = sorted(p for p in paths if p.startswith('out/BlinkGraph/'))
    main_blobs = blob_index(CACHE, REV)
    deps_spec = importlib.util.spec_from_file_location('dependency_parser', SUPPORT / 'tools/dependencies.py')
    dep_parser = importlib.util.module_from_spec(deps_spec)
    deps_spec.loader.exec_module(dep_parser)
    declared = dep_parser.inventory(SRC / 'DEPS', REV)
    gitlink_count = dep_parser.verify_gitlinks(declared, subprocess.check_output(['git', '--git-dir=' + str(CACHE), 'ls-tree', '-r', '-z', REV]))
    indices = {}
    input_rows = []
    input_text = (OUT / 'build.ninja.d').read_text().split(': ', 1)[1].replace('\\\n', '')
    for filename in sorted(set(shlex.split(input_text))):
        absolute = Path(os.path.abspath(OUT / filename))
        relative = absolute.relative_to(SRC).as_posix()
        key = owner(relative, owners)
        if not absolute.is_file():
            input_rows.append({'path': relative, 'origin': 'existence_check', 'owner': key or 'chromium', 'kind': 'directory' if absolute.is_dir() else 'missing', 'git_blob': None})
            if key:
                roles.setdefault(key, set()).add('evaluation_input')
            continue
        data = absolute.read_bytes()
        expected = None
        origin = 'generated'
        if key and owners[key].get('dep_type') == 'git':
            entry = owners[key]
            cache = WORK / 'deps-cache' / (key.replace('/', '_') + '.git')
            if key not in indices:
                indices[key] = blob_index(cache, entry['revision'])
            expected = indices[key].get(relative[len(key) + 1:])
            origin = 'git_dependency'
        elif key:
            origin = owners[key]['dep_type'] + '_package'
        elif not relative.startswith('out/'):
            expected = main_blobs.get(relative)
            origin = 'chromium' if expected else 'generated'
        actual = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        if expected and actual != expected:
            raise ValueError('Original GN input was modified: ' + relative)
        input_rows.append({'path': relative, 'origin': origin, 'owner': key or 'chromium',
                           'sha256': hashlib.sha256(data).hexdigest(), 'git_blob': expected})
        if key:
            roles.setdefault(key, set()).add('evaluation_input')
    # Bootstrap tools are explicit even when GN does not model them as targets.
    for key in ('buildtools/linux64', 'third_party/llvm-build/Release+Asserts', 'third_party/rust-toolchain', 'build/linux/debian_bullseye_amd64-sysroot', 'third_party/ninja'):
        if key in owners:
            roles.setdefault(key, set()).add('build_infrastructure')
    selected = {key: {**owners[key], 'roles': sorted(value)} for key, value in sorted(roles.items())}
    preserved = {}
    provenance = SUPPORT / 'upstream/chromium'
    for name, source in [('DEPS', SRC / 'DEPS'), ('dotfile.gn', SRC / '.gn')]:
        target = provenance / name
        target.write_bytes(source.read_bytes())
        preserved[target.relative_to(ROOT).as_posix()] = {'original_path': 'DEPS' if name == 'DEPS' else '.gn', 'revision': REV, 'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}
    for key in ('third_party/angle', 'third_party/dawn', 'third_party/devtools-frontend/src'):
        source = SRC / key / 'DEPS'
        target = provenance / 'dependency-deps' / (key.replace('/', '_') + '.DEPS')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        preserved[target.relative_to(ROOT).as_posix()] = {'original_path': 'DEPS', 'repository': owners[key]['repository'], 'revision': owners[key]['revision'], 'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}
    shutil.copyfile(WORK / 'declared-deps.json', SUPPORT / 'reports/chromium-declared-dependencies.json')
    graph_path = SUPPORT / 'reports/linux-x64-blink-graph.json.gz'
    with graph_path.open('wb') as stream, gzip.GzipFile(filename='', mode='wb', fileobj=stream, mtime=0) as compressed:
        compressed.write(graph_bytes)
    write_json(SUPPORT / 'reports/linux-x64-gn-inputs.json', input_rows)
    write_json(SUPPORT / 'reports/linux-x64-graph-files.json', {'source_or_include_paths': source_paths, 'generated_paths': generated_paths})
    browser_inputs = [r['path'] for r in input_rows if any(r['path'] == p or r['path'].startswith(p + '/') for p in ('chrome', 'content', 'headless', 'android_webview'))]
    gn_version = subprocess.check_output([GN, '--version'], text=True).strip()
    lock = {
        'schema': 1, 'chromium_revision': REV,
        'profile': {'name': 'linux-x64-release-static', 'root_target': TARGET, 'args_gn': (OUT / 'args.gn').read_text(), 'script_executable': '/usr/bin/python3', 'python_version': subprocess.check_output(['/usr/bin/python3', '--version'], text=True).strip(), 'gn_version': gn_version, 'gn_sha256': hashlib.sha256(Path(GN).read_bytes()).hexdigest()},
        'scope': 'Evaluated GN target closure and typed file/include references for this profile; not a C++ include closure or a successful build',
        'root_deps_sha256': hashlib.sha256((SRC / 'DEPS').read_bytes()).hexdigest(),
        'declared_root_git_pins_verified': gitlink_count,
        'preserved_upstream_inputs': preserved,
        'dependency_policy': 'The pinned Chromium root DEPS and Gitlinks are authoritative for this integrated graph; do not independently sync child DEPS with standalone defaults.',
        'dependencies': selected,
        'graph': {'path': graph_path.relative_to(ROOT).as_posix(), 'sha256': hashlib.sha256(graph_bytes).hexdigest(), 'compressed_sha256': hashlib.sha256(graph_path.read_bytes()).hexdigest(), 'target_count': len(closure)},
        'limitations': ['Only Linux x64 in this configuration is resolved.', 'Most non-Blink C++ source bodies are not materialized; original pinned repositories remain required.', 'Host Python is recorded rather than installed from the original hermetic package.', 'Packages retain DEPS platform conditions; use these when fetching.', 'Graph generation is not compilation, linking or runtime validation.'],
    }
    write_json(SUPPORT / 'config/dependencies.lock.json', lock)
    summary = {
        'chromium_revision': REV, 'root_target': TARGET, 'target_count': len(closure),
        'target_types': dict(sorted(Counter(graph[label]['type'] for label in closure).items())),
        'dependency_count': len(selected), 'dependency_types': dict(Counter(e.get('dep_type') for e in selected.values())),
        'gn_input_count': len(input_rows), 'git_verified_input_count': sum(r['git_blob'] is not None for r in input_rows),
        'source_or_include_path_count': len(source_paths), 'generated_path_count': len(generated_paths),
        'runtime_dependency_paths': (WORK / 'runtime-deps.txt').read_text().splitlines(),
        'browser_target_boundary_passed': boundary['passed'], 'boundary_violations': boundary['violations'],
        'remaining_browser_build_inputs': browser_inputs,
        'compiled': False, 'linked': False, 'runtime_validated': False,
    }
    write_json(SUPPORT / 'reports/linux-x64-graph-summary.json', summary)
    (SUPPORT / 'config/linux-x64-graph.args.gn').write_bytes((OUT / 'args.gn').read_bytes())
    (SUPPORT / 'reports/linux-x64-evaluated-args.json').write_bytes((WORK / 'evaluated-args.json').read_bytes())
    print(json.dumps({key: val for key, val in summary.items() if key != 'runtime_dependency_paths'}, indent=2), flush=True)
    if not boundary['passed']:
        raise ValueError('Browser target boundary failed; see saved report')


if __name__ == '__main__':
    capture()
