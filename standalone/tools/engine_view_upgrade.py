"""Apply only the pinned shared-library TLS recipe to an existing engine view.

The two GN overlays are staged beside the view. Original sources and every
native output remain untouched; Ninja subsequently decides what needs rebuilding.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile

PATHS = ('third_party/blink/renderer/platform/heap/BUILD.gn', 'v8/BUILD.gn')
RECEIPT = 'engine_view_upgrade'
JOURNAL = 'engine-view-upgrade-intent.json'
STAGE = 'engine-view-upgrade-stage'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def key(value):
    return digest(value)[:20]


def bytes_digest(value):
    return hashlib.sha256(value).hexdigest()


def read(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('Engine upgrade metadata must be a local regular file')
    return json.loads(path.read_text())


def atomic_bytes(path, data, tempdir):
    """Keep the original alias until the complete replacement is written."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=tempdir, prefix='.engine-upgrade-', delete=False) as output:
            temporary = Path(output.name)
            output.write(data)
        temporary.chmod(0o644)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_json(path, value):
    atomic_bytes(path, (json.dumps(value, indent=2) + '\n').encode(), path.parent)


def validate_policy(policy):
    if (policy.get('schema') != 1 or policy.get('id') != 'tls-library-v1'
            or policy.get('overlay_paths') != list(PATHS)
            or policy.get('from_recipe_sha256') == policy.get('to_recipe_sha256')):
        raise ValueError('Unreviewed engine view upgrade policy')


def manifest_inputs(manifest):
    result = {'revision': manifest['chromium_revision'], 'source_layout': manifest['source_layout'],
        'layout_copies': manifest['layout_copies'], 'project_build_inputs': manifest['project_build_inputs'],
        'project_sources': manifest['project_sources'],
        'gn': {name.removeprefix('standalone/gn/'): sha
               for name, sha in manifest['owned_files'].items() if name.startswith('standalone/gn/')}}
    if 'build_profile' in manifest:
        result['build_profile'] = manifest['build_profile']
    return result


def verify_receipt(manifest, policy, key_data=None):
    """Verify the immutable Opus→TLS key chain without reading file contents.

    With no live key_data, all recorded key inputs are reconstructed from the
    manifest, including the owned GN file hashes. Source directory aliases are
    resolved to their canonical paths. Normal view verification must
    still verify those files and the pinned profile/source inputs themselves.
    """
    validate_policy(policy)
    receipt = manifest.get(RECEIPT)
    if receipt is None:
        return False
    inputs = manifest_inputs(manifest)
    live = {name: value for name, value in (key_data or {}).items() if name != 'recipe'}
    previous = receipt.get('from_recipe', {})
    current = manifest['recipe']
    before = key({**inputs, 'recipe': previous})
    after = key({**inputs, 'recipe': current})
    if (receipt.get('schema') != 1 or receipt.get('id') != policy['id']
            or manifest['chromium_revision'] != policy['chromium_revision']
            or digest(previous) != policy['from_recipe_sha256']
            or digest(current) != policy['to_recipe_sha256']
            or receipt.get('key_inputs') != inputs or key_data is not None and live != inputs
            or key_data is not None and key_data.get('recipe') != current
            or receipt.get('from_configuration_key') != before
            or receipt.get('to_configuration_key') != after
            or manifest.get('project_build_upgrade', {}).get(
                'from_configuration_key', manifest.get('configuration_key')) != after
            or receipt.get('profile_upgrade') != manifest.get('profile_upgrade')):
        raise ValueError('Engine upgrade receipt or configuration inputs changed')
    profile_receipt = manifest.get('profile_upgrade')
    if profile_receipt:
        original = {**inputs, 'build_profile': profile_receipt.get('from_profile'), 'recipe': previous}
        if (profile_receipt.get('schema') != 1
                or profile_receipt.get('from_configuration_key') != manifest['key']
                or profile_receipt.get('to_configuration_key') != before
                or profile_receipt.get('to_profile') != inputs.get('build_profile')
                or key(original) != manifest['key']):
            raise ValueError('Opus and engine upgrade keys are not continuous')
    elif before != manifest['key']:
        raise ValueError('Engine upgrade does not start from its original view key')
    for name in PATHS:
        if (name in previous['files'] or name not in current['files']
                or manifest['owned_files'].get(name) != current['files'][name]['adapted_sha256']):
            raise ValueError('Engine upgrade overlay inventory changed')
    expected_sources = {str(Path(name).parent): str((Path(inputs['source_layout']) / name).parent.resolve())
                        for name in PATHS}
    recorded = receipt.get('split_directories', {})
    if (any(expected_sources.get(name) != source for name, source in recorded.items())
            or any(manifest['split_sources'].get(name) != source for name, source in recorded.items())):
        raise ValueError('Engine upgrade source directory inventory changed')
    return True


def original_overlay(base, name, entry, data):
    source = base / name
    if source.is_symlink() or not source.is_file():
        raise ValueError('Engine upgrade requires the original regular GN input')
    result = source.read_bytes()
    if bytes_digest(result) != entry['sha256']:
        raise ValueError('Original engine upgrade GN input changed')
    for change in entry['replacements']:
        old, new = change['old'].encode(), change['new'].encode()
        if result.count(old) != 1:
            raise ValueError('Engine upgrade must replace one exact original occurrence')
        result = result.replace(old, new)
    if result != data or bytes_digest(result) != entry['adapted_sha256']:
        raise ValueError('Engine upgrade overlay differs from the reviewed recipe')


def local_directory(path):
    if os.path.lexists(path):
        if path.is_symlink() or not path.is_dir() or path.resolve() != path:
            raise ValueError('Engine upgrade staging directory is not local')
    else:
        path.mkdir()


def directory_contents(path, source, data, complete=True):
    if path.is_symlink() or not path.is_dir() or path.resolve() != path:
        raise ValueError('Engine upgrade directory is not local')
    expected = {p.name for p in source.iterdir() if p.name != '.git'}
    actual = {p.name for p in path.iterdir()}
    if not actual.issubset(expected) or complete and actual != expected:
        raise ValueError('Unexpected local files in engine upgrade directory')
    for name in actual:
        child = path / name
        if name == 'BUILD.gn' and not child.is_symlink():
            if not child.is_file() or child.read_bytes() != data:
                raise ValueError('Engine upgrade overlay was changed locally')
        elif not child.is_symlink() or child.readlink() != source / name:
            raise ValueError('Engine upgrade source alias was changed locally')
    if complete and (path / 'BUILD.gn').is_symlink():
        raise ValueError('Engine upgrade overlay has not been staged')


def plans_for(view, base):
    result = []
    for name in PATHS:
        parent = Path(name).parent
        destination = view / parent
        source = (base / parent).resolve(strict=True)
        if destination.is_symlink() and destination.resolve(strict=True) == source:
            result.append({'path': name, 'kind': 'directory', 'link': str(destination.readlink())})
        elif (destination.is_dir() and destination.resolve() == destination
                and (view / name).is_symlink() and (view / name).resolve(strict=True) == source / 'BUILD.gn'):
            result.append({'path': name, 'kind': 'file', 'link': str((view / name).readlink())})
        else:
            raise ValueError('Unrecorded local engine upgrade input must be preserved: ' + name)
    return result


def stage_and_publish(view, base, plans, overlays, write_overlay):
    # Keep the caller API stable; its ordinary overlay writer may leave a
    # truncated file after a failed write, so stage GN bytes atomically here.
    container = view.parent / STAGE
    local_directory(container)
    stage = container / 'src'
    local_directory(stage)
    for plan in plans:
        name = plan['path']
        parent = Path(name).parent
        source = (base / parent).resolve(strict=True)
        expected_original = source if plan['kind'] == 'directory' else source / 'BUILD.gn'
        if Path(plan['link']).resolve() != expected_original:
            raise ValueError('Engine upgrade journal contains a different original alias')
        current = stage
        for part in parent.parts:
            current /= part
            local_directory(current)
        if plan['kind'] == 'directory':
            directory_contents(current, source, overlays[name], complete=False)
            children = [p for p in source.iterdir() if p.name != '.git']
        elif plan['kind'] == 'file':
            if any(p.name != 'BUILD.gn' for p in current.iterdir()):
                raise ValueError('Unexpected local files in engine upgrade staging')
            children = [source / 'BUILD.gn']
        else:
            raise ValueError('Unknown engine upgrade publication kind')
        for child in children:
            alias = current / child.name
            if not os.path.lexists(alias):
                alias.symlink_to(child, target_is_directory=child.is_dir())
        staged_file = stage / name
        if staged_file.is_symlink():
            if staged_file.readlink() != source / 'BUILD.gn':
                raise ValueError('Engine upgrade staging alias changed')
            atomic_bytes(staged_file, overlays[name], view.parent)
        elif not staged_file.is_file() or staged_file.read_bytes() != overlays[name]:
            raise ValueError('Engine upgrade staging overlay changed')
        destination = view / (parent if plan['kind'] == 'directory' else Path(name))
        staged = current if plan['kind'] == 'directory' else staged_file
        if plan['kind'] == 'directory':
            directory_contents(staged, source, overlays[name])
        if destination.is_symlink():
            if str(destination.readlink()) != plan['link']:
                raise ValueError('Engine upgrade original alias changed before publication')
            if plan['kind'] == 'file':
                staged.replace(destination)
                continue
            destination.unlink()
        if not os.path.lexists(destination):
            staged.rename(destination)
        elif plan['kind'] == 'directory':
            directory_contents(destination, source, overlays[name])
            for child in staged.iterdir():
                child.unlink()  # Exact checked aliases or the reviewed staged file.
            staged.rmdir()
        elif not destination.is_file() or destination.read_bytes() != overlays[name]:
            raise ValueError('Published engine upgrade overlay changed')
        else:
            staged.unlink()


def finish(view):
    container = view.parent / STAGE
    paths = {container, container / 'src'}
    for name in PATHS:
        paths.update((container / 'src' / name).parents)
    for path in sorted((p for p in paths if p == container or container in p.parents),
                       key=lambda p: len(p.parts), reverse=True):
        if os.path.lexists(path):
            if path.is_symlink() or not path.is_dir():
                raise ValueError('Unexpected engine upgrade staging entry')
            path.rmdir()  # Never recursively delete unreviewed files.
    (view.parent / JOURNAL).unlink(missing_ok=True)


def select_view(context, key_data, canonical_key, policy, overlays,
                verify_previous, verify_current, write_overlay):
    """Return an approved retained view, otherwise None for normal new staging."""
    validate_policy(policy)
    if key(key_data) != canonical_key:
        raise ValueError('Engine upgrade canonical key differs from its inputs')
    if (policy['chromium_revision'] != key_data['revision']
            or digest(key_data['recipe']) != policy['to_recipe_sha256']):
        return None
    pointer = Path(context['current_path'])
    if not os.path.lexists(pointer):
        return None
    current = read(pointer)
    view = Path(current['view'])
    manifest_path = view.parent / 'view.json'
    manifest = read(manifest_path)
    if (view != Path(context['work_dir']) / 'views' / manifest['key'] / 'src'
            or current['key'] != manifest['key'] or view.resolve() != view):
        raise ValueError('Engine upgrade must retain its original local physical view')
    if manifest.get(RECEIPT):
        verify_receipt(manifest, policy)
        if manifest_inputs(manifest) != {k: v for k, v in key_data.items() if k != 'recipe'}:
            return None
        verify_receipt(manifest, policy, key_data)
        verify_current(view, manifest)
        for parent, source in manifest[RECEIPT]['split_directories'].items():
            directory_contents(view / parent, Path(source), overlays[parent + '/BUILD.gn'])
        journal = view.parent / JOURNAL
        if os.path.lexists(journal) and read(journal).get('new_manifest') != manifest:
            raise ValueError('Completed engine upgrade journal changed')
        finish(view)
        return view
    if digest(manifest['recipe']) != policy['from_recipe_sha256']:
        return None
    before = key({**key_data, 'recipe': manifest['recipe']})
    if (before != manifest.get('configuration_key', manifest['key'])
            or manifest_inputs(manifest) != {k: v for k, v in key_data.items() if k != 'recipe'}):
        return None  # A profile, compiler/cache identity or unrelated input changed.
    if set(key_data['recipe']['files']) - set(manifest['recipe']['files']) != set(PATHS):
        raise ValueError('Engine upgrade must add exactly the two reviewed GN overlays')
    if any(name in manifest['owned_files'] for name in PATHS) or set(overlays) != set(PATHS):
        raise ValueError('Engine upgrade overlay ownership differs from its policy')
    verify_previous(view, manifest)
    base = Path(key_data['source_layout'])
    for name in PATHS:
        original_overlay(base, name, key_data['recipe']['files'][name], overlays[name])
    journal = view.parent / JOURNAL
    existing = read(journal) if os.path.lexists(journal) else None
    plans = existing['plans'] if existing else plans_for(view, base)
    if [p.get('path') for p in plans] != list(PATHS):
        raise ValueError('Engine upgrade journal overlay inventory changed')
    updated = copy.deepcopy(manifest)
    updated.update(recipe=key_data['recipe'], configuration_key=canonical_key)
    split = {str(Path(p['path']).parent): str((base / p['path']).parent.resolve())
             for p in plans if p['kind'] == 'directory'}
    updated['split_sources'].update(split)
    updated['owned_files'].update({name: bytes_digest(data) for name, data in overlays.items()})
    updated[RECEIPT] = {'schema': 1, 'id': policy['id'], 'from_recipe': manifest['recipe'],
        'from_configuration_key': before, 'to_configuration_key': canonical_key,
        'key_inputs': {k: v for k, v in key_data.items() if k != 'recipe'},
        'profile_upgrade': copy.deepcopy(manifest.get('profile_upgrade')), 'split_directories': split}
    verify_receipt(updated, policy, key_data)
    intent = {'schema': 1, 'old_manifest': manifest, 'new_manifest': updated, 'plans': plans}
    if existing:
        if existing != intent:
            raise ValueError('Engine upgrade journal changed before commit')
    else:
        atomic_json(journal, intent)
    stage_and_publish(view, base, plans, overlays, write_overlay)
    atomic_json(manifest_path, updated)
    verify_current(view, updated)
    finish(view)
    return view
