#!/usr/bin/env python3
"""Engine-owned GN entry point and guarded source view; never builds a browser."""
import argparse
from collections import Counter, deque
import gzip
import hashlib
import importlib.util
import json
import os
import posixpath
import re
from pathlib import Path, PurePosixPath
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
SUPPORT = ROOT / 'standalone'
BASE = SUPPORT / 'work/graph/src'
WORK = SUPPORT / 'work/engine'
GN = SUPPORT / 'work/graph/tools/gn'
NINJA = BASE / 'third_party/ninja/ninja'
TARGET = '//:blink_engine'
BROWSER_ROOTS = {'chrome', 'content', 'headless', 'android_webview', 'ios'}
BROWSER_RESOURCE_PREFIXES = ('ui/webui/resources',)
PROJECT_DIRS = ('common', 'engine', 'include', 'platform')


def support_module(name):
    spec = importlib.util.spec_from_file_location('engine_' + name, SUPPORT / ('tools/' + name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_context(profile='release', cache=None, download_cache=False):
    """Opt-in profiles have their own view, pointer, reports and native outputs."""
    if profile not in ('release', 'dev'):
        raise ValueError('Unknown engine build profile: ' + profile)
    cached = profile == 'dev' if cache is None else cache
    if profile == 'release' and cached:
        raise ValueError('Compiler cache is opt-in through --profile dev; the release build stays unchanged')
    isolated = profile != 'release' or cached
    context = {'profile': profile, 'cache': cached, 'isolated': isolated,
               'work_dir': WORK, 'report_dir': SUPPORT / 'reports',
               'current_path': WORK / 'current.json', 'overlays': {}, 'identity': {}}
    if isolated:
        namespace = profile + ('-cache' if cached else '-no-cache')
        context.update(work_dir=WORK / 'profiles' / namespace,
                       report_dir=SUPPORT / 'reports/profiles' / namespace)
        context['current_path'] = context['work_dir'] / 'current.json'
        profiles = support_module('build_profiles')
        context['identity'] = profiles.identity(profile)
        context['args'] = profiles.args_text(profile)
        context['overlays'] = profiles.overlays(profile, BASE)
        path = SUPPORT / 'config/engine-profiles.json'
        context['upgrade_policy'] = (read_json(path)['profiles'][profile].get('incremental_upgrades', [])
                                     if path.is_file() else [])
    if cached:
        helper = support_module('compiler_cache')
        prepared = helper.prepare(download=download_cache)
        context['compiler_cache'] = prepared
        context['cache_identity'] = {'pin': helper.load(),
                                     'wrapper_sha256': prepared['wrapper_sha256']}
        context['args'] += '\ncc_wrapper = ' + json.dumps(prepared['wrapper']) + '\n'
    return context


def report_path(name, context=None):
    return (context['report_dir'] if context else SUPPORT / 'reports') / name


def current_path(context=None):
    return context['current_path'] if context else WORK / 'current.json'


def layout_module():
    spec = importlib.util.spec_from_file_location('engine_rustfmt_layout', SUPPORT / 'tools/rustfmt_layout.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def generator_layout_module():
    spec = importlib.util.spec_from_file_location('engine_generator_layout', SUPPORT / 'tools/generator_layout.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def blink_python_layout_module():
    spec = importlib.util.spec_from_file_location('engine_blink_python_layout', SUPPORT / 'tools/blink_python_layout.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def generator_layout_inputs(revision, dependencies, hydrate_missing=False):
    layout = generator_layout_module()
    recipe = layout.validate_recipe(revision, dependencies)
    missing = [name for name in recipe['files'] if not (BASE / name).exists()]
    if missing and hydrate_missing:
        # Hydrate only explicit official dependency files, as for other GN
        # metadata inputs. Verification alone never downloads anything.
        declared = read_json(SUPPORT / 'work/graph/declared-deps.json')
        selected = declared.get('dependencies', {}).get('src/' + recipe['dependency'], {})
        if (declared.get('chromium_revision') != revision
                or declared.get('deps_sha256') != dependencies['root_deps_sha256']
                or selected.get('repository') != recipe['repository']
                or selected.get('revision') != recipe['dependency_revision']):
            raise ValueError('Generator hydration metadata differs from the reviewed pin')
        wanted = [str(PurePosixPath(name).relative_to(recipe['dependency'])) for name in missing]
        subprocess.run([sys.executable, '-B', str(SUPPORT / 'tools/graph/hydrate.py'),
                        'dep', recipe['dependency'], *wanted], check=True)
    copies = layout.configuration_inputs(BASE, revision, dependencies)
    blink_layout = blink_python_layout_module()
    copies.update(blink_layout.configuration_inputs(BASE, revision))
    recipe = blink_layout.combined_recipe(recipe, revision)
    return layout, copies, recipe


def build_environment(context=None):
    """Generators must never leave Python bytecode in pristine source trees."""
    result = os.environ.copy()
    result.pop('PYTHONPATH', None)
    result['PYTHONDONTWRITEBYTECODE'] = '1'
    work = context['work_dir'] if context else WORK
    result['PYTHONPYCACHEPREFIX'] = str(work / 'python-cache')
    if context and context.get('cache'):
        result = support_module('compiler_cache').environment(result, context['compiler_cache'])
    return result


def check_build_prerequisites(view, context=None):
    spec = importlib.util.spec_from_file_location('engine_build_prerequisites', SUPPORT / 'tools/build_prerequisites.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    kwargs = ({'report_path': report_path('linux-x64-build-prerequisites.json', context),
               'diagnostics_dir': context['work_dir'] / 'diagnostics/prerequisites'}
              if context and context['isolated'] else {})
    report = module.check(view, **kwargs)
    print(f"Build prerequisites: {report['audited_action_scripts']} reviewed scripts, import/tool checks passed", flush=True)


def runtime_checks_module():
    spec = importlib.util.spec_from_file_location('engine_runtime_checks', SUPPORT / 'tools/runtime_checks.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def project_build_inputs():
    return {path.relative_to(ROOT).as_posix(): digest(path.read_bytes())
            for name in PROJECT_DIRS for path in sorted((SUPPORT / name).rglob('*'))
            if path.is_file() and (path.name == 'BUILD.gn' or path.suffix == '.gni')}


def link_project_sources(view, projects):
    for name, original in projects.items():
        source = Path(original)
        if name not in PROJECT_DIRS or source != SUPPORT / name or not source.is_dir() or source.is_symlink():
            raise ValueError('Invalid engine integration source directory: ' + name)
        destination = view / 'standalone' / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(destination):
            if not destination.is_symlink() or destination.readlink() != source:
                raise ValueError('Engine integration source link changed: ' + name)
        else:
            destination.symlink_to(source, target_is_directory=True)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, indent=2) + '\n')


def safe_relative(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or not path.parts or str(path) != name:
        raise ValueError('Invalid build-view path: ' + name)
    return path


def adapt_bytes(original, entry):
    if digest(original) != entry['sha256']:
        raise ValueError('Upstream GN input changed; review the adaptation recipe')
    result = original
    for change in entry['replacements']:
        old, new = change['old'].encode(), change['new'].encode()
        if result.count(old) != 1:
            raise ValueError('Adaptation must replace exactly one original occurrence')
        result = result.replace(old, new)
    if digest(result) != entry['adapted_sha256']:
        raise ValueError('Adapted GN input differs from reviewed recipe')
    return result


def detach_directory(path, original_blink_names, split_sources=None, view=None):
    """Split only the view's symlink; preserve every original source byte."""
    if path.is_symlink():
        source = path.resolve(strict=True)
        if not source.is_dir():
            raise ValueError('Expected directory in source view: ' + str(path))
        children = [p for p in source.iterdir() if p.name != '.git']
        if split_sources is not None:
            split_sources[path.relative_to(view).as_posix()] = str(source)
        if source == ROOT:
            children = [p for p in children if p.name in original_blink_names]
        path.unlink()
        path.mkdir()
        for child in children:
            (path / child.name).symlink_to(child, target_is_directory=child.is_dir())
    elif not path.exists():
        path.mkdir()
    elif not path.is_dir():
        raise ValueError('Expected view directory: ' + str(path))


def write_overlay(view, relative, data, original_blink_names, split_sources=None):
    relative = safe_relative(relative)
    current = view
    for part in relative.parts[:-1]:
        current = current / part
        detach_directory(current, original_blink_names, split_sources, view)
    destination = view / relative.as_posix()
    if destination.is_symlink():
        destination.unlink()
    elif destination.exists():
        raise ValueError('Refusing to overwrite a local build-view file: ' + str(destination))
    with destination.open('xb') as stream:
        stream.write(data)


def verify_blink():
    subprocess.run([sys.executable, '-B', str(SUPPORT / 'tools/blink.py'), 'verify'], check=True)


def verify_implicit_sources(context=None):
    command = [sys.executable, '-B', str(SUPPORT / 'tools/materialize_implicit_sources.py'), '--verify']
    if context and context['isolated']:
        command.extend(['--report-path', str(report_path('materialized-implicit-sources.json', context))])
    subprocess.run(command, check=True)


def refresh_sources(view, manifest, original_blink_names):
    """Expose newly hydrated sources in split directories without losing overlays."""
    for relative, original in manifest.get('split_sources', {}).items():
        source = Path(original)
        for child in source.iterdir():
            if child.name == '.git' or source == ROOT and child.name not in original_blink_names:
                continue
            name = relative + '/' + child.name
            destination = view / name
            if name in manifest['owned_files'] or name in manifest['split_sources']:
                continue
            if os.path.lexists(destination):
                if not destination.is_symlink() or destination.readlink() != child:
                    raise ValueError('Unexpected local source in build view: ' + name)
            else:
                destination.symlink_to(child, target_is_directory=child.is_dir())


def prepare(context=None):
    verify_blink()
    if (not (BASE / 'build/config/gclient_args.gni').exists() or not GN.is_file()
            or not (SUPPORT / 'work/graph/declared-deps.json').is_file()):
        subprocess.run([sys.executable, '-B', str(SUPPORT / 'tools/graph/prepare.py')], check=True)
    lock = read_json(SUPPORT / 'config/upstream.lock.json')
    for name in read_json(SUPPORT / 'config/rustfmt-layout.json')['files']:
        if not (BASE / safe_relative(name)).exists():
            fetch_original_input(name)
    layout_copies = layout_module().configuration_inputs(BASE, lock['revision'])
    dependencies = read_json(SUPPORT / 'config/dependencies.lock.json')
    if dependencies['chromium_revision'] != lock['revision']:
        raise ValueError('Resolve a matching dependency baseline before generating the engine')
    generator_layout, generator_copies, generator_recipe = generator_layout_inputs(
        lock['revision'], dependencies, hydrate_missing=True)
    if subprocess.check_output(['git', '-C', str(BASE), 'rev-parse', 'HEAD'], text=True).strip() != lock['revision']:
        raise ValueError('Cached source workspace has a different revision; prepare a fresh workspace')
    if digest(GN.read_bytes()) != dependencies['profile']['gn_sha256']:
        raise ValueError('GN executable differs from the pinned dependency profile')
    recipe = read_json(SUPPORT / 'config/engine-adaptations.json')
    if recipe['chromium_revision'] != lock['revision']:
        raise ValueError('Adaptations and Blink upstream revision differ')
    if digest((ROOT / recipe['patch']['path']).read_bytes()) != recipe['patch']['sha256']:
        raise ValueError('Recorded engine import patch changed')
    if digest((SUPPORT / 'gn/VERSION').read_bytes()) != recipe['version_metadata']['original_sha256']:
        raise ValueError('Engine compatibility version differs from the recorded upstream version')
    key_data = {'revision': lock['revision'], 'recipe': recipe, 'source_layout': str(BASE),
                'layout_copies': {name: digest(data) for name, data in layout_copies.items()},
                'project_build_inputs': project_build_inputs(),
                'project_sources': {name: str(SUPPORT / name) for name in PROJECT_DIRS if (SUPPORT / name).is_dir()},
                'gn': {p.name: digest(p.read_bytes()) for p in sorted((SUPPORT / 'gn').iterdir()) if p.is_file()}}
    # Retain the existing release key byte for byte. Every opt-in profile,
    # including its compiler wrapper and reviewed source overlays, is isolated.
    if context and context['isolated']:
        key_data['build_profile'] = profile_identity(context)
    key = digest(json.dumps(key_data, sort_keys=True).encode())[:20]
    work = context['work_dir'] if context else WORK
    view = work / 'views' / key / 'src'
    retained = None
    project_policy = SUPPORT / 'config/project-build-upgrades.json'
    if project_policy.is_file():
        retained = support_module('project_build_upgrade').select_view(
            context or {'work_dir': work, 'current_path': current_path()},
            key_data, key, read_json(project_policy),
            verify_previous=lambda candidate, record, old: verify_view(
                candidate, record, approved_previous_project_inputs=old),
            verify_current=verify_view)
        if retained is not None:
            view = retained
    upgrade_path = SUPPORT / 'config/engine-view-upgrades.json'
    if retained is None and upgrade_path.is_file():
        upgrades = support_module('engine_view_upgrade')
        policy = read_json(upgrade_path)
        overlays = {name: adapt_bytes((BASE / name).read_bytes(), recipe['files'][name])
                    for name in policy['overlay_paths']}
        retained = upgrades.select_view(
            context or {'work_dir': work, 'current_path': current_path()},
            key_data, key, policy, overlays,
            verify_previous=verify_view, verify_current=verify_view,
            write_overlay=lambda candidate, name, data, split: write_overlay(
                candidate, name, data, set(lock['protected_roots']), split))
        if retained is not None:
            view = retained
    if retained is None and context and context['isolated'] and context.get('upgrade_policy'):
        profiles = support_module('build_profiles')
        retained = support_module('profile_upgrade').select_view(
            context, key_data, key, approve=profiles.reviewed_upgrade,
            verify_previous=lambda candidate, record: verify_view(
                candidate, record, approved_previous_profile=record['build_profile']),
            verify_current=verify_view)
        if retained is not None:
            view = retained
    manifest_path = view.parent / 'view.json'
    if manifest_path.exists():
        manifest = read_json(manifest_path)
        if context and context['isolated'] and manifest.get('build_profile') != profile_identity(context):
            raise ValueError('Recorded development profile differs from its view identity')
        verify_view(view, manifest, allow_layout_upgrade=True)
        # Byte-identical entry copies do not change compile/GN contents or the
        # view identity. Upgrade the layout in place to retain completed objects.
        if generator_layout.stage(view, BASE, manifest, generator_copies,
                                  write_overlay, set(lock['protected_roots']), recipe=generator_recipe):
            write_json(manifest_path, manifest)
        generator_layout.finish_stage(view, manifest, recipe=generator_recipe)
        refresh_sources(view, manifest, set(lock['protected_roots']))
        verify_view(view, manifest)
        pointer = {'view': str(view), 'key': manifest['key']}
        if manifest.get('configuration_key'):
            pointer['configuration_key'] = manifest['configuration_key']
        write_json(current_path(context), pointer)
        return view
    if view.exists():
        raise ValueError('Incomplete build view exists; preserve it and use a fresh view')
    view.mkdir(parents=True)
    # Include future top-level dependency folders as dangling source links. They
    # become available when original locked dependencies are materialized.
    tops = set(p.name for p in BASE.iterdir())
    tops.update(subprocess.check_output(['git', '-C', str(BASE), 'ls-tree', '--name-only', lock['revision']], text=True).splitlines())
    for name in sorted(tops - BROWSER_ROOTS - {'.git', '.gn', 'BUILD.gn', 'out', 'standalone'}):
        source = BASE / name
        (view / name).symlink_to(source, target_is_directory=source.is_dir())
    owned, split_sources = {}, {}
    names = set(lock['protected_roots'])
    for source in sorted((SUPPORT / 'gn').iterdir()):
        if source.is_file():
            relative = 'standalone/gn/' + source.name
            write_overlay(view, relative, source.read_bytes(), names, split_sources)
            owned[relative] = digest(source.read_bytes())
    for relative, source in (('.gn', SUPPORT / 'gn/dotfile.gn'), ('BUILD.gn', SUPPORT / 'gn/BUILD.gn')):
        write_overlay(view, relative, source.read_bytes(), names, split_sources)
        owned[relative] = digest(source.read_bytes())
    for relative, data in layout_copies.items():
        write_overlay(view, relative, data, names, split_sources)
        owned[relative] = digest(data)
    link_project_sources(view, key_data['project_sources'])
    for relative, entry in recipe['files'].items():
        result = adapt_bytes((BASE / relative).read_bytes(), entry)
        write_overlay(view, relative, result, names, split_sources)
        owned[relative] = digest(result)
    for relative, result in (context or {}).get('overlays', {}).items():
        if relative in owned:
            raise ValueError('Build profile must not overwrite an engine adaptation: ' + relative)
        write_overlay(view, relative, result, names, split_sources)
        owned[relative] = digest(result)
    manifest = {'schema': 1, 'chromium_revision': lock['revision'], 'recipe': recipe,
                'key': key, 'owned_files': owned, 'source_layout': str(BASE), 'split_sources': split_sources,
                'layout_copies': {name: digest(data) for name, data in layout_copies.items()},
                'project_sources': key_data['project_sources'],
                'project_build_inputs': key_data['project_build_inputs'],
                'excluded_browser_roots': sorted(BROWSER_ROOTS)}
    if context and context['isolated']:
        manifest['build_profile'] = profile_identity(context)
    generator_layout.stage(view, BASE, manifest, generator_copies,
                           write_overlay, names, recipe=generator_recipe)
    write_json(manifest_path, manifest)
    generator_layout.finish_stage(view, manifest, recipe=generator_recipe)
    verify_view(view, manifest)
    write_json(current_path(context), {'view': str(view), 'key': key})
    return view


def verify_view(view, manifest, allow_layout_upgrade=False, approved_previous_profile=None,
                approved_previous_project_inputs=None):
    if manifest.get('build_profile'):
        profile = manifest['build_profile']
        profiles = support_module('build_profiles')
        current = {**profile, 'configuration': profiles.identity(profile['name']),
                   'overlays': {name: digest(data) for name, data in
                                profiles.overlays(profile['name'], BASE).items()}}
        if profile != current:
            if (approved_previous_profile != profile
                    or not profiles.reviewed_upgrade(profile, current)):
                raise ValueError('Recorded development profile or original compiler input changed')
        if manifest.get('profile_upgrade'):
            receipt = manifest['profile_upgrade']
            profile_end_key = manifest.get('engine_view_upgrade', {}).get(
                'from_configuration_key', manifest.get('configuration_key'))
            if (receipt.get('schema') != 1 or receipt.get('to_profile') != profile
                    or receipt.get('from_configuration_key') != manifest['key']
                    or receipt.get('to_configuration_key') != profile_end_key
                    or not profiles.reviewed_upgrade(receipt.get('from_profile'), profile)):
                raise ValueError('Recorded development profile upgrade changed')
    if manifest.get('engine_view_upgrade'):
        support_module('engine_view_upgrade').verify_receipt(
            manifest, read_json(SUPPORT / 'config/engine-view-upgrades.json'))
    if manifest.get('project_build_upgrade'):
        support_module('project_build_upgrade').verify(
            manifest, read_json(SUPPORT / 'config/project-build-upgrades.json'))
    if 'generator_layout_copies' in manifest:
        layout, copies, recipe = generator_layout_inputs(
            manifest['chromium_revision'], read_json(SUPPORT / 'config/dependencies.lock.json'))
        reviewed = {name: digest(data) for name, data in copies.items()}
        recorded = manifest['generator_layout_copies']
        if (any(reviewed.get(name) != expected for name, expected in recorded.items())
                or not allow_layout_upgrade and reviewed != recorded):
            raise ValueError('Generator layout differs from the reviewed recipe')
        layout.verify_view(view, {name: copies[name] for name in recorded}, recipe=recipe)
    if 'project_sources' in manifest:
        for name, original in manifest['project_sources'].items():
            destination = view / 'standalone' / name
            if (name not in PROJECT_DIRS or Path(original) != SUPPORT / name
                    or not destination.is_symlink() or destination.readlink() != Path(original)):
                raise ValueError('Engine integration source link changed: ' + name)
        recorded = manifest.get('project_build_upgrade', {}).get(
            'inputs', manifest.get('project_build_inputs'))
        if recorded != project_build_inputs():
            if (approved_previous_project_inputs != recorded
                    or not support_module('project_build_upgrade').reviewed(
                        recorded, project_build_inputs(),
                        read_json(SUPPORT / 'config/project-build-upgrades.json'))):
                raise ValueError('Engine integration GN files changed; regenerate the source view')
    if manifest.get('layout_copies'):
        layout = layout_module()
        copies = layout.configuration_inputs(BASE, manifest['chromium_revision'])
        if {name: digest(data) for name, data in copies.items()} != manifest['layout_copies']:
            raise ValueError('Source layout adaptation differs from its reviewed inputs')
        layout.verify_view(view, copies)
    for name in BROWSER_ROOTS:
        if os.path.lexists(view / name):
            raise ValueError('Browser source directory entered the engine view: ' + name)
    for relative, expected in manifest['owned_files'].items():
        path = view / safe_relative(relative)
        if path.is_symlink() or not path.is_file() or digest(path.read_bytes()) != expected:
            raise ValueError('Engine build-view adaptation changed: ' + relative)
    for relative, entry in manifest['recipe']['files'].items():
        if digest((BASE / relative).read_bytes()) != entry['sha256']:
            raise ValueError('Original adaptation input changed: ' + relative)


def profile_identity(context):
    return {'name': context['profile'], 'cache': context['cache'],
            'configuration': context['identity'], 'args_gn': context['args'],
            'overlays': {name: digest(data) for name, data in context['overlays'].items()},
            'compiler_cache': context.get('cache_identity')}


def paths(context=None):
    view = prepare(context)
    return view, view / 'out/Engine'


def compare_graphs(baseline, engine, view, approved_rewrites=None, approved_removals=None,
                   approved_config_effects=None, approved_shared_library_tls=None):
    def normalize(value):
        if isinstance(value, str):
            return value.replace(str(BASE), '$SRC').replace(str(view), '$SRC').replace('out/BlinkGraph', 'out/$PROFILE').replace('out/Engine', 'out/$PROFILE')
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        return value
    removed = sorted(set(baseline) - set(engine))
    # A reviewed dependency cut may drop its now-unreachable build tools and
    # resources. Pin every removed target's complete normalized baseline node;
    # never accept a prefix-based waiver or arbitrary missing engine library.
    approved_removals = approved_removals or {}
    unreviewed_removals = [label for label in removed if approved_removals.get(label) !=
                          digest(json.dumps(normalize(baseline[label]), sort_keys=True).encode())]
    stale_removals = sorted(set(approved_removals) - set(removed))
    changed = {label: sorted(key for key in set(baseline[label]) | set(engine[label])
                             if normalize(baseline[label].get(key)) != normalize(engine[label].get(key)))
               for label in sorted(set(baseline) & set(engine))}
    changed = {label: fields for label, fields in changed.items() if fields}
    residual = dict(changed)
    for label, fields in (approved_rewrites or {}).items():
        if label not in baseline or label not in engine:
            raise ValueError('Reviewed target is absent: ' + label)
        expected = dict(baseline[label])
        for field, replacements in fields.items():
            if field == 'visibility':
                if (set(replacements) != {'old', 'new'}
                        or not isinstance(replacements['old'], list)
                        or replacements['new'] != replacements['old'] + ['//standalone/engine:blink_embed']):
                    raise ValueError('Only the exact reviewed engine visibility addition is allowed')
                if expected.get(field) != replacements['old']:
                    raise ValueError('Reviewed visibility no longer matches the baseline')
                expected[field] = replacements['new']
                continue
            if field in ('sources', 'deps'):
                if (not isinstance(replacements, dict) or set(replacements) != {'remove'}
                        or not isinstance(replacements['remove'], list)
                        or not replacements['remove']):
                    raise ValueError('Source/dependency adaptations require exact reviewed removals')
                expected[field] = list(expected[field])
                for value in replacements['remove']:
                    if expected[field].count(value) != 1:
                        raise ValueError('Reviewed source/dependency removal no longer matches the baseline')
                    expected[field].remove(value)
                continue
            if field not in ('inputs', 'args'):
                raise ValueError('Only reviewed metadata, SDK visibility or source/dependency cuts may change')
            expected[field] = list(expected[field])
            for replacement in replacements:
                if expected[field].count(replacement['old']) != 1:
                    raise ValueError('Reviewed version metadata no longer matches the baseline')
                expected[field] = [replacement['new'] if value == replacement['old'] else value for value in expected[field]]
        differences = sorted(key for key in set(expected) | set(engine[label]) if normalize(expected.get(key)) != normalize(engine[label].get(key)))
        if differences:
            residual[label] = differences
        else:
            residual.pop(label, None)
    # Removing a dependency also removes (or reorders) its inherited configs
    # and system libraries in downstream nodes. Review these separately from
    # direct source/dependency cuts, with complete ordered lists on both sides.
    # This exception cannot introduce a compiler flag, macro, include or library.
    for label, fields in (approved_config_effects or {}).items():
        if label not in baseline or label not in engine:
            raise ValueError('Reviewed config-effect target is absent: ' + label)
        for field, values in fields.items():
            if (field not in ('all_dependent_configs', 'configs', 'defines', 'include_dirs', 'libs', 'cflags')
                    or not isinstance(values, dict) or set(values) != {'old', 'new'}
                    or not isinstance(values['old'], list) or not isinstance(values['new'], list)
                    or any(not isinstance(v, str) for v in values['old'] + values['new'])
                    or any(not isinstance(value, str) for value in values['old'] + values['new'])
                    or Counter(values['new']) - Counter(values['old'])
                    or values['old'] == values['new']):
                raise ValueError('Config effects allow only exact inherited list removals/reordering')
            if (normalize(baseline[label].get(field)) != values['old']
                    or normalize(engine[label].get(field)) != values['new']):
                raise ValueError('Reviewed inherited config effect differs: ' + label + ' ' + field)
            remaining = [name for name in residual.get(label, []) if name != field]
            if remaining:
                residual[label] = remaining
            else:
                residual.pop(label, None)
    # Shared-library TLS uses the upstream accessors rather than local-exec
    # relocations. Authorize only its exact generated flag and V8 internal
    # define; this is separate from the removal-only config-effects policy.
    tls = approved_shared_library_tls or {}
    if tls:
        if set(tls) != {'blink_buildflags', 'v8_defines'} or not tls['v8_defines']:
            raise ValueError('Invalid shared-library TLS review')
        blink_label = '//third_party/blink/renderer/platform/heap:blink_heap_buildflags'
        flag = tls['blink_buildflags']
        if flag != {'target': blink_label,
                    'old': ['--flags', 'BLINK_HEAP_INSIDE_SHARED_LIBRARY=false'],
                    'new': ['--flags', 'BLINK_HEAP_INSIDE_SHARED_LIBRARY=true']}:
            raise ValueError('Only the upstream Blink shared-library heap flag is allowed')
        entries = [(blink_label, 'response_file_contents', flag)]
        for label, values in tls['v8_defines'].items():
            if (not label.startswith('//v8:') and not label.startswith('//v8/')
                    or label not in baseline or label not in engine
                    or '//v8:features' not in baseline[label].get('configs', [])
                    or '//v8:features' not in engine[label].get('configs', [])
                    or not isinstance(values, dict) or set(values) != {'old', 'new'}
                    or not isinstance(values['old'], list) or not isinstance(values['new'], list)
                    or 'V8_TLS_USED_IN_LIBRARY' in values['old']
                    or values['new'].count('V8_TLS_USED_IN_LIBRARY') != 1
                    or [v for v in values['new'] if v != 'V8_TLS_USED_IN_LIBRARY'] != values['old']):
                raise ValueError('Only the upstream V8 internal library TLS define is allowed')
            entries.append((label, 'defines', values))
        for label, field, values in entries:
            if (label not in baseline or label not in engine
                    or normalize(baseline[label].get(field)) != values['old']
                    or normalize(engine[label].get(field)) != values['new']):
                raise ValueError('Reviewed shared-library TLS config differs: ' + label)
            remaining = [name for name in residual.get(label, []) if name != field]
            if remaining:
                residual[label] = remaining
            else:
                residual.pop(label, None)
    return {'baseline_target_count': len(baseline), 'added_targets': sorted(set(engine) - set(baseline)),
            'removed_targets': removed, 'changed_targets': changed,
            'baseline_targets_unchanged': not removed and not changed,
            'reviewed_target_rewrites': approved_rewrites or {}, 'unreviewed_changes': residual,
            'reviewed_removed_targets': approved_removals,
            'unreviewed_removed_targets': unreviewed_removals,
            'stale_reviewed_removals': stale_removals,
            'reviewed_inherited_config_effects': approved_config_effects or {},
            'reviewed_shared_library_tls': tls,
            'matches_reviewed_adaptations': not unreviewed_removals and not stale_removals and not residual,
            'normalization': 'Source root and output-profile paths only; all target fields compared'}


def generate(context=None):
    view, output = paths(context)
    flags = ['--root=' + str(view)]
    args = context.get('args') if context and context['isolated'] else (SUPPORT / 'config/linux-x64-graph.args.gn').read_text()
    command = [str(GN), 'gen', str(output), *flags, '--args=' + args, '--tracelog=' + str(view.parent / 'gn-trace.json')]
    seen = set()
    for _ in range(100):
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                env=build_environment(context))
        (view.parent / 'gn-gen.log').write_text(result.stdout)
        print(result.stdout, end='', flush=True)
        if not result.returncode:
            break
        candidates = re.findall(r"[\"']" + re.escape(str(view)) + r"/([^\"'\n]+)[\"']", result.stdout)
        missing = next((name for name in candidates if not (view / name).exists()), None)
        if missing is None or missing in seen:
            raise subprocess.CalledProcessError(result.returncode, command)
        seen.add(missing)
        fetch_original_input(missing)
        prepare(context)  # refresh split directory links after metadata hydration
        if not (view / missing).exists():
            raise ValueError('Missing generated/package input needs explicit preparation: ' + missing)
    else:
        raise ValueError('Too many missing GN inputs')
    write_json(view.parent / 'generation-command.json', command)
    return view, output


def fetch_original_input(relative):
    safe_relative(relative)
    if relative.split('/', 1)[0] in BROWSER_ROOTS:
        raise ValueError('Refusing to restore browser inputs into an engine build: ' + relative)
    spec = importlib.util.spec_from_file_location('engine_hydration', SUPPORT / 'tools/graph/hydrate.py')
    hydration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hydration)
    owners = sorted((name.removeprefix('src/') for name, entry in hydration.DEPS.items()
                     if entry.get('dep_type') == 'git'), key=len, reverse=True)
    owner = next((name for name in owners if relative == name or relative.startswith(name + '/')), None)
    if owner:
        hydration.dependency(owner)
        if not (BASE / relative).exists():
            hydration.dependency(owner, [relative[len(owner) + 1:]])
    else:
        if relative.startswith('third_party/blink/'):
            raise ValueError('Missing Blink input must be reviewed against the pristine source tree')
        hydration.hydrate(hydration.CACHE, hydration.REV, BASE, [relative])


def browser_source_path(relative, policy=None):
    policy = policy or {}
    prefixes = BROWSER_RESOURCE_PREFIXES + tuple(policy.get('forbidden_source_prefixes', []))
    return (relative.split('/', 1)[0] in BROWSER_ROOTS
            or relative in policy.get('forbidden_source_files', [])
            or any(relative == path or relative.startswith(path + '/') for path in prefixes))


def browser_file_references(graph, view, policy=None):
    found = []
    source_root = BASE.resolve()
    canonical_paths = {}
    for label, node in graph.items():
        for field in ('sources', 'inputs', 'data', 'script', 'public', 'include_dirs', 'lib_dirs', 'args'):
            values = node.get(field, [])
            for value in [values] if isinstance(values, str) else values:
                candidate = value.split('=', 1)[-1]
                for prefix in ('//', '../../', str(view) + '/', str(BASE) + '/'):
                    if not candidate.startswith(prefix):
                        continue
                    relative = posixpath.normpath(candidate[len(prefix):])
                    if relative not in canonical_paths:
                        real = (Path(view) / relative).resolve()
                        canonical_paths[relative] = (real.is_relative_to(source_root)
                            and browser_source_path(real.relative_to(source_root).as_posix(), policy))
                    if browser_source_path(relative, policy) or canonical_paths[relative]:
                        found.append({'target': label, 'field': field, 'path': value})
                        break
    return found


def check_declared_inputs(ninja, output, view, targets, context=None):
    """Check all Ninja source leaves; missing generated outputs have build rules."""
    command = [ninja, '-C', str(output), '-t', 'inputs', *targets]
    text = subprocess.check_output(command, text=True)
    sources, missing = set(), []
    for line in text.splitlines():
        values = shlex.split(line)
        if len(values) != 1:
            raise ValueError('Unexpected Ninja input path: ' + line)
        path = Path(os.path.abspath(output / values[0]))
        if path.is_relative_to(output):
            continue
        if not path.is_relative_to(view):
            raise ValueError('Ninja source input escapes the engine view: ' + str(path))
        relative = path.relative_to(view).as_posix()
        sources.add(relative)
        if not path.exists():
            missing.append(relative)
    report = {'root_targets': targets, 'declared_input_count': len(text.splitlines()),
              'declared_source_input_count': len(sources), 'missing_source_inputs': sorted(set(missing)),
              'source_input_inventory_sha256': digest(('\n'.join(sorted(sources)) + '\n').encode()),
              'all_declared_source_inputs_present': not missing,
              'compiled': False,
              'limitation': 'Ninja-declared source leaves only; implicit compiler includes and generator imports are not established by this check.'}
    write_json(report_path('linux-x64-engine-build-inputs.json', context), report)
    return report


def normalized_target(node, view):
    if isinstance(node, str):
        return node.replace(str(BASE), '$SRC').replace(str(view), '$SRC').replace('out/BlinkGraph', 'out/$PROFILE').replace('out/Engine', 'out/$PROFILE')
    if isinstance(node, list):
        return [normalized_target(item, view) for item in node]
    if isinstance(node, dict):
        return {name: normalized_target(value, view) for name, value in node.items()}
    return node


def graph_additions(graph, parity, view):
    owned = {TARGET, '//standalone/engine:blink_embed', '//standalone/engine:blink_embed_smoke'}
    path = SUPPORT / 'config/runtime-graph-additions.json'
    config = read_json(path) if path.exists() else {'targets': {}}
    if path.exists():
        if config['chromium_revision'] != read_json(SUPPORT / 'config/upstream.lock.json')['revision']:
            raise ValueError('Runtime graph additions use a different upstream revision')
        for relative, expected in config.get('source_inputs', {}).items():
            safe_relative(relative)
            if relative.split('/', 1)[0] in BROWSER_ROOTS or digest((BASE / relative).read_bytes()) != expected:
                raise ValueError('Reviewed upstream runtime build input changed: ' + relative)
    fingerprints = {label: digest(json.dumps(normalized_target(graph[label], view), sort_keys=True).encode())
                    for label in parity['added_targets'] if label not in owned}
    unreviewed = {label: value for label, value in fingerprints.items() if config['targets'].get(label) != value}
    stale = sorted(set(config['targets']) - set(fingerprints))
    project = set(parity['added_targets']) & owned
    return {'project_targets': sorted(project),
            'upstream_target_sha256': fingerprints, 'unreviewed_targets': unreviewed,
            'stale_reviewed_targets': stale, 'missing_project_targets': sorted(owned - project),
            'passed': not unreviewed and not stale and project == owned}


def inspect(view, output, context=None):
    graph_bytes = subprocess.check_output([str(GN), 'desc', str(output), '*', '--all-toolchains', '--format=json', '--root=' + str(view)])
    graph = json.loads(graph_bytes)
    spec = importlib.util.spec_from_file_location('blink_boundary', SUPPORT / 'tools/blink.py')
    blink = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(blink)
    policy = read_json(SUPPORT / 'config/boundary.json')
    boundary = blink.boundary_check(graph, TARGET, policy)
    input_text = (output / 'build.ninja.d').read_text().split(': ', 1)[1].replace('\\\n', '')
    inputs = sorted(set(Path(os.path.abspath(output / path)).relative_to(view).as_posix() for path in shlex.split(input_text)))
    required = {'.gn', 'BUILD.gn', 'standalone/gn/platform_features.gni'}
    if not required.issubset(inputs):
        raise ValueError('Engine-owned entry/features were not loaded by GN')
    # Generic GRIT build helpers may live below ui/webui/resources/tools.
    # Runtime/source/action bans do not prohibit merely importing these GN
    # templates; selected targets and their actual file inputs are checked below.
    forbidden = [name for name in inputs if name.split('/', 1)[0] in BROWSER_ROOTS]
    for name in inputs:
        real = (view / name).resolve()
        if any(real == BASE / part or real.is_relative_to(BASE / part) for part in BROWSER_ROOTS):
            forbidden.append(name)
    browser_files = browser_file_references(graph, view, policy)
    if forbidden or browser_files or not boundary['passed']:
        raise ValueError('Engine graph contains browser inputs or targets: ' + repr(forbidden or browser_files))
    baseline_lock = read_json(SUPPORT / 'config/dependencies.lock.json')
    baseline_bytes = gzip.decompress((ROOT / baseline_lock['graph']['path']).read_bytes())
    if digest(baseline_bytes) != baseline_lock['graph']['sha256']:
        raise ValueError('Recorded baseline graph changed')
    recipe = read_json(SUPPORT / 'config/engine-adaptations.json')
    comparison_graph, comparison_view = graph, view
    profile_parity = None
    if context and context['isolated']:
        # The recorded release graph is itself rechecked against the official
        # baseline and exact integration changes. It is never regenerated here.
        release_view = Path(read_json(WORK / 'current.json')['view'])
        release_manifest = read_json(release_view.parent / 'view.json')
        verify_view(release_view, release_manifest)
        if release_manifest['recipe'] != recipe:
            raise ValueError('Development graph requires a matching, verified release graph')
        comparison_graph = read_json(release_view.parent / 'graph.json')
        comparison_view = release_view
        profile_parity = support_module('build_profiles').compare_dev(
            comparison_graph, graph, release_view, view, base=BASE)
        write_json(report_path('linux-x64-profile-parity.json', context), profile_parity)
    parity = compare_graphs(json.loads(baseline_bytes), comparison_graph, comparison_view,
                            recipe.get('target_rewrites'), recipe.get('target_removals'),
                            recipe.get('inherited_config_effects'), recipe.get('shared_library_tls'))
    write_json(report_path('linux-x64-engine-parity.json', context), parity)
    additions = graph_additions(comparison_graph, parity, comparison_view)
    write_json(report_path('linux-x64-runtime-graph-additions.json', context), additions)
    (view.parent / 'graph.json').write_bytes(graph_bytes)
    if (not parity['matches_reviewed_adaptations'] or not additions['passed']
            or profile_parity is not None and not profile_parity['passed']):
        raise ValueError('Engine migration changed upstream targets; see engine-parity report')
    report = {'root_target': TARGET, 'view': str(view), 'build_directory': str(output),
              'target_count': boundary['target_count'], 'boundary': boundary,
              'browser_build_inputs': forbidden, 'owned_dotfile': True,
              'browser_action_or_source_references': browser_files,
              'adapted_files': sorted(read_json(view.parent / 'view.json')['recipe']['files']),
              'baseline_parity': parity,
              'runtime_graph_additions': additions,
              'build_profile': context['profile'] if context else 'release',
              'profile_parity': profile_parity,
              'compiled': False, 'linked': False, 'runtime_validated': False,
              'limitation': 'Engine-owned GN aggregation using pinned upstream library/toolchain inputs; graph checks do not establish compilation, linking or runtime correctness.'}
    (view.parent / 'graph.json').write_bytes(graph_bytes)
    compressed = gzip.compress(graph_bytes, mtime=0)
    report_path('linux-x64-engine-graph.json.gz', context).parent.mkdir(parents=True, exist_ok=True)
    report_path('linux-x64-engine-graph.json.gz', context).write_bytes(compressed)
    report['graph_sha256'] = digest(graph_bytes)
    report['compressed_graph_sha256'] = digest(compressed)
    write_json(report_path('linux-x64-engine-entry.json', context), report)
    write_json(report_path('linux-x64-engine-inputs.json', context), inputs)
    print(f"Engine graph: {boundary['target_count']} targets, no browser target/input violations", flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'gen', 'verify', 'build', 'version-check', 'runtime-smoke', 'runtime-check'))
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--jobs', type=int, help='Maximum concurrent Ninja jobs for a user-requested build')
    parser.add_argument('--with-smoke', action='store_true', help='Also build the embedding validation harness')
    parser.add_argument('--profile', choices=('release', 'dev'), default='release',
                        help='Development uses an isolated low-optimization view and cache; default release is unchanged')
    cache_options = parser.add_mutually_exclusive_group()
    cache_options.add_argument('--cache', dest='cache', action='store_true', default=None,
                               help='Use the pinned local compiler cache (dev profile)')
    cache_options.add_argument('--no-cache', dest='cache', action='store_false',
                               help='Use an independent dev view without a compiler cache')
    args = parser.parse_args()
    if args.jobs is not None and args.jobs < 1:
        parser.error('--jobs must be positive')
    if args.with_smoke and args.command != 'build':
        parser.error('--with-smoke is only valid with build')
    if args.command == 'runtime-smoke' and args.profile != 'release':
        parser.error('Development profile SDK publication is disabled; use the release build for runtime-smoke')
    context = build_context(args.profile, args.cache,
                            download_cache=args.command not in ('verify', 'runtime-smoke', 'runtime-check'))
    if args.command in ('runtime-smoke', 'runtime-check'):
        if args.dry_run or args.jobs is not None:
            parser.error(args.command + ' only executes already-built artifacts; build options do not apply')
        if args.command == 'runtime-check':
            report = runtime_checks_module().check(
                report_path=report_path('linux-x64-runtime-check.json', context),
                build_report_path=report_path('linux-x64-engine-build.json', context),
                build_profile=context['profile'])
            print('Embedding native runtime check passed; profile:', report['build_profile'], '; SDK not staged')
        else:
            report = runtime_checks_module().smoke()
            print('Embedding runtime smoke passed; SDK:', report['sdk_directory'])
    elif args.command == 'gen':
        view, output = generate(context)
        inspect(view, output, context)
    elif args.command in ('build', 'version-check'):
        view, output = generate(context)
        inspect(view, output, context)
        if not NINJA.is_file():
            raise ValueError('Install the pinned Ninja with standalone/tools/materialize_packages.py first')
        ninja = str(NINJA)
        metadata = args.command == 'version-check'
        if not metadata:
            verify_implicit_sources(context)
            check_build_prerequisites(view, context)
        if metadata and not (view / 'base/check_version_internal.h.in').exists():
            fetch_original_input('base/check_version_internal.h.in')
            prepare(context)
        targets = ['gen/base/check_version_internal.h', 'gen/base/version_info/version_info_values.h'] if metadata else ['blink_engine']
        if args.with_smoke:
            targets.append('blink_embed_smoke')
        input_report = check_declared_inputs(ninja, output, view, targets, context)
        print(f"Ninja source inputs: {input_report['declared_source_input_count']}, missing: {len(input_report['missing_source_inputs'])}", flush=True)
        if input_report['missing_source_inputs'] and not args.dry_run:
            raise ValueError('Restore pinned build inputs with standalone/tools/materialize.py; see reports/linux-x64-engine-build-inputs.json')
        command = [ninja, '-C', str(output), *(['-n'] if args.dry_run else []),
                   *(['-j', str(args.jobs)] if args.jobs is not None else []), *targets]
        checks = runtime_checks_module() if not metadata and not args.dry_run else None
        snapshot = checks.capture_snapshot() if checks else None
        log_path = view.parent / ('ninja-version.log' if metadata else 'ninja-build.log')
        tail = deque(maxlen=20)
        failures, failure_remaining = [], 0
        with log_path.open('w') as log:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                       env=build_environment(context))
            for line in process.stdout:
                log.write(line)
                log.flush()
                tail.append(line.rstrip('\n'))
                if line.startswith('FAILED:') and len(failures) < 5:
                    failures.append([])
                    failure_remaining = 32
                if failure_remaining:
                    failures[-1].append(line.rstrip('\n'))
                    failure_remaining -= 1
                if not args.dry_run:
                    print(line, end='', flush=True)
            process.stdout.close()
            returncode = process.wait()
        build_report = {
            'command': command, 'ninja_version': subprocess.check_output([ninja, '--version'], text=True).strip(),
            'ninja_sha256': digest(NINJA.read_bytes()),
            'dry_run': args.dry_run, 'exit_code': returncode,
            'build_profile': context['profile'],
            'compiler_cache': context.get('compiler_cache'),
            'compiled': False, 'runtime_smoke_passed': False,
            'failure_excerpts': failures,
            'log': str(log_path), 'output_tail': list(tail)}
        receipt_error = None
        if checks and not returncode:
            try:
                verify_view(view, read_json(view.parent / 'view.json'))
                build_report.update(checks.build_receipt(view, output, args.with_smoke, snapshot,
                                                        build_profile=context['profile']))
            except (OSError, ValueError) as error:
                receipt_error = error
                build_report['validation_error'] = str(error)
        if metadata and not args.dry_run and not returncode:
            values = dict(line.split('=', 1) for line in (SUPPORT / 'gn/VERSION').read_text().splitlines())
            expected = '.'.join(values[key] for key in ('MAJOR', 'MINOR', 'BUILD', 'PATCH'))
            header = (output / 'gen/base/version_info/version_info_values.h').read_text()
            if '#define PRODUCT_NAME "Blink"' not in header or f'#define PRODUCT_VERSION "{expected}"' not in header:
                raise ValueError('Engine version header does not match the pinned metadata')
            build_report.update(engine_product='Blink', upstream_compatibility_version=expected, generated_headers_verified=True)
        write_json(report_path('linux-x64-engine-version.json' if metadata else 'linux-x64-engine-build.json', context), build_report)
        if receipt_error:
            raise ValueError('Native build finished but its output receipt could not be verified: ' + str(receipt_error))
        if args.dry_run:
            print('\n'.join(tail))
        if returncode:
            for failure in failures:
                print('\nFailed command and diagnostic:\n' + '\n'.join(failure), flush=True)
            print('Full build log: ' + str(log_path), flush=True)
            raise subprocess.CalledProcessError(returncode, command)
    elif args.command == 'verify':
        verify_blink()
        view = Path(read_json(current_path(context))['view'])
        manifest = read_json(view.parent / 'view.json')
        verify_view(view, manifest)
        if context['isolated'] and manifest.get('build_profile') != profile_identity(context):
            raise ValueError('Build profile or cache settings changed; generate a fresh development view')
        if not context['isolated'] and manifest.get('build_profile'):
            raise ValueError('The release pointer must not identify an isolated development build')
        if 'generator_layout_copies' not in manifest:
            raise ValueError('Generator layout upgrade required; run engine.py prepare')
        if read_json(SUPPORT / 'config/engine-adaptations.json') != manifest['recipe']:
            raise ValueError('Build view uses an older adaptation recipe')
        for relative, expected in manifest['owned_files'].items():
            if relative.startswith('standalone/gn/') and digest((ROOT / relative).read_bytes()) != expected:
                raise ValueError('Project GN configuration changed; regenerate the engine view')
        print('Verified engine-owned GN files and recorded adaptations')
        verify_implicit_sources(context)
    else:
        view = prepare(context)
        print('Engine build view:', view)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print('error:', error, file=sys.stderr)
        sys.exit(2)
