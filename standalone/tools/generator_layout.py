#!/usr/bin/env python3
"""Byte-identical entry scripts keep Dawn's Python/Go paths in the view.

These copies change only filesystem layout. Never patch the generator, relax its
input checks or overwrite upstream files. Existing object directories can stay.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile

ROOT = Path(__file__).resolve().parents[2]
RECIPE = ROOT / 'standalone/config/generator-layout.json'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def value_digest(value):
    return digest(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())


def read_recipe(path=RECIPE):
    recipe = json.loads(Path(path).read_text())
    if recipe.get('schema') != 1:
        raise ValueError('Unsupported generator layout recipe')
    return recipe


def validate_recipe(revision, dependencies, recipe=None):
    recipe = read_recipe() if recipe is None else recipe
    if recipe['chromium_revision'] != revision or dependencies['chromium_revision'] != revision:
        raise ValueError('Generator layout and Chromium revision differ')
    dependency = dependencies['dependencies'].get(recipe['dependency'], {})
    if (dependency.get('dep_type') != 'git' or not dependency.get('selected_for_profile')
            or dependency.get('repository') != recipe['repository']
            or dependency.get('revision') != recipe['dependency_revision']):
        raise ValueError('Generator layout and selected dependency pin differ')
    for relative, entry in recipe['files'].items():
        path = PurePosixPath(relative)
        dependency_root = PurePosixPath(recipe['dependency'])
        selected_entry = (path.parent == dependency_root / 'generator'
                          or path == dependency_root / 'tools/generate-sources-gn.py')
        if (path.is_absolute() or '..' in path.parts or str(path) != relative
                or not selected_entry
                or path.suffix != '.py' or entry.get('mode') != 'copy'
                or entry.get('original_mode') not in ('100644', '100755')):
            raise ValueError('Invalid generator entry layout path: ' + relative)
    return recipe


def configuration_inputs(base, revision, dependencies, recipe=None):
    recipe = validate_recipe(revision, dependencies, recipe)
    return original_inputs(base, recipe)


def original_inputs(base, recipe):
    """Check the bytes and modes again when finishing a recorded upgrade."""
    inputs = {}
    for relative, entry in recipe['files'].items():
        source = Path(base) / relative
        data = source.read_bytes()
        blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        mode = 0o755 if entry['original_mode'] == '100755' else 0o644
        if (source.is_symlink() or digest(data) != entry['original_sha256']
                or blob != entry['git_blob'] or stat.S_IMODE(source.stat().st_mode) != mode):
            raise ValueError('Pristine generator entry changed: ' + relative)
        inputs[relative] = data
    return inputs


def verify_view(view, inputs, recipe=None):
    recipe = read_recipe() if recipe is None else recipe
    root = Path(view).resolve()
    for relative, data in inputs.items():
        path = root / relative
        mode = 0o755 if recipe['files'][relative]['original_mode'] == '100755' else 0o644
        if (path.is_symlink() or path.resolve() != path or not path.is_file()
                or path.read_bytes() != data or stat.S_IMODE(path.stat().st_mode) != mode):
            raise ValueError('Generator entry must be an unchanged local copy: ' + relative)


def intent_path(view):
    return Path(view).parent / (Path(view).name + '.generator-layout-upgrade.json')


def read_intent(view):
    path = intent_path(view)
    if not os.path.lexists(path):
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError('Generator layout upgrade intent must be a local file')
    return json.loads(path.read_text())


def publish_intent(view, intent):
    """Publish a complete journal exclusively; never expose a partial JSON file."""
    path = intent_path(view)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.generator-layout-',
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write((json.dumps(intent, indent=2) + '\n').encode())
            stream.flush()
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def completed_manifest(original, expected, splits):
    result = json.loads(json.dumps(original))
    result['split_sources'].update(splits)
    result['owned_files'].update(expected)
    result['generator_layout_copies'] = expected
    return result


def source_aliases(source, recipe):
    """A split Blink-root alias exposes only pinned upstream root names."""
    source = Path(source)
    names = None
    if source == ROOT:
        metadata = recipe.get('blink_python_layout', {})
        names = metadata.get('source_root_names')
        if (not isinstance(names, list) or not names
                or metadata.get('chromium_revision') != recipe.get('chromium_revision')
                or any(not isinstance(name, str) or PurePosixPath(name).name != name
                       or name in ('.', '..', '.git') for name in names)):
            raise ValueError('Blink root split requires reviewed upstream root names')
    return {child.name: str(child) for child in source.iterdir()
            if child.name != '.git' and (names is None or child.name in names)}


def plan_intent(view, base, manifest, inputs, recipe):
    """Describe each alias directory before any source-view mutation occurs."""
    splits, aliases = {}, {}
    previous = manifest.get('generator_layout_copies', {})
    for relative in inputs:
        if relative in previous:
            continue
        target = Path(view) / relative
        if target.resolve(strict=True) != (Path(base) / relative).resolve(strict=True):
            raise ValueError('Unexpected local generator entry: ' + relative)
        ancestor_will_split = False
        for parent in reversed(PurePosixPath(relative).parents):
            if str(parent) == '.':
                continue
            name = parent.as_posix()
            destination = Path(view) / name
            if destination.is_symlink() or ancestor_will_split:
                source = (Path(base) / name).resolve(strict=True)
                if not source.is_dir() or destination.resolve(strict=True) != source:
                    raise ValueError('Unexpected generator source directory: ' + name)
                splits[name] = str(source)
                aliases[name] = source_aliases(source, recipe)
                ancestor_will_split = True
    expected = {name: digest(data) for name, data in inputs.items()}
    original = json.loads(json.dumps(manifest))
    result = completed_manifest(original, expected, splits)
    return {'schema': 1, 'view': str(Path(view).resolve()),
            'source_layout': str(Path(base).resolve()),
            'recipe_sha256': value_digest(recipe),
            'inputs': {name: {'sha256': expected[name],
                               'mode': recipe['files'][name]['original_mode']}
                       for name in inputs},
            'original_manifest': original,
            'original_manifest_sha256': value_digest(original),
            'completed_manifest_sha256': value_digest(result),
            'planned_split_sources': splits, 'planned_aliases': aliases}


def validate_intent(view, base, manifest, inputs, recipe, intent):
    expected = {name: digest(data) for name, data in inputs.items()}
    recorded_inputs = {name: {'sha256': expected[name],
                              'mode': recipe['files'][name]['original_mode']}
                       for name in inputs}
    original = intent.get('original_manifest', {})
    if (intent.get('schema') != 1 or intent.get('view') != str(Path(view).resolve())
            or intent.get('source_layout') != str(Path(base).resolve())
            or intent.get('recipe_sha256') != value_digest(recipe)
            or intent.get('inputs') != recorded_inputs
            or intent.get('original_manifest_sha256') != value_digest(original)):
        raise ValueError('Generator layout upgrade intent differs from reviewed inputs')
    if ('source_layout' in original
            and Path(original['source_layout']).resolve() != Path(base).resolve()):
        raise ValueError('Generator layout upgrade source differs from its manifest')
    splits = intent.get('planned_split_sources', {})
    aliases = intent.get('planned_aliases', {})
    if set(splits) != set(aliases):
        raise ValueError('Generator layout upgrade directory plan differs')
    permitted = {parent.as_posix() for name in inputs
                 for parent in PurePosixPath(name).parents if str(parent) != '.'}
    for name, source in splits.items():
        if name not in permitted or Path(source) != (Path(base) / name).resolve(strict=True):
            raise ValueError('Unsafe generator layout upgrade directory: ' + name)
        source_path = Path(source)
        expected_aliases = source_aliases(source_path, recipe)
        if aliases[name] != expected_aliases:
            raise ValueError('Generator layout source directory changed during upgrade: ' + name)
    result = completed_manifest(original, expected, splits)
    if (intent.get('completed_manifest_sha256') != value_digest(result)
            or value_digest(manifest) not in (value_digest(original), value_digest(result))):
        raise ValueError('Generator layout upgrade manifest changed')
    return result


def verify_intermediate(view, base, intent, inputs, recipe):
    """Accept only planned aliases, split directories and exact intermediate copies."""
    splits = intent['planned_split_sources']
    original = intent['original_manifest']
    for relative, aliases in intent['planned_aliases'].items():
        directory = Path(view) / relative
        if directory.is_symlink():
            if directory.resolve(strict=True) != Path(splits[relative]):
                raise ValueError('Generator layout upgrade alias changed: ' + relative)
            continue
        # A path through an as-yet unsplit ancestor is still original source.
        if directory.resolve(strict=True) == (Path(base) / relative).resolve(strict=True):
            continue
        if not directory.is_dir() or directory.resolve() != directory:
            raise ValueError('Generator layout upgrade directory changed: ' + relative)
        if {child.name for child in directory.iterdir()} != set(aliases):
            raise ValueError('Unexpected local files in generator upgrade: ' + relative)
        for child_name, source in aliases.items():
            name = relative + '/' + child_name
            child = Path(view) / name
            if name in splits:
                # Each planned child directory is checked in its own iteration.
                continue
            if name in inputs and not child.is_symlink() and child.resolve() == child:
                verify_view(view, {name: inputs[name]}, recipe)
            elif name in original.get('owned_files', {}):
                if (child.is_symlink() or not child.is_file()
                        or digest(child.read_bytes()) != original['owned_files'][name]):
                    raise ValueError('Existing generator upgrade overlay changed: ' + name)
            elif not child.is_symlink() or child.readlink() != Path(source):
                raise ValueError('Generator layout upgrade alias changed: ' + name)


def stage(view, base, manifest, inputs, write_overlay, protected_roots, recipe=None):
    """Upgrade only reviewed aliases in a guarded view; retain all build outputs.

    The caller verifies its original manifest first, saves the upgraded
    manifest after this function, then calls finish_stage(). The intent journal
    permits exact intermediate copies to resume after an interrupted write.
    Unexpected local files without that pre-existing intent are never adopted.
    """
    recipe = read_recipe() if recipe is None else recipe
    expected = {name: digest(data) for name, data in inputs.items()}
    previous = manifest.get('generator_layout_copies', {})
    if any(name not in expected or expected[name] != value for name, value in previous.items()):
        raise ValueError('Recorded generator layout differs from the reviewed recipe')
    if previous:
        verify_view(view, {name: inputs[name] for name in previous}, recipe)
    pending = {name: data for name, data in inputs.items() if name not in previous}
    intent = read_intent(view)
    if intent is None and pending:
        intent = plan_intent(view, base, manifest, inputs, recipe)
        publish_intent(view, intent)
    if intent is None:
        verify_view(view, inputs, recipe)
        return False
    result = validate_intent(view, base, manifest, inputs, recipe, intent)
    verify_intermediate(view, base, intent, inputs, recipe)
    for relative, data in inputs.items():
        path = Path(view) / relative
        if not path.is_symlink() and path.resolve() == path:
            # Only this operation's persisted intent authorizes recovery of a
            # regular file absent from the original on-disk manifest.
            verify_view(view, {relative: data}, recipe)
            continue
        write_overlay(Path(view), relative, data, protected_roots,
                      result['split_sources'])
        path.chmod(0o755 if recipe['files'][relative]['original_mode'] == '100755' else 0o644)
    verify_intermediate(view, base, intent, inputs, recipe)
    verify_view(view, inputs, recipe)
    manifest.clear()
    manifest.update(result)
    return bool(pending)


def finish_stage(view, manifest, recipe=None):
    """Remove intent only after the caller saved exactly the completed manifest."""
    intent = read_intent(view)
    if intent is None:
        return False
    recipe = read_recipe() if recipe is None else recipe
    base = Path(intent['source_layout'])
    inputs = original_inputs(base, recipe)
    result = validate_intent(view, base, manifest, inputs, recipe, intent)
    if value_digest(manifest) != value_digest(result):
        raise ValueError('Generator layout upgrade manifest has not been completed')
    saved = Path(view).parent / 'view.json'
    if (saved.is_symlink() or not saved.is_file()
            or value_digest(json.loads(saved.read_text())) != value_digest(result)):
        raise ValueError('Generator layout upgrade completed manifest has not been saved')
    verify_intermediate(view, base, intent, inputs, recipe)
    verify_view(view, inputs, recipe)
    intent_path(view).unlink()
    return True
