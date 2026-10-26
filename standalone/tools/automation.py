#!/usr/bin/env python3
"""Read-only upstream discovery and provenance-bound engine release planning.

This tool never imports sources, creates Git tags, publishes releases or claims
that graph validation is runtime validation. Network access is explicit in the
upstream and ensure-baseline commands only.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
CONFIG = Path('standalone/config/release.json')
VERSION = Path('standalone/gn/VERSION')
LOCKS = ('upstream.lock.json', 'dependencies.lock.json', 'engine-adaptations.json',
         'history.lock.json', 'nested-source-dependencies.lock.json',
         'nested-build-packages.lock.json', 'implicit-source-dependencies.lock.json',
         'generator-layout.json',
         'blink-python-layout.json', 'build-prerequisites.json',
         'release.json')
REVISION = re.compile(r'[0-9a-f]{40}')
VERSION_NUMBER = re.compile(r'\d+\.\d+\.\d+\.\d+')
OFFICIAL_SOURCE = 'https://chromium.googlesource.com/chromium/src'
VERSION_API = 'https://versionhistory.googleapis.com/v1/chrome/platforms'


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def sha256(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def parse_version(text):
    values = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator or key not in ('MAJOR', 'MINOR', 'BUILD', 'PATCH') or not value.isdecimal() or key in values:
            raise ValueError('Invalid or duplicate upstream VERSION field')
        values[key] = int(value)
    if set(values) != {'MAJOR', 'MINOR', 'BUILD', 'PATCH'}:
        raise ValueError('Incomplete upstream VERSION')
    return '.'.join(str(values[key]) for key in ('MAJOR', 'MINOR', 'BUILD', 'PATCH'))


def identity(root=ROOT):
    config = read_json(root / CONFIG)
    if config['release_track'] not in ('development', 'stable'):
        raise ValueError('Unknown engine release track')
    if any(type(config[field]) is not int or config[field] < 1 for field in ('abi_version', 'package_revision')):
        raise ValueError('ABI and package revisions must be positive integers')
    upstream = read_json(root / 'standalone/config/upstream.lock.json')
    revision = upstream['revision']
    if not REVISION.fullmatch(revision):
        raise ValueError('Upstream must be an immutable full commit ID')
    adaptation = read_json(root / 'standalone/config/engine-adaptations.json')
    if (adaptation['chromium_revision'] != revision or
            sha256(root / VERSION) != adaptation['version_metadata']['original_sha256']):
        raise ValueError('VERSION does not match the reviewed upstream compatibility metadata')
    version = parse_version((root / VERSION).read_text())
    package = version + '-engine.' + str(config['package_revision'])
    return {'product': config['product'], 'upstream_revision': revision,
            'upstream_version': version, 'version': package, 'tag': 'blink-' + package,
            'abi_version': config['abi_version'], 'release_track': config['release_track'],
            'profile': config['supported_profile']}


def snapshot(root=ROOT):
    """Bind receipts to source HEAD and every lock, not a previous successful run."""
    head = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    if not REVISION.fullmatch(head):
        raise ValueError('Expected a Git commit ID')
    # Engine generation refreshes tracked diagnostic reports. They are evidence,
    # not source; requiring them to remain unchanged would block every CI build.
    dirty = subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain=v1', '--untracked-files=all',
                                     '--', '.', ':(exclude)standalone/reports'], text=True)
    support_files = {}
    for directory in ('common', 'config', 'engine', 'gn', 'include', 'patches', 'platform', 'tools'):
        for path in sorted((root / 'standalone' / directory).rglob('*')):
            if '__pycache__' in path.parts or path.suffix == '.pyc':
                continue
            if path.is_symlink():
                raise ValueError('Project support files must not be symlinks: ' + str(path))
            if path.is_file():
                support_files[path.relative_to(root).as_posix()] = sha256(path)
    workflows = {path.relative_to(root).as_posix(): sha256(path)
                 for path in sorted((root / '.github').rglob('*')) if path.is_file()}
    return {'git_commit': head, 'git_worktree_clean': not bool(dirty),
            'support_files': support_files, 'automation_files': workflows,
            'locks': {name: sha256(root / 'standalone/config' / name) for name in LOCKS},
            'version_sha256': sha256(root / VERSION)}


def get_bytes(url):
    if urllib.parse.urlparse(url).scheme != 'https':
        raise ValueError('Official discovery requires HTTPS')
    request = urllib.request.Request(url, headers={'User-Agent': 'BlinkEngineUpstreamDiscovery/1'})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def get_json(url):
    return json.loads(get_bytes(url).decode().removeprefix(")]}'\n"))


def discover(root=ROOT, fetch_json=get_json, fetch_bytes=get_bytes):
    config = read_json(root / CONFIG)
    channel, platform = config['upstream_channel'], config['upstream_platform']
    if channel not in ('stable', 'beta', 'dev', 'canary') or platform != 'linux':
        raise ValueError('Unsupported upstream tracking profile')
    # VersionHistory describes Chrome release channels. Resolve its version tag
    # in the official Chromium source and verify VERSION at the immutable SHA.
    feed = f'{VERSION_API}/{platform}/channels/{channel}/versions?order_by=version%20desc&page_size=1'
    rows = fetch_json(feed).get('versions', [])
    versions = [row['version'] for row in rows if VERSION_NUMBER.fullmatch(row.get('version', ''))]
    if not versions:
        raise ValueError('Official release feed contains no valid versions')
    version = max(versions, key=lambda value: tuple(map(int, value.split('.'))))
    tag_url = f'{OFFICIAL_SOURCE}/+/refs/tags/{version}?format=JSON'
    commit = fetch_json(tag_url).get('commit', '')
    if not REVISION.fullmatch(commit):
        raise ValueError('Official Chromium tag did not resolve to a commit')
    source_url = f'{OFFICIAL_SOURCE}/+/{commit}/chrome/VERSION?format=TEXT'
    version_bytes = base64.b64decode(fetch_bytes(source_url), validate=True)
    if parse_version(version_bytes.decode()) != version:
        raise ValueError('Chromium tag VERSION disagrees with the release feed')
    current = identity(root)
    candidate_version = tuple(map(int, version.split('.')))
    current_version = tuple(map(int, current['upstream_version'].split('.')))
    if current['upstream_revision'] == commit:
        update_status = 'current'
    elif candidate_version < current_version:
        update_status = 'older-than-current'
    elif candidate_version == current_version:
        update_status = 'same-version-different-revision'
    else:
        update_status = 'newer-version'
    candidate_key = hashlib.sha256((version + '\n' + commit).encode()).hexdigest()
    return {'schema': 1, 'checked_at': datetime.now(timezone.utc).isoformat(),
            'candidate_key': candidate_key, 'channel': channel, 'platform': platform,
            'current': current, 'candidate': {'version': version, 'revision': commit,
            'version_sha256': hashlib.sha256(version_bytes).hexdigest()},
            'same_revision': current['upstream_revision'] == commit,
            'same_version': current['upstream_version'] == version,
            'update_status': update_status,
            'automatic_proposal_eligible': update_status == 'newer-version',
            'sources': {'release_feed': feed, 'chromium_tag': tag_url, 'version_file': source_url},
            'source_update_performed': False, 'release_published': False,
            'required_update_steps': [
                'Import the full Blink path history at candidate.revision; retain original author/date/message and ID mapping, including legacy history.',
                'Update the pristine source inventory, original README and license bytes at the same immutable revision.',
                'Recompute pinned dependency closure and review exact build/platform adaptations; do not reuse old lock hashes.',
                'Generate the engine-only graph, check browser target and typed file boundaries, and compile the SDK.',
                'Run the embedding initialization, rendering and shutdown smoke tests before release validation.']}


def ensure_baseline(root=ROOT):
    history = read_json(root / 'standalone/config/history.lock.json')
    commit = history['extraction']['extracted_revision']
    tree = read_json(root / 'standalone/config/upstream.lock.json')['git_source_tree']['tree']
    if not REVISION.fullmatch(commit) or not REVISION.fullmatch(tree):
        raise ValueError('Invalid baseline Git identity')
    command = ['git', '-C', str(root)]
    if subprocess.run([*command, 'cat-file', '-e', tree + '^{tree}'], stderr=subprocess.DEVNULL).returncode:
        environment = dict(os.environ)
        token = environment.pop('BLINK_BASELINE_GITHUB_TOKEN', '')
        if token:
            # Let private-repository CI fetch its immutable ancestor without
            # persisting checkout credentials or placing the token in argv.
            authorization = base64.b64encode(('x-access-token:' + token).encode()).decode()
            environment.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',
                               GIT_CONFIG_VALUE_0='AUTHORIZATION: basic ' + authorization)
        subprocess.run([*command, 'fetch', '--no-tags', '--depth=1', 'origin', commit], check=True, env=environment)
    subprocess.run([*command, 'cat-file', '-e', tree + '^{tree}'], check=True)


def stage_sdk(output, destination=None, resources=None, root=ROOT):
    """Stage compiled Linux SDK files without building or rewriting any source.

    Resource selection is explicit: the caller must choose the snapshot that
    matches the evaluated USE_V8_CONTEXT_SNAPSHOT build flag, never whichever
    stale snapshot happens to exist first in an output directory.
    """
    output = Path(output).resolve()
    resources = resources or {}
    required = {'icu_data', 'v8_snapshot', 'blink_resources'}
    if not required.issubset(resources) or set(resources) - required - {'blink_scaled_resources'}:
        raise ValueError('SDK needs explicit ICU, V8 snapshot and Blink resource paths')
    current = identity(root)
    destination = Path(destination or root / 'standalone/out/sdk' / (current['version'] + '-' + current['profile']))
    if not destination.is_absolute():
        destination = root / destination
    allowed = (root / 'standalone/out').resolve()
    if destination.is_symlink() or not destination.resolve().is_relative_to(allowed) or destination.resolve() == allowed:
        raise ValueError('SDK destination must be inside standalone/out')
    files = {}
    for name, target in (('libblink_embed.so', 'lib/libblink_embed.so'), ('blink_embed_smoke', 'bin/blink_embed_smoke')):
        path = output / name
        if not path.is_file() or path.is_symlink():
            raise ValueError('Compiled SDK output is missing: ' + str(path))
        with path.open('rb') as source:
            if source.read(4) != b'\x7fELF':
                raise ValueError('Linux SDK output is not an ELF binary: ' + str(path))
        files[target] = path
    resource_paths = {}
    for key, value in sorted(resources.items()):
        path = Path(value)
        path = path if path.is_absolute() else output / path
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(output):
            raise ValueError('SDK resource must be a real build output: ' + str(path))
        target = 'share/blink/' + path.name
        if target in files:
            raise ValueError('SDK resource basenames collide')
        files[target] = path
        resource_paths[key] = target
    headers = sorted((root / 'standalone/include').rglob('*.h'))
    if not headers:
        raise ValueError('Embedding API headers are missing')
    for path in headers:
        if path.is_symlink():
            raise ValueError('Embedding API header must not be a symlink')
        files['include/' + path.relative_to(root / 'standalone/include').as_posix()] = path
    for name in ('LICENSE', 'LICENSE_FOR_ABOUT_CREDITS', 'README.md'):
        path = root / name
        if not path.is_file():
            raise ValueError('Original upstream attribution is missing: ' + name)
        files['notices/blink/' + name] = path
    for source, target in (('standalone/engine/NOTICE', 'notices/blink/ENGINE_NOTICE'),
                           ('standalone/runtime.md', 'docs/runtime.md')):
        path = root / source
        if not path.is_file() or path.is_symlink():
            raise ValueError('SDK runtime attribution or scope documentation is missing: ' + source)
        files[target] = path
    for name in LOCKS:
        files['provenance/' + name] = root / 'standalone/config' / name
    # Preserve original notices from the materialized library/dependency tree.
    # This deliberately errs on the side of retaining extra build-tool notices;
    # it does not claim a legal audit or replace upstream license requirements.
    source_tree = root / 'standalone/work/graph/src'
    notice_prefixes = ('LICENSE', 'COPYING', 'NOTICE', 'COPYRIGHT', 'AUTHORS')
    excluded = {'chrome', 'content', 'headless', 'android_webview', 'ios', 'out', 'standalone'}
    for top in sorted(source_tree.iterdir()) if source_tree.is_dir() else []:
        if top.is_symlink():
            continue
        if top.is_file() and (top.name.startswith(notice_prefixes) or top.name == 'README.chromium'):
            files['notices/dependencies/' + top.name] = top
            if top.name == 'LICENSE':
                files['notices/chromium/LICENSE'] = top
            continue
        if top.name in excluded or not top.is_dir() or top.name.startswith('.'):
            continue
        for directory, directories, names in os.walk(top, followlinks=False):
            directories[:] = sorted(name for name in directories if not name.startswith('.') and
                                    not (Path(directory) / name).is_symlink())
            for name in sorted(names):
                path = Path(directory) / name
                if (name.startswith(notice_prefixes) or name == 'README.chromium') and not path.is_symlink():
                    files['notices/dependencies/' + path.relative_to(source_tree).as_posix()] = path
    for name in ('LICENSE', 'README.md'):
        original = root / 'standalone/upstream/chromium' / name
        if original.is_file() and not original.is_symlink():
            files['notices/chromium/' + name] = original
    state = snapshot(root)
    inventory = {name: {'sha256': sha256(path), 'bytes': path.stat().st_size} for name, path in sorted(files.items())}
    manifest = {'schema': 1, **current, 'snapshot': state, 'resources': resource_paths,
                'files': inventory, 'license_audit_completed': False,
                'scope': 'Staged SDK binaries, embedding headers, runtime resources, original notices and pinned provenance; no browser application'}
    if destination.exists():
        previous = read_json(destination / 'sdk-manifest.json')
        if previous != manifest:
            raise ValueError('SDK destination already contains a different build; preserve it or choose a new destination')
        staged = {path.relative_to(destination).as_posix() for path in destination.rglob('*') if path.is_file()}
        if staged != set(inventory) | {'sdk-manifest.json'} or any(path.is_symlink() for path in destination.rglob('*')):
            raise ValueError('Existing SDK contains unexpected files or links')
        for name, row in inventory.items():
            path = destination / name
            if not path.is_file() or path.is_symlink() or sha256(path) != row['sha256']:
                raise ValueError('Existing SDK staged file changed: ' + name)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.blink-sdk-', dir=destination.parent) as directory:
            stage = Path(directory) / 'sdk'
            stage.mkdir()
            for name, source in sorted(files.items()):
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                if sha256(target) != inventory[name]['sha256']:
                    raise ValueError('Build output changed during SDK staging: ' + name)
            write_json(stage / 'sdk-manifest.json', manifest)
            stage.rename(destination)
    return [{'path': path.relative_to(root).as_posix(), 'sha256': sha256(path)}
            for path in sorted(destination.rglob('*')) if path.is_file()]


def release_plan(root=ROOT, evidence=None, candidate=None):
    current, state = identity(root), snapshot(root)
    reasons = []
    artifacts = []
    if (root / 'standalone/config/upstream-update.json').exists():
        reasons.append('An unresolved upstream proposal remains; complete and remove it before release validation')
    if state.get('git_worktree_clean') is not True:
        reasons.append('Release validation requires committed project sources in a clean worktree')
    if evidence is None:
        reasons.append('No compilation and runtime-validation receipt was supplied')
    else:
        if evidence.get('build_profile', 'release') != 'release':
            reasons.append('Development profile artifacts cannot be published as a release SDK')
        if evidence.get('snapshot') != state:
            reasons.append('Validation receipt does not match current source and locks')
        if evidence.get('compiled') is not True or evidence.get('runtime_smoke_passed') is not True:
            reasons.append('A full SDK build and executed embedding rendering smoke test must pass')
        for row in evidence.get('artifacts', []):
            path = Path(row.get('path', ''))
            if path.is_absolute() or '..' in path.parts or not path.parts:
                reasons.append('Invalid artifact path in validation receipt')
                continue
            actual = root / path
            if not actual.is_file() or actual.is_symlink() or sha256(actual) != row.get('sha256'):
                reasons.append('Validated artifact is missing or changed: ' + str(path))
            else:
                artifacts.append({'path': str(path), 'sha256': row['sha256'], 'bytes': actual.stat().st_size})
        if not artifacts:
            reasons.append('No validated SDK artifacts were supplied')
    if current['release_track'] == 'stable':
        official = (candidate or {}).get('candidate', {})
        if ((candidate or {}).get('channel') != 'stable' or official.get('revision') != current['upstream_revision'] or
                official.get('version') != current['upstream_version']):
            reasons.append('Stable branding requires a matching verified official stable upstream candidate')
    return {'schema': 1, **current, 'snapshot': state, 'publishable': not reasons,
            'blockers': reasons, 'artifacts': artifacts,
            'license_policy': 'Imported engine and dependency notices remain authoritative; the separate browser license does not replace them.',
            'published': False}


def runner_preflight(root=ROOT, minimum_free_gib=None):
    required = read_json(root / CONFIG)['full_build_minimum_free_gib'] if minimum_free_gib is None else minimum_free_gib
    free = shutil.disk_usage(root).free / 1024**3
    if sys.platform != 'linux' or platform.machine() not in ('x86_64', 'AMD64'):
        raise ValueError('This full-build profile requires an x86_64 Linux runner')
    if sys.version_info[:2] < (3, 12):
        raise ValueError('Full-build preparation requires Python 3.12 or newer')
    missing = [name for name in ('git', 'pkg-config', 'cc', 'c++') if shutil.which(name) is None]
    if missing:
        raise ValueError('Full-build runner is missing required host tools: ' + ', '.join(missing))
    script = read_json(root / 'standalone/config/dependencies.lock.json')['profile']['script_executable']
    version = subprocess.check_output([script, '-c', 'import sys; print(sys.version_info[0], sys.version_info[1])'], text=True).strip()
    try:
        major, minor = map(int, version.split())
    except ValueError as error:
        raise ValueError('Cannot identify the absolute Python interpreter used by GN') from error
    if (major, minor) < (3, 12):
        raise ValueError('GN uses ' + script + '; that interpreter also needs Python 3.12 or newer')
    if required <= 0 or free < required:
        raise ValueError(f'Trusted full-build runner needs at least {required} GiB free; available {free:.1f} GiB')
    return {'architecture': platform.machine(), 'free_gib': round(free, 1),
            'minimum_free_gib': required, 'gn_python': script, 'gn_python_version': f'{major}.{minor}',
            'required_host_tools_present': True, 'passed': True}


def sdk_artifact_directory(evidence, root=ROOT):
    """Select only this executed smoke's SDK; CI artifacts are not releases."""
    if isinstance(evidence, dict) and evidence.get('build_profile', 'release') != 'release':
        raise ValueError('Development profile artifacts cannot be staged as a release SDK')
    if (not isinstance(evidence, dict) or evidence.get('compiled') is not True or
            evidence.get('runtime_smoke_passed') is not True or evidence.get('sdk_staged') is not True or
            evidence.get('snapshot') != snapshot(root)):
        raise ValueError('SDK artifact upload requires a matching successful native runtime receipt')
    value = evidence.get('sdk_directory', '')
    if not isinstance(value, str) or '\n' in value or '\r' in value or '\\' in value:
        raise ValueError('Invalid SDK artifact directory')
    path = Path(value)
    if (path.is_absolute() or '..' in path.parts or len(path.parts) < 4 or
            path.parts[:3] != ('standalone', 'out', 'sdk') or path.as_posix() != value or
            any(re.fullmatch('[A-Za-z0-9._-]+', part) is None for part in path.parts)):
        raise ValueError('SDK artifact must select one directory below standalone/out/sdk')
    directory = root / path
    if not directory.is_dir() or directory.is_symlink() or not directory.resolve().is_relative_to((root / 'standalone/out/sdk').resolve()):
        raise ValueError('Validated SDK artifact directory is missing or unsafe')
    rows = evidence.get('artifacts', [])
    if (not isinstance(rows, list) or not rows or any(not isinstance(row, dict) or not isinstance(row.get('path'), str) or
                                                    not isinstance(row.get('sha256'), str) for row in rows)):
        raise ValueError('Native receipt has no valid SDK artifact inventory')
    expected = {row['path'] for row in rows}
    actual = {item.relative_to(root).as_posix() for item in directory.rglob('*') if item.is_file()}
    if len(expected) != len(rows) or expected != actual or any(item.is_symlink() for item in directory.rglob('*')):
        raise ValueError('SDK artifact inventory differs from this runtime receipt')
    for row in rows:
        item = root / row['path']
        if not item.is_file() or sha256(item) != row['sha256']:
            raise ValueError('Validated SDK artifact changed: ' + row['path'])
    return path.as_posix()


def archive_sdk_artifact(evidence, root=ROOT):
    """Archive only the selected inventory and retain executable permissions."""
    directory = root / sdk_artifact_directory(evidence, root)
    destination = root / 'standalone/out/ci' / (directory.name + '.tar.gz')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.blink-sdk-archive-', dir=destination.parent) as temporary:
        staged = Path(temporary) / destination.name
        with staged.open('wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0, compresslevel=1) as compressed:
            with tarfile.open(fileobj=compressed, mode='w') as archive:
                for row in sorted(evidence['artifacts'], key=lambda row: row['path']):
                    source = root / row['path']
                    name = directory.name + '/' + source.relative_to(directory).as_posix()
                    info = archive.gettarinfo(str(source), arcname=name)
                    info.uid = info.gid = 0
                    info.uname = info.gname = ''
                    info.mtime = 0
                    with source.open('rb') as data:
                        archive.addfile(info, data)
        sdk_artifact_directory(evidence, root)  # Reject changes while archiving.
        archive_hash = sha256(staged)
        if destination.exists():
            if not destination.is_file() or destination.is_symlink() or sha256(destination) != archive_hash:
                raise ValueError('A different SDK archive already exists; preserve and review it')
        else:
            staged.rename(destination)
    return {'sdk_directory': directory.relative_to(root).as_posix(),
            'archive_path': destination.relative_to(root).as_posix(), 'archive_sha256': archive_hash}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('version')
    commands.add_parser('ensure-baseline')
    upstream = commands.add_parser('upstream', help='Read official release APIs; never alter source locks')
    upstream.add_argument('--output', type=Path, default=ROOT / 'standalone/out/automation/upstream-candidate.json')
    release = commands.add_parser('release-plan')
    release.add_argument('--evidence', type=Path)
    release.add_argument('--candidate', type=Path)
    release.add_argument('--output', type=Path, default=ROOT / 'standalone/out/automation/release-plan.json')
    release.add_argument('--require-ready', action='store_true')
    runner = commands.add_parser('check-runner')
    runner.add_argument('--minimum-free-gib', type=int)
    sdk = commands.add_parser('sdk-artifact', help='Select exactly the successful smoke SDK for a CI artifact upload')
    sdk.add_argument('--evidence', type=Path, required=True)
    sdk.add_argument('--github-output', action='store_true')
    sdk.add_argument('--archive', action='store_true', help='Create one tar.gz preserving SDK executable permissions')
    args = parser.parse_args(argv)
    if args.command == 'ensure-baseline':
        ensure_baseline()
    elif args.command == 'version':
        print(json.dumps(identity(), indent=2))
    elif args.command == 'upstream':
        report = discover()
        write_json(args.output, report)
        print(json.dumps(report, indent=2))
    elif args.command == 'check-runner':
        print(json.dumps(runner_preflight(minimum_free_gib=args.minimum_free_gib), indent=2))
    elif args.command == 'sdk-artifact':
        evidence = read_json(args.evidence)
        outputs = archive_sdk_artifact(evidence) if args.archive else {'sdk_directory': sdk_artifact_directory(evidence)}
        if args.github_output:
            output_file = os.environ.get('GITHUB_OUTPUT')
            if not output_file:
                raise ValueError('GITHUB_OUTPUT is only available in a GitHub Actions step')
            with Path(output_file).open('a') as output:
                for key, value in outputs.items():
                    output.write(key + '=' + value + '\n')
        print(json.dumps(outputs, indent=2))
    else:
        report = release_plan(evidence=read_json(args.evidence) if args.evidence else None,
                              candidate=read_json(args.candidate) if args.candidate else None)
        write_json(args.output, report)
        print(json.dumps(report, indent=2))
        if args.require_ready and not report['publishable']:
            return 2
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print('error:', error, file=sys.stderr)
        sys.exit(2)
