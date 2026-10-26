#!/usr/bin/env python3
"""Pinned low-optimization build overlays and strict release/dev comparison.

The release profile returns its original GN arguments verbatim. Development
copies and adapts only its own guarded view; pristine inputs are never written.
"""
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import stat

ROOT = Path(__file__).resolve().parents[2]
CONFIG = Path('standalone/config/engine-profiles.json')
OPTIMIZATION_CONFIGS = {
    '//build/config/compiler:' + name
    for name in ('default_optimization', 'optimize', 'optimize_max', 'optimize_speed')
}
OPUS_TARGETS = {'//third_party/opus:' + name for name in ('opus', 'opus_avx2', 'opus_sse41')}
OPUS_PRIVATE_CONFIG = '//third_party/opus:opus_private_config'
CFLAG_REPLACEMENTS = [
    (['-O2'], ['-O0']),
    (['-O3'], ['-O0']),
]
# This exact pinned config nests :optimize_speed; GN desc exposes the outer
# config rather than its selected child. Keep its consumers explicit.
OPTIMIZATION_ALIASES = {
    '//third_party/zstd:zstd_internal_config': {
        'selected_config': '//build/config/compiler:optimize_speed',
        'source': 'third_party/zstd/BUILD.gn',
        'sha256': 'f6368bcfc407be3a784fb23dec587ca84276dd42a2bb1518b1e3a484dd5ebf71',
        'git_blob': 'f1fbe7fe41afe61e95b2c42222ff0570cdf55342',
        'targets': ['//third_party/zstd:' + name
                    for name in ('common', 'compress', 'decompress')],
    },
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load(name='release', root=ROOT):
    if name not in ('release', 'dev'):
        raise ValueError('Unknown engine build profile: ' + str(name))
    root = Path(root)
    config = json.loads((root / CONFIG).read_text())
    dependencies = json.loads((root / 'standalone/config/dependencies.lock.json').read_text())
    upstream = json.loads((root / 'standalone/config/upstream.lock.json').read_text())
    original_args = dependencies['profile']['args_gn']
    if (config.get('schema') != 1 or config['chromium_revision'] != upstream['revision']
            or dependencies['chromium_revision'] != upstream['revision']
            or config['baseline_profile'] != dependencies['profile']['name']
            or digest(original_args.encode()) != config['baseline_args_sha256']
            or config['profiles']['release']['args_gn'] != original_args
            or config['profiles']['release']['overlays']):
        raise ValueError('Engine profiles differ from the pinned release baseline')
    result = copy.deepcopy(config['profiles'][name])
    result.update(name=name, chromium_revision=config['chromium_revision'],
                  baseline_args_sha256=config['baseline_args_sha256'])
    return result


def args_text(name='release', root=ROOT):
    return load(name, root)['args_gn']


def identity(name='release', root=ROOT):
    profile = load(name, root)
    return {'name': name, 'chromium_revision': profile['chromium_revision'],
            'args_sha256': digest(profile['args_gn'].encode()),
            'baseline_args_sha256': profile['baseline_args_sha256'],
            'overlays': {relative: entry['adapted_sha256']
                         for relative, entry in profile['overlays'].items()}}


def reviewed_upgrade(previous, current, root=ROOT):
    """Only the recorded Opus correction can retain an existing dev output path."""
    if (not isinstance(previous, dict) or not isinstance(current, dict)
            or previous.get('name') != 'dev' or current.get('name') != 'dev'
            or current.get('configuration') != identity('dev', root)):
        return False
    policy = load('dev', root).get('incremental_upgrades', [])
    for entry in policy:
        if (entry.get('id') != 'opus-o1-v1'
                or entry.get('overlay_paths') != ['third_party/opus/BUILD.gn']
                or previous.get('configuration') != entry.get('from_configuration')
                or previous.get('overlays') != previous['configuration'].get('overlays')):
            continue
        old = previous['overlays']
        new = current.get('overlays', {})
        if (set(new) != set(old) | set(entry['overlay_paths'])
                or any(new.get(name) != value for name, value in old.items())
                or new != current['configuration']['overlays']):
            return False
        expected = {**previous, 'configuration': current['configuration'], 'overlays': new}
        return expected == current
    return False


def overlays(name, base, root=ROOT):
    result = {}
    for relative, entry in load(name, root)['overlays'].items():
        path = PurePosixPath(relative)
        if (relative not in ('build/config/compiler/BUILD.gn', 'third_party/opus/BUILD.gn') or path.is_absolute()
                or '..' in path.parts or str(path) != relative):
            raise ValueError('Unreviewed build-profile overlay path: ' + relative)
        source = Path(base) / relative
        data = source.read_bytes()
        blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        if (source.is_symlink() or digest(data) != entry['sha256']
                or blob != entry['git_blob'] or entry.get('original_mode') != '100644'
                or stat.S_IMODE(source.stat().st_mode) != 0o644):
            raise ValueError('Pinned build-profile overlay input changed: ' + relative)
        for change in entry['replacements']:
            old, new = change['old'].encode(), change['new'].encode()
            if data.count(old) != 1:
                raise ValueError('Build-profile overlay must replace one exact config')
            data = data.replace(old, new)
        if digest(data) != entry['adapted_sha256']:
            raise ValueError('Build-profile overlay differs from the reviewed bytes')
        result[relative] = data
    return result


def normalized(value, view, base=None):
    if isinstance(value, str):
        value = value.replace(str(Path(view)), '$SRC')
        if base is not None:
            value = value.replace(str(Path(base)), '$SRC')
        return value
    if isinstance(value, list):
        return [normalized(x, view, base) for x in value]
    if isinstance(value, dict):
        return {k: normalized(v, view, base) for k, v in value.items()}
    return value


def replace_once(values, old, new):
    starts = [i for i in range(len(values) - len(old) + 1)
              if values[i:i + len(old)] == old]
    if len(starts) != 1:
        return None
    index = starts[0]
    return values[:index] + new + values[index + len(old):]


def ordered_token_changes(before, after, mapping):
    """Map every selected token in place, preserving all other tokens exactly."""
    expected = [mapping.get(token, token) for token in before]
    if expected != after:
        return None
    changes = [{'index': index, 'old': old, 'new': new}
               for index, (old, new) in enumerate(zip(before, after))
               if old != new]
    return changes or None


def reviewed_change(label, field, before, after, node):
    if (not isinstance(before, list) or not isinstance(after, list)
            or any(not isinstance(x, str) for x in before + after)):
        return None
    configs = set(node.get('configs', []))
    configured = bool(configs & OPTIMIZATION_CONFIGS) or any(
        alias in configs and label in entry['targets']
        for alias, entry in OPTIMIZATION_ALIASES.items())
    if configured and field in ('cflags', 'cflags_c', 'cflags_cc', 'cflags_objc', 'cflags_objcc'):
        if label in OPUS_TARGETS and field == 'cflags' and OPUS_PRIVATE_CONFIG in configs:
            mapping = {'-O2': '-O0', '-O3': '-O0'}
            if [mapping.get(token, token) for token in before] + ['-O1'] == after:
                return {'kind': 'opus-codec-o1', 'final_flag': '-O1'}
        changes = ordered_token_changes(before, after, {'-O2': '-O0', '-O3': '-O0'})
        if changes:
            return {'kind': 'low-optimization-cflags', 'replacements': changes}
    if configured and field == 'rustflags':
        changes = ordered_token_changes(before, after, {'-Copt-level=3': '-Copt-level=0'})
        if changes:
            return {'kind': 'low-optimization-rustflags', 'replacements': changes}
    if field == 'configs' and label.startswith('//third_party/blink/'):
        old = ['//build/config/compiler:default_symbols']
        new = ['//build/config/compiler:no_symbols']
        if replace_once(before, old, new) == after:
            return {'kind': 'explicit-blink-symbol-level-zero', 'old': old, 'new': new}
    if field == 'configs' and label == '//third_party/opus:opus':
        speed = '//build/config/compiler:optimize_speed'
        if before.count(OPUS_PRIVATE_CONFIG) == 1 and before.count(speed) == 1:
            expected = [value for value in before if value != OPUS_PRIVATE_CONFIG]
            expected.insert(expected.index(speed) + 1, OPUS_PRIVATE_CONFIG)
            if after == expected:
                return {'kind': 'opus-private-optimization-applied-after-speed-config'}
    return None


def compare_dev(release_graph, dev_graph, release_view, dev_view, base=None):
    """All fields are equal except the explicit ordered token changes below."""
    if base is not None:
        for entry in OPTIMIZATION_ALIASES.values():
            data = (Path(base) / entry['source']).read_bytes()
            blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
            if digest(data) != entry['sha256'] or blob != entry['git_blob']:
                raise ValueError('Pinned nested optimization config changed: ' + entry['source'])
    added = sorted(set(dev_graph) - set(release_graph))
    removed = sorted(set(release_graph) - set(dev_graph))
    changes, unreviewed = {}, {}
    for label in sorted(set(release_graph) & set(dev_graph)):
        before = normalized(release_graph[label], release_view, base)
        after = normalized(dev_graph[label], dev_view, base)
        fields = {}
        for field in sorted(set(before) | set(after)):
            if before.get(field) == after.get(field):
                continue
            approved = reviewed_change(label, field, before.get(field), after.get(field), before)
            fields[field] = {'release': before.get(field), 'dev': after.get(field),
                             'approved_change': approved}
            if approved is None:
                unreviewed.setdefault(label, []).append(field)
        if fields:
            changes[label] = fields
    return {'schema': 1, 'release_target_count': len(release_graph),
            'dev_target_count': len(dev_graph), 'added_targets': added,
            'removed_targets': removed, 'changed_targets': changes,
            'unreviewed_changes': unreviewed,
            'passed': not added and not removed and not unreviewed,
            'normalization': 'Exact source-view/root prefixes only; output-relative paths and every target field remain compared.',
            'allowed_changes': {
                'cflags': [{'old': old, 'new': new} for old, new in CFLAG_REPLACEMENTS],
                'rustflags': {'old': ['-Copt-level=3'], 'new': ['-Copt-level=0']},
                'nested_optimization_configs': copy.deepcopy(OPTIMIZATION_ALIASES),
                'blink_configs': {'old': ['//build/config/compiler:default_symbols'],
                                  'new': ['//build/config/compiler:no_symbols']},
                'restriction': 'Map every reviewed optimization token in its original position, preserving list length and every other token. Require an original selected optimization config or the exact reviewed nested config and consumer. Symbol-config substitution is restricted to Blink-owned targets. No source, dependency, macro, include, action argument or library waiver.'}}
