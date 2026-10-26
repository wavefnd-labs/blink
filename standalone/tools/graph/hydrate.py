from pathlib import Path
import importlib.util
import json
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
WORK = ROOT / 'standalone/work/graph'
SRC = WORK / 'src'
REV = json.loads((ROOT / 'standalone/config/upstream.lock.json').read_text())['revision']
HISTORY_CACHE = ROOT / 'standalone/work/history/chromium-mirror.git'
CACHE = HISTORY_CACHE if HISTORY_CACHE.exists() else WORK / 'chromium.git'
DEPS = json.loads((WORK / 'declared-deps.json').read_text())['dependencies'] if (WORK / 'declared-deps.json').exists() else {}
SUFFIXES = {'.gn', '.gni', '.py', '.pyi', '.pydeps', '.gritdeps', '.scope', '.json', '.txt', '.tmpl', '.template', '.xml', '.grd', '.grdp', '.mojom', '.typemap', '.list', '.filelist', '.globlist', '.cfg', '.ini', '.sha1', '.version', '.isolate', '.pyl', '.map', '.csv', '.dummy'}


def git(cache, *args, **kwargs):
    return subprocess.check_output(['git', '-c', 'credential.helper=', '--git-dir=' + str(cache), *args], **kwargs)


def selected(path):
    return Path(path).suffix in SUFFIXES or Path(path).name in {'DEPS', 'LICENSE', 'COPYING', 'VERSION', 'README.chromium', 'LASTCHANGE', 'LASTCHANGE.committime', '.gn'}


def hydrate(cache, revision, destination, wanted=None, skip_blink=False):
    rows = git(cache, 'ls-tree', '-r', '-z', revision).split(b'\0')
    entries = []
    for row in rows:
        if not row:
            continue
        info, name = row.split(b'\t', 1)
        mode, kind, oid = info.split()
        name = os.fsdecode(name)
        if kind != b'blob' or skip_blink and name.startswith('third_party/blink/'):
            continue
        if wanted is None and not selected(name) or wanted is not None and not any(name == w or name.startswith(w.rstrip('/') + '/') for w in wanted):
            continue
        path = destination / name
        if path.is_file() or path.is_symlink():
            continue
        entries.append((mode, oid, name))
    unique = sorted({oid for _, oid, _ in entries})
    env = {**os.environ, 'GIT_NO_LAZY_FETCH': '1'}
    check = git(cache, 'cat-file', '--batch-check=%(objectname) %(objecttype)', input=b'\n'.join(unique) + b'\n', env=env) if unique else b''
    missing = [line.split()[0] for line in check.splitlines() if line.endswith(b' missing')]
    print(f'{destination.relative_to(WORK)}: {len(entries)} files, {len(missing)} missing blobs', flush=True)
    for i in range(0, len(missing), 1500):
        batch = missing[i:i + 1500]
        subprocess.run(['git', '-c', 'credential.helper=', '-c', 'fetch.negotiationAlgorithm=noop', '--git-dir=' + str(cache), 'fetch', 'origin', '--no-tags', '--no-write-fetch-head', '--recurse-submodules=no', '--filter=blob:none', '--stdin'], input=b'\n'.join(batch) + b'\n', check=True)
        print(f'Fetched {min(i + len(batch), len(missing))}/{len(missing)} blobs', flush=True)
    process = subprocess.Popen(['git', '--git-dir=' + str(cache), 'cat-file', '--batch'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
    try:
        for mode, oid, name in entries:
            process.stdin.write(oid + b'\n')
            process.stdin.flush()
            header = process.stdout.readline().split()
            if len(header) != 3 or header[1] != b'blob':
                raise ValueError(f'Missing blob: {name}')
            data = process.stdout.read(int(header[2]))
            assert process.stdout.read(1) == b'\n'
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if mode == b'120000':
                target.symlink_to(os.fsdecode(data))
            else:
                target.write_bytes(data)
                target.chmod(0o755 if mode == b'100755' else 0o644)
    finally:
        process.stdin.close()
        process.stdout.close()
        process.wait()
    return len(entries)


def dependency(name, wanted=None):
    key = 'src/' + name
    entry = DEPS.get(key)
    if not entry or entry.get('dep_type') != 'git':
        raise ValueError(f'No declared Git dependency: {key}')
    cache = WORK / 'deps-cache' / (name.replace('/', '_') + '.git')
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['git', 'init', '--bare', '--quiet', str(cache)], check=True)
        git(cache, 'config', 'remote.origin.url', entry['repository'])
        git(cache, 'config', 'remote.origin.promisor', 'true')
        git(cache, 'config', 'remote.origin.partialclonefilter', 'blob:none')
    git(cache, 'config', 'remote.origin.url', entry['repository'])
    try:
        git(cache, 'cat-file', '-e', entry['revision'] + '^{commit}', env={**os.environ, 'GIT_NO_LAZY_FETCH': '1'}, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        print('Fetching pinned dependency:', key, entry['revision'], flush=True)
        git(cache, 'fetch', '--filter=blob:none', '--depth=1', '--no-tags', 'origin', entry['revision'])
    # Version generators must see this dependency's commit, not the parent
    # Chromium repository found by walking upward from a plain source folder.
    destination = SRC / name
    destination.mkdir(parents=True, exist_ok=True)
    git(cache, 'config', 'core.bare', 'false')
    git(cache, 'config', 'core.worktree', str(destination))
    git(cache, 'update-ref', 'refs/heads/pinned', entry['revision'])
    git(cache, 'symbolic-ref', 'HEAD', 'refs/heads/pinned')
    (destination / '.git').write_text('gitdir: ' + str(cache) + '\n')
    return hydrate(cache, entry['revision'], SRC / name, wanted)


if __name__ == '__main__':
    if sys.argv[1] == 'main':
        if not SRC.exists():
            subprocess.run(['git', 'clone', '--shared', '--no-checkout', '--branch=upstream', str(CACHE), str(SRC)], check=True)
            subprocess.run(['git', '-C', str(SRC), 'update-ref', 'HEAD', REV], check=True)
        hydrate(CACHE, REV, SRC, None, skip_blink=True)
        link = SRC / 'third_party/blink'
        if not link.exists():
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(ROOT, target_is_directory=True)
    elif sys.argv[1] == 'file':
        hydrate(CACHE, REV, SRC, sys.argv[2:])
    elif sys.argv[1] == 'dep':
        dependency(sys.argv[2], sys.argv[3:] or None)
