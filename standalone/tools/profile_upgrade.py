"""Keep one reviewed Opus profile fix in its original physical build directory.

Only a previously owned profile and a pristine Opus directory alias may be
upgraded. Native outputs, Ninja state, generated module maps and PCMs are never
visited. A journal lets a completed directory publication resume after a crash.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

OPUS = 'third_party/opus'
OVERLAY = OPUS + '/BUILD.gn'


def configuration_key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:20]


def read(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('Profile upgrade metadata must be a local regular file: ' + str(path))
    return json.loads(path.read_text())


def atomic_bytes(path, data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.profile-upgrade-', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        temporary.chmod(0o644)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_json(path, value):
    atomic_bytes(path, (json.dumps(value, indent=2) + '\n').encode())


def check_directory(path, source, data, complete=True):
    if path.is_symlink() or path.resolve() != path or not path.is_dir():
        raise ValueError('Profile upgrade directory is not local: ' + str(path))
    expected = {p.name for p in source.iterdir() if p.name != '.git'}
    actual = {p.name for p in path.iterdir()}
    if not actual.issubset(expected) or complete and actual != expected:
        raise ValueError('Unexpected files in Opus profile upgrade directory')
    for name in actual:
        item = path / name
        if name == 'BUILD.gn':
            if item.is_symlink() or not item.is_file() or item.read_bytes() != data:
                raise ValueError('Opus profile overlay differs from the reviewed bytes')
        elif not item.is_symlink() or item.readlink() != source / name:
            raise ValueError('Opus source alias changed during profile upgrade: ' + name)


def publish_directory(view, source, data):
    target = view / OPUS
    stage = view.parent / 'opus-profile-upgrade-stage'
    if stage.exists() or stage.is_symlink():
        check_directory(stage, source, data, complete=False)
    else:
        stage.mkdir()
    for child in source.iterdir():
        if child.name in ('.git', 'BUILD.gn'):
            continue
        alias = stage / child.name
        if not os.path.lexists(alias):
            alias.symlink_to(child, target_is_directory=child.is_dir())
    if not (stage / 'BUILD.gn').exists():
        atomic_bytes(stage / 'BUILD.gn', data)
    check_directory(stage, source, data)
    if target.is_symlink():
        if target.readlink() != source:
            raise ValueError('Opus directory is not the original source alias')
        target.unlink()
    if not os.path.lexists(target):
        stage.rename(target)
    else:
        check_directory(target, source, data)
        shutil.rmtree(stage)  # Only the exact checked staging aliases are removed.


def select_view(context, key_data, canonical_key, *, approve, verify_previous, verify_current):
    """Return an approved existing physical view, or None for normal new staging.

    Callbacks validate the pinned old/new profile policy and the complete view.
    The caller retains control of current.json and subsequent GN regeneration.
    """
    if context.get('profile') != 'dev' or not context.get('isolated'):
        return None
    if configuration_key(key_data) != canonical_key:
        raise ValueError('Profile upgrade received an inconsistent configuration key')
    pointer = Path(context['current_path'])
    if not os.path.lexists(pointer):
        return None
    current = read(pointer)
    view = Path(current['view'])
    manifest_path = view.parent / 'view.json'
    manifest = read(manifest_path)
    if (view != Path(context['work_dir']) / 'views' / manifest['key'] / 'src'
            or current['key'] != manifest['key'] or view.resolve() != view
            or (view / 'third_party').resolve() != view / 'third_party'):
        raise ValueError('Profile upgrade must retain its original local physical view')
    expected = key_data['build_profile']
    if expected['name'] != 'dev' or expected['cache'] != context['cache']:
        raise ValueError('Profile upgrade namespace/cache mode differs')
    if manifest.get('configuration_key') == canonical_key:
        receipt = manifest.get('profile_upgrade', {})
        previous = receipt.get('from_profile')
        old_key = configuration_key({**key_data, 'build_profile': previous})
        if (manifest.get('build_profile') != expected or not approve(previous, expected)
                or receipt.get('from_configuration_key') != old_key or old_key != manifest['key']
                or receipt.get('to_configuration_key') != canonical_key
                or receipt.get('to_profile') != expected):
            raise ValueError('Recorded profile upgrade receipt changed')
        verify_current(view, manifest)
        source = Path(key_data['source_layout']) / OPUS
        check_directory(view / OPUS, source, context['overlays'][OVERLAY])
        journal = view.parent / 'profile-upgrade-intent.json'
        if os.path.lexists(journal):
            intent = read(journal)
            if intent.get('view') != str(view) or intent.get('new_manifest') != manifest:
                raise ValueError('Completed profile upgrade journal changed')
            journal.unlink()
        return view
    if (Path(context['work_dir']) / 'views' / canonical_key).exists():
        return None
    journal = view.parent / 'profile-upgrade-intent.json'
    original = read(journal)['old_manifest'] if os.path.lexists(journal) else manifest
    previous = original.get('build_profile')
    if not approve(previous, expected):
        return None
    old_key = configuration_key({**key_data, 'build_profile': previous})
    if original['key'] != old_key or original.get('profile_upgrade'):
        return None  # Unrelated inputs changed, or this is not the one legacy transition.
    if manifest != original:
        raise ValueError('Profile upgrade manifest changed before commit')
    old_overlays, new_overlays = previous['overlays'], expected['overlays']
    if (set(new_overlays) - set(old_overlays) != {OVERLAY}
            or any(new_overlays.get(name) != sha for name, sha in old_overlays.items())
            or any(original['owned_files'].get(name) != sha for name, sha in old_overlays.items())
            or OVERLAY in original['owned_files']):
        raise ValueError('Profile upgrade must add only the reviewed Opus overlay')
    verify_previous(view, original)
    data = context['overlays'][OVERLAY]
    if hashlib.sha256(data).hexdigest() != new_overlays[OVERLAY]:
        raise ValueError('Profile upgrade Opus bytes differ from the current policy')
    source = Path(key_data['source_layout']) / OPUS
    if source.is_symlink() or source.resolve() != source or not source.is_dir():
        raise ValueError('Profile upgrade requires the original local Opus source directory')
    target = view / OPUS
    if not os.path.lexists(journal) and (not target.is_symlink() or target.readlink() != source):
        raise ValueError('Unrecorded local Opus input must be preserved')
    updated = copy.deepcopy(original)
    updated.update(configuration_key=canonical_key, build_profile=expected)
    updated['owned_files'][OVERLAY] = new_overlays[OVERLAY]
    updated['split_sources'][OPUS] = str(source)
    updated['profile_upgrade'] = {'schema': 1, 'from_configuration_key': old_key,
        'to_configuration_key': canonical_key, 'from_profile': previous, 'to_profile': expected}
    intent = {'schema': 1, 'view': str(view), 'old_manifest': original, 'new_manifest': updated}
    if os.path.lexists(journal):
        if read(journal) != intent:
            raise ValueError('Profile upgrade journal differs from the reviewed transition')
    else:
        atomic_json(journal, intent)
    publish_directory(view, source, data)
    atomic_json(manifest_path, updated)
    verify_current(view, updated)
    journal.unlink()
    return view
