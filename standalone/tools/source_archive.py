"""Materialize immutable official Gitiles archives against a Git blob inventory.

Archive bytes are transport only. The caller's original Git tree remains the
authority, including files that an archive exporter might omit or transform.
"""
from __future__ import annotations

import hashlib
import http.client
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import materialize


class ArchiveUnavailable(RuntimeError):
    """The official archive transport failed; no source integrity claim was made."""


def archive_url(repository, revision):
    parsed = urllib.parse.urlsplit(repository)
    if (parsed.scheme != 'https' or not parsed.hostname
            or not parsed.hostname.endswith('.googlesource.com')
            or parsed.username or parsed.password or parsed.port
            or parsed.query or parsed.fragment
            or '..' in PurePosixPath(parsed.path).parts):
        raise ValueError('Archive source must be an official HTTPS googlesource repository')
    if not re.fullmatch('[0-9a-f]{40}', revision):
        raise ValueError('Archive revision must be a full immutable Git commit ID')
    return repository.rstrip('/').removesuffix('.git') + '/+archive/' + revision + '.tar.gz'


def safe_name(name):
    path = PurePosixPath(name)
    if (not name or not path.parts or path.is_absolute() or '..' in path.parts
            or '\\' in name or '\0' in name):
        raise ValueError('Unsafe source archive path: ' + repr(name))
    return path.as_posix()


def check_ancestors(path):
    """Reject symlinks even above the requested destination, before any writes."""
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError('Source archive destination ancestor is a symlink: ' + str(current))


def fetch_archive(repository, revision, work_dir, label):
    url = archive_url(repository, revision)
    cache = work_dir / 'source-archives'
    check_ancestors(cache)
    cache.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(repository.rstrip('/').encode()).hexdigest()[:20] + '-' + revision
    archive = cache / (key + '.tar.gz')
    partial = archive.with_suffix(archive.suffix + '.partial')
    for path in (archive, partial):
        if os.path.lexists(path) and (path.is_symlink() or not path.is_file()):
            raise ValueError('Unexpected source archive cache path: ' + str(path))
    if archive.exists():
        return archive, url
    last_error = None
    for attempt in range(3):
        offset = partial.stat().st_size if partial.exists() else 0
        headers = {'Range': f'bytes={offset}-'} if offset else {}
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                if response.status == 206:
                    content_range = response.headers.get('Content-Range', '')
                    if not content_range.startswith(f'bytes {offset}-'):
                        raise ValueError('Source archive resume range mismatch')
                    mode = 'ab'
                elif response.status == 200:
                    # Gitiles normally ignores Range. Restart an incomplete
                    # transfer rather than appending a second gzip stream.
                    mode = 'wb'
                else:
                    raise ArchiveUnavailable(f'Unexpected archive HTTP status: {response.status}')
                print(f'{label}: downloading official pinned archive', flush=True)
                received = 0
                progress = 64 * 1024 * 1024
                with partial.open(mode) as output:
                    while data := response.read(1024 * 1024):
                        output.write(data)
                        received += len(data)
                        if received >= progress:
                            print(f'{label}: downloaded {received // (1024 * 1024)} MiB of archive', flush=True)
                            progress += 64 * 1024 * 1024
                length = response.headers.get('Content-Length')
                if length is not None and received != int(length):
                    raise OSError('Source archive transport was incomplete')
            partial.replace(archive)
            return archive, url
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError, ArchiveUnavailable) as error:
            last_error = error
            if attempt < 2:
                time.sleep(attempt + 1)
    raise ArchiveUnavailable(f'{label}: official source archive unavailable: {last_error}')


def blob_hash(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def verify_archive(archive, stage, entries, label=None):
    expected = {}
    for mode, oid, name in entries:
        normalized = safe_name(name)
        if normalized != name or name in expected or mode not in ('100644', '100755', '120000'):
            raise ValueError('Invalid original Git source inventory: ' + name)
        expected[name] = (mode, oid)
    seen = set()
    byte_count = 0
    try:
        with tarfile.open(archive, mode='r|gz') as tar:
            for member in tar:
                if member.isdir() and PurePosixPath(member.name) == PurePosixPath('.'):
                    continue
                name = safe_name(member.name)
                if member.isdir():
                    if name in expected:
                        raise ValueError('Archive directory replaces an original Git blob: ' + name)
                    continue
                if name not in expected:
                    raise ValueError('Extra file in source archive: ' + name)
                if name in seen:
                    raise ValueError('Duplicate file in source archive: ' + name)
                mode, oid = expected[name]
                if mode == '120000':
                    if not member.issym():
                        raise ValueError('Archive symlink type mismatch: ' + name)
                    data = os.fsencode(member.linkname)
                else:
                    if not member.isfile():
                        raise ValueError('Archive regular-file type mismatch: ' + name)
                    if bool(member.mode & stat.S_IXUSR) != (mode == '100755'):
                        raise ValueError('Archive executable mode mismatch: ' + name)
                    source = tar.extractfile(member)
                    if source is None:
                        raise ValueError('Archive file body is absent: ' + name)
                    with source:
                        data = source.read()
                    if len(data) != member.size:
                        raise ValueError('Archive file body is truncated: ' + name)
                if blob_hash(data) != oid:
                    raise ValueError('Archive Git blob mismatch: ' + name)
                materialize.check_parents(stage, name)
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                if mode == '120000':
                    target.symlink_to(os.fsdecode(data))
                else:
                    with target.open('xb') as output:
                        output.write(data)
                    target.chmod(0o755 if mode == '100755' else 0o644)
                seen.add(name)
                byte_count += len(data)
                if label and len(seen) % 10000 == 0:
                    print(f'{label}: verified {len(seen)}/{len(expected)} archive Git blobs', flush=True)
    except (tarfile.TarError, EOFError) as error:
        raise ValueError('Invalid or truncated pinned source archive') from error
    missing = set(expected) - seen
    if missing:
        raise ValueError('Original Git files missing from source archive: ' + repr(sorted(missing)[:10]))
    return byte_count


def validate_destination(destination, entries):
    check_ancestors(destination)
    existing = 0
    for mode, oid, name in entries:
        materialize.check_parents(destination, name)
        path = destination / name
        if os.path.lexists(path):
            materialize.validate_existing(path, mode, oid)
            existing += 1
    return existing


def restore_archive(repository, revision, destination, entries, work_dir, label):
    """Verify a complete archive in isolation before installing any source file."""
    destination, work_dir = Path(destination), Path(work_dir)
    entries = list(entries)
    archive_url(repository, revision)
    for _, _, name in entries:
        if safe_name(name) != name:
            raise ValueError('Invalid original source inventory path: ' + name)
    validate_destination(destination, entries)
    archive, url = fetch_archive(repository, revision, work_dir, label)
    check_ancestors(archive.parent)
    with tempfile.TemporaryDirectory(prefix='verified-source-', dir=archive.parent) as temporary:
        stage = Path(temporary)
        total_bytes = verify_archive(archive, stage, entries, label)
        # A download can take minutes: recheck existing sources before installing.
        existing = validate_destination(destination, entries)
        restored, restored_bytes = 0, 0
        for index, (mode, oid, name) in enumerate(entries, 1):
            if index % 10000 == 0:
                print(f'{label}: installing verified source {index}/{len(entries)}', flush=True)
            materialize.check_parents(destination, name)
            target = destination / name
            if os.path.lexists(target):
                materialize.validate_existing(target, mode, oid)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = stage / name
            if mode == '120000':
                text = os.readlink(source)
                target.symlink_to(text)
                restored_bytes += len(os.fsencode(text))
            else:
                materialize.install_file(target, mode, source=source)
                restored_bytes += source.stat().st_size
            restored += 1
    inventory = ''.join(f'{mode} {oid}\t{name}\n' for mode, oid, name in entries).encode()
    with archive.open('rb') as stream:
        transport_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
    print(f'{label}: verified archive against {len(entries)} Git blobs, restored {restored} files', flush=True)
    return {'revision': revision, 'tree_file_count': len(entries), 'verified_existing': existing,
            'restored_files': restored, 'restored_bytes': restored_bytes,
            'fetched_blobs': 0, 'inventory_sha256': hashlib.sha256(inventory).hexdigest(),
            'transport': 'official_gitiles_archive', 'archive_url': url,
            'archive_sha256': transport_hash, 'archive_bytes': archive.stat().st_size,
            'verified_source_bytes': total_bytes}
