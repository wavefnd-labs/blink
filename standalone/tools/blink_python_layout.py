#!/usr/bin/env python3
"""Review pristine Blink entry copies that need Chromium-shaped Python paths.

Filesystem layout is the only adaptation. The shared generator_layout journal
stages these alongside Dawn's entries without changing the existing build key.
"""
import hashlib
import json
from pathlib import Path, PurePosixPath
import stat

ROOT = Path(__file__).resolve().parents[2]
RECIPE = ROOT / 'standalone/config/blink-python-layout.json'
PREFIX = 'third_party/blink/'
BINDINGS = 'third_party/blink/renderer/bindings/scripts'
PROBES = 'third_party/blink/renderer/build/scripts/make_instrumenting_probes.py'


def read_recipe(path=RECIPE):
    return json.loads(Path(path).read_text())


def validate_recipe(revision, recipe=None, upstream_lock=None):
    recipe = read_recipe() if recipe is None else recipe
    if upstream_lock is None:
        upstream_lock = json.loads((ROOT / 'standalone/config/upstream.lock.json').read_text())
    if (recipe.get('schema') != 1 or recipe.get('chromium_revision') != revision
            or upstream_lock.get('revision') != revision
            or recipe.get('source_root_names') != upstream_lock.get('protected_roots')
            or recipe.get('repository') != 'https://chromium.googlesource.com/chromium/src'):
        raise ValueError('Blink Python layout and official Chromium revision differ')
    for relative, entry in recipe.get('files', {}).items():
        path = PurePosixPath(relative)
        if (path.is_absolute() or '..' in path.parts or str(path) != relative
                or not (str(path.parent) == BINDINGS or relative == PROBES)
                or path.suffix != '.py' or entry.get('mode') != 'copy'
                or entry.get('source_path') != relative.removeprefix(PREFIX)
                or entry.get('original_mode') not in ('100644', '100755')):
            raise ValueError('Invalid Blink Python entry layout path: ' + relative)
    if not recipe.get('files'):
        raise ValueError('Blink Python layout has no reviewed entries')
    return recipe


def configuration_inputs(base, revision, recipe=None, upstream_lock=None):
    recipe = validate_recipe(revision, recipe, upstream_lock)
    result = {}
    for relative, entry in recipe['files'].items():
        source = Path(base) / relative
        data = source.read_bytes()
        blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        mode = 0o755 if entry['original_mode'] == '100755' else 0o644
        if (source.is_symlink() or hashlib.sha256(data).hexdigest() != entry['original_sha256']
                or blob != entry['git_blob'] or stat.S_IMODE(source.stat().st_mode) != mode):
            raise ValueError('Pristine Blink Python entry changed: ' + relative)
        result[relative] = data
    return result


def combined_recipe(dawn_recipe, revision, recipe=None, upstream_lock=None):
    """Keep one journal and copy manifest for the reviewed generator entries."""
    recipe = validate_recipe(revision, recipe, upstream_lock)
    if dawn_recipe.get('chromium_revision') != revision:
        raise ValueError('Blink and Dawn Python layout revisions differ')
    result = json.loads(json.dumps(dawn_recipe))
    if set(result['files']) & set(recipe['files']):
        raise ValueError('Blink and Dawn Python layout paths overlap')
    result['files'].update(recipe['files'])
    result['blink_python_layout'] = recipe
    return result


def selected_entry_coverage(graph, recipe=None):
    """Report the exact selected action consumers of the reviewed copies."""
    recipe = read_recipe() if recipe is None else recipe
    selected = {}
    for target, node in graph.items():
        script = node.get('script', '').removeprefix('//')
        if script in recipe['files']:
            selected.setdefault(script, []).append(target)
    missing = set(recipe['files']) - set(selected)
    if missing:
        raise ValueError('Reviewed Blink Python entry absent from selected graph: '
                         + ', '.join(sorted(missing)))
    return {name: sorted(targets) for name, targets in sorted(selected.items())}
