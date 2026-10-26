"""Retain native work for pinned project snapshot/export config corrections.

The linked project GN file is edited explicitly before prepare. Only view
metadata is published here; no upstream file, GN output or native object is
written. An interrupted atomic manifest write can be retried from the old state.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile

FIELD = 'project_build_upgrade'


def key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:20]


def reviewed(previous, current, policy):
    stages = [policy.get('old_sha256'), *policy.get('intermediate_sha256', []),
              policy.get('new_sha256')]
    if (policy.get('schema') != 1 or policy.get('id') != 'snapshot-startup-config-v1'
            or policy.get('path') != 'standalone/engine/BUILD.gn'
            or any(not isinstance(stage, str) for stage in stages)
            or len(set(stages)) != len(stages)):
        raise ValueError('Unreviewed project build upgrade policy')
    return (isinstance(previous, dict) and isinstance(current, dict)
            and previous.get(policy['path']) in stages
            and current.get(policy['path']) in stages
            and stages.index(previous[policy['path']]) < stages.index(current[policy['path']])
            and current == {**previous, policy['path']: current[policy['path']]})


def inputs(manifest):
    result = {'revision': manifest['chromium_revision'], 'recipe': manifest['recipe'],
              'source_layout': manifest['source_layout'], 'layout_copies': manifest['layout_copies'],
              'project_build_inputs': manifest['project_build_inputs'],
              'project_sources': manifest['project_sources'],
              'gn': {p.removeprefix('standalone/gn/'): sha for p, sha in manifest['owned_files'].items()
                     if p.startswith('standalone/gn/')}}
    if 'build_profile' in manifest:
        result['build_profile'] = manifest['build_profile']
    return result


def verify(manifest, policy, live=None):
    receipt = manifest.get(FIELD)
    if not isinstance(receipt, dict):
        raise ValueError('Project build upgrade receipt is missing')
    old = inputs(manifest)
    new = {**old, 'project_build_inputs': receipt.get('inputs')}
    prior = manifest.get('engine_view_upgrade', {})
    if (receipt.get('schema') != 1 or receipt.get('id') != policy['id']
            or manifest['chromium_revision'] != policy['chromium_revision']
            or not reviewed(old['project_build_inputs'], new['project_build_inputs'], policy)
            or receipt.get('from_configuration_key') != key(old)
            or receipt.get('to_configuration_key') != key(new)
            or manifest.get('configuration_key') != key(new)
            or prior.get('to_configuration_key') != key(old)
            or live is not None and live != new):
        raise ValueError('Project build upgrade inputs or key chain changed')
    return True


def atomic_json(path, value):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.project-upgrade-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write((json.dumps(value, indent=2) + '\n').encode())
        temporary.chmod(0o644)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def select_view(context, live, canonical_key, policy, verify_previous, verify_current):
    if key(live) != canonical_key:
        raise ValueError('Project upgrade received an inconsistent key')
    pointer = Path(context['current_path'])
    if not os.path.lexists(pointer):
        return None
    if pointer.is_symlink() or not pointer.is_file():
        raise ValueError('Project upgrade pointer must be a local regular file')
    current = json.loads(pointer.read_text())
    view = Path(current['view'])
    manifest_path = view.parent / 'view.json'
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError('Project upgrade manifest must be a local regular file')
    manifest = json.loads(manifest_path.read_text())
    if (view != Path(context['work_dir']) / 'views' / manifest['key'] / 'src'
            or current['key'] != manifest['key'] or view.resolve() != view):
        raise ValueError('Project upgrade must retain its original local view')
    if manifest.get(FIELD):
        verify(manifest, policy)
        expected = {**inputs(manifest), 'project_build_inputs': manifest[FIELD]['inputs']}
        if live != expected:
            if (not reviewed(expected['project_build_inputs'], live['project_build_inputs'], policy)
                    or live != {**expected, 'project_build_inputs': live['project_build_inputs']}):
                return None
            verify_previous(view, manifest, expected['project_build_inputs'])
            updated = copy.deepcopy(manifest)
            updated['configuration_key'] = canonical_key
            updated[FIELD]['inputs'] = live['project_build_inputs']
            updated[FIELD]['to_configuration_key'] = canonical_key
            verify(updated, policy, live)
            verify_current(view, updated)
            atomic_json(manifest_path, updated)
            return view
        verify(manifest, policy, live)
        verify_current(view, manifest)
        return view
    old = inputs(manifest)
    if (manifest['chromium_revision'] != policy['chromium_revision']
            or not reviewed(old['project_build_inputs'], live['project_build_inputs'], policy)
            or live != {**old, 'project_build_inputs': live['project_build_inputs']}
            or manifest.get('configuration_key') != key(old)
            or manifest.get('engine_view_upgrade', {}).get('to_configuration_key') != key(old)):
        return None
    verify_previous(view, manifest, old['project_build_inputs'])
    updated = copy.deepcopy(manifest)
    updated['configuration_key'] = canonical_key
    updated[FIELD] = {'schema': 1, 'id': policy['id'], 'inputs': live['project_build_inputs'],
                      'from_configuration_key': key(old), 'to_configuration_key': canonical_key}
    verify(updated, policy, live)
    verify_current(view, updated)
    atomic_json(manifest_path, updated)
    return view
