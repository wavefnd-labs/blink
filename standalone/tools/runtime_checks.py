#!/usr/bin/env python3
"""Bind completed native engine builds and executed smoke tests to their inputs.

This module never invokes GN, Ninja or a compiler. A smoke check only executes
the already-built harness identified by the latest successful build receipt.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess

_AUTOMATION_SPEC = importlib.util.spec_from_file_location('runtime_automation', Path(__file__).with_name('automation.py'))
automation = importlib.util.module_from_spec(_AUTOMATION_SPEC)
_AUTOMATION_SPEC.loader.exec_module(automation)

ROOT = automation.ROOT
BUILD_REPORT = Path('standalone/reports/linux-x64-engine-build.json')
SMOKE_REPORT = Path('standalone/reports/linux-x64-runtime-smoke.json')
CHECK_REPORT = Path('standalone/reports/linux-x64-runtime-check.json')
BUILDFLAGS = Path('gen/tools/v8_context_snapshot/buildflags.h')
FLAG = re.compile(r'^\s*#define\s+BUILDFLAG_INTERNAL_USE_V8_CONTEXT_SNAPSHOT\(\)\s+\(([01])\)\s*$', re.MULTILINE)
FILENAME = re.compile(r'^\s*#define\s+BUILDFLAG_INTERNAL_V8_CONTEXT_SNAPSHOT_FILENAME\(\)\s+\(("[^"\n]+")\)\s*$', re.MULTILINE)
RESOURCE_FLAGS = {'icu_data': '--icu-data', 'v8_snapshot': '--v8-snapshot',
                  'blink_resources': '--blink-resources', 'blink_scaled_resources': '--blink-scaled-resources'}


def capture_snapshot(root=ROOT):
    return automation.snapshot(root)


def real_file(path, output=None, elf=False, executable=False):
    path = Path(path)
    if not path.is_file() or path.is_symlink() or not path.stat().st_size:
        raise ValueError('Native build output is missing or invalid: ' + str(path))
    if output is not None and not path.resolve().is_relative_to(Path(output).resolve()):
        raise ValueError('Native output escapes its build directory: ' + str(path))
    if elf:
        with path.open('rb') as stream:
            if stream.read(4) != b'\x7fELF':
                raise ValueError('Native Linux output is not ELF: ' + str(path))
    if executable and not os.access(path, os.X_OK):
        raise ValueError('Native smoke harness is not executable: ' + str(path))
    return path


def relative_name(value):
    if not isinstance(value, str):
        raise ValueError('Native output paths must be strings')
    path = PurePosixPath(value)
    if not value or path.is_absolute() or '..' in path.parts or '\\' in value or str(path) != value:
        raise ValueError('Invalid relative native output path: ' + value)
    return path


def resource_paths(output):
    """Read the generated flag and filename; never guess from stale snapshots."""
    output = Path(output).resolve()
    text = real_file(output / BUILDFLAGS, output).read_text()
    flags = FLAG.findall(text)
    if len(flags) != 1:
        raise ValueError('Generated V8 context snapshot flag is missing or ambiguous')
    if flags[0] == '1':
        names = FILENAME.findall(text)
        if len(names) != 1:
            raise ValueError('Generated V8 context snapshot filename is missing or ambiguous')
        snapshot_name = json.loads(names[0])
        if len(relative_name(snapshot_name).parts) != 1:
            raise ValueError('V8 context snapshot must be a root build output')
    else:
        snapshot_name = 'snapshot_blob.bin'
    resources = {'icu_data': 'icudtl.dat', 'v8_snapshot': snapshot_name,
                 'blink_resources': 'gen/third_party/blink/public/resources/blink_resources.pak'}
    scaled = 'gen/third_party/blink/public/resources/blink_scaled_resources_100_percent.pak'
    if os.path.lexists(output / scaled):
        resources['blink_scaled_resources'] = scaled
    for value in resources.values():
        real_file(output / value, output)
    return resources


def artifact(path, root=ROOT):
    path = real_file(path)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Recorded build artifact must be inside the repository')
    return {'path': path.relative_to(root).as_posix(), 'sha256': automation.sha256(path)}


def configuration_fingerprint(view, output):
    """Bind successful binaries to their actual GN arguments and source view."""
    result = {}
    for key, path in (('args_gn_sha256', Path(output) / 'args.gn'),
                      ('view_manifest_sha256', Path(view).parent / 'view.json')):
        if os.path.lexists(path):
            result[key] = automation.sha256(real_file(path))
    return result


def checked_profile(name):
    if name not in ('release', 'dev'):
        raise ValueError('Unknown native build profile: ' + str(name))
    return name


def build_receipt(view, output, with_smoke, snapshot, root=ROOT, build_profile='release'):
    """Call only after a successful full Ninja build; dry-runs cannot use this."""
    view, output = Path(view).resolve(), Path(output).resolve()
    checked_profile(build_profile)
    if not view.is_relative_to(root.resolve()) or not output.is_relative_to(view):
        raise ValueError('Build receipt has an invalid source view or output path')
    if capture_snapshot(root) != snapshot:
        raise ValueError('Engine source or locks changed during the build')
    configuration = configuration_fingerprint(view, output)
    if build_profile == 'dev' and set(configuration) != {'args_gn_sha256', 'view_manifest_sha256'}:
        raise ValueError('Development build receipt requires its GN arguments and view manifest')
    files = [real_file(output / 'libblink_embed.so', output, elf=True)]
    if with_smoke:
        files.append(real_file(output / 'blink_embed_smoke', output, elf=True, executable=True))
    resources = resource_paths(output)
    files.extend(output / name for name in resources.values())
    files.append(output / BUILDFLAGS)
    records = [artifact(path, root) for path in sorted(set(files))]
    if capture_snapshot(root) != snapshot:
        raise ValueError('Engine source or locks changed while recording build outputs')
    if configuration_fingerprint(view, output) != configuration:
        raise ValueError('Build configuration changed while recording build outputs')
    return {'schema': 1, 'snapshot': snapshot, 'compiled': True,
            'build_profile': build_profile, 'build_configuration': configuration,
            'view': str(view), 'build_directory': str(output),
            'smoke_built': bool(with_smoke), 'built_artifacts': records,
            'runtime_resources': resources, 'runtime_smoke_passed': False}


def verify_build(build, root=ROOT, build_profile='release'):
    if not isinstance(build, dict):
        raise ValueError('Native build receipt must be a JSON object')
    checked_profile(build_profile)
    if build.get('build_profile', 'release') != build_profile:
        raise ValueError('Native build receipt belongs to another build profile')
    if build.get('compiled') is not True or build.get('dry_run') is True or build.get('exit_code', 0) != 0:
        raise ValueError('Runtime smoke requires a successful complete native build, not a dry-run')
    if build.get('smoke_built') is not True:
        raise ValueError('Build the embedding harness explicitly with --with-smoke first')
    if build.get('snapshot') != capture_snapshot(root):
        raise ValueError('Successful build receipt is stale for the current engine source and locks')
    if not isinstance(build.get('view'), str) or not isinstance(build.get('build_directory'), str):
        raise ValueError('Native build receipt paths must be strings')
    view, output = Path(build['view']), Path(build['build_directory'])
    if not view.is_absolute() or not output.is_absolute() or not output.resolve().is_relative_to(view.resolve()) or not view.resolve().is_relative_to(root.resolve()):
        raise ValueError('Successful build receipt has an invalid source view or output path')
    recorded_configuration = build.get('build_configuration')
    if (recorded_configuration is not None and
            recorded_configuration != configuration_fingerprint(view, output)):
        raise ValueError('Native build GN arguments or view manifest changed after compilation')
    if build_profile == 'dev' and (not isinstance(recorded_configuration, dict)
            or set(recorded_configuration) != {'args_gn_sha256', 'view_manifest_sha256'}):
        raise ValueError('Development build receipt is missing its configuration fingerprints')
    real_file(output / 'libblink_embed.so', output, elf=True)
    real_file(output / 'blink_embed_smoke', output, elf=True, executable=True)
    resources = resource_paths(output)
    if build.get('runtime_resources') != resources:
        raise ValueError('Recorded resources no longer match generated V8 snapshot flags or build outputs')
    expected = {(output / name).relative_to(root).as_posix() for name in resources.values()}
    expected.update((output / name).relative_to(root).as_posix() for name in ('libblink_embed.so', 'blink_embed_smoke', BUILDFLAGS))
    records = build.get('built_artifacts', [])
    if (not isinstance(records, list) or any(not isinstance(row, dict) or not isinstance(row.get('path'), str) or
                                            not isinstance(row.get('sha256'), str) for row in records) or
            {row.get('path') for row in records} != expected or len(records) != len(expected)):
        raise ValueError('Build receipt does not cover exactly the native binaries and runtime inputs')
    for row in records:
        path = root / relative_name(row['path'])
        real_file(path, output)
        if automation.sha256(path) != row.get('sha256'):
            raise ValueError('Built artifact changed after compilation: ' + row['path'])
    return output, resources


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate native smoke receipt field: ' + key)
        result[key] = value
    return result


def validate_smoke_json(text):
    try:
        value = json.loads(text, object_pairs_hook=unique_keys)
    except (json.JSONDecodeError, TypeError) as error:
        raise ValueError('Native smoke did not emit its JSON receipt') from error
    if not isinstance(value, dict):
        raise ValueError('Native smoke receipt must be a JSON object')
    for field in ('initialized', 'html_committed', 'pixel_assertions', 'resized', 'wrong_thread_rejected', 'shutdown',
                  'javascript', 'input', 'navigation', 'ime', 'child_frames', 'storage', 'workers',
                  'images', 'fonts'):
        if value.get(field) is not True:
            raise ValueError('Native smoke assertion is missing or failed: ' + field)
    for field, expected in (('schema_version', 4), ('abi_version', 1), ('width', 80), ('height', 80)):
        if type(value.get(field)) is not int or value[field] != expected:
            raise ValueError('Native smoke receipt has an invalid ' + field)
    if type(value.get('host_resource_fetches')) is not int or value['host_resource_fetches'] < 1:
        raise ValueError('Native smoke receipt has an invalid host_resource_fetches')
    if not isinstance(value.get('rgba_fnv1a64'), str) or not re.fullmatch('[0-9a-fA-F]{16}', value['rgba_fnv1a64']):
        raise ValueError('Native smoke receipt has an invalid pixel checksum')
    return value


def _run(root=ROOT, report_path=None, timeout=180, build_report_path=None,
         build_profile='release', stage_sdk=True):
    """Run one verified native harness, optionally staging a release SDK."""
    checked_profile(build_profile)
    if stage_sdk and build_profile != 'release':
        # Reject before opening a report, so a mistaken dev invocation cannot
        # invalidate release runtime evidence or stage a dev SDK as a release.
        raise ValueError('Runtime smoke and SDK staging currently support only the release profile')
    directory = root / 'standalone/reports'
    if build_profile == 'dev':
        directory /= 'profiles/dev-cache'
    report_path = Path(report_path or (root / SMOKE_REPORT if stage_sdk else directory / CHECK_REPORT.name))
    build_report_path = Path(build_report_path or directory / BUILD_REPORT.name)
    if not stage_sdk and report_path.resolve() in {
            (root / SMOKE_REPORT).resolve(), (root / BUILD_REPORT).resolve(),
            build_report_path.resolve()}:
        raise ValueError('Runtime check cannot overwrite build or release smoke receipts')
    report = {'schema': 1, 'checked_at': datetime.now(timezone.utc).isoformat(),
              'compiled': False, 'runtime_smoke_passed': False, 'sdk_staged': False,
              'build_profile': build_profile,
              'check_only': not stage_sdk, 'runtime_check_passed': False,
              'artifacts': [], 'limitation': 'ABI 1 pixels, JavaScript, input/IME, child frames, DOM storage and dedicated classic worker assertions; see standalone/runtime.md for the supported scope and remaining host integrations.'}
    # Invalidate an earlier success immediately; later failures must not leave
    # that old receipt looking like the result of the current invocation.
    automation.write_json(report_path, report)
    try:
        build = automation.read_json(build_report_path)
        output, resources = verify_build(build, root, build_profile=build_profile)
        report.update(compiled=True, snapshot=build['snapshot'], build_directory=str(output),
                      build_configuration=build.get('build_configuration', {}),
                      built_artifacts=build['built_artifacts'])
        command = [str(output / 'blink_embed_smoke')]
        command.extend(RESOURCE_FLAGS[key] + '=' + str(output / value) for key, value in sorted(resources.items()))
        environment = dict(os.environ)
        environment['LD_LIBRARY_PATH'] = str(output) + (os.pathsep + environment['LD_LIBRARY_PATH'] if environment.get('LD_LIBRARY_PATH') else '')
        report['command'] = command
        result = subprocess.run(command, cwd=output, env=environment, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=timeout, check=False)
        report.update(exit_code=result.returncode, stdout_tail=result.stdout[-16384:], stderr_tail=result.stderr[-16384:])
        if result.returncode:
            raise ValueError('Native embedding smoke exited with status ' + str(result.returncode))
        pixels = validate_smoke_json(result.stdout)
        report['native_receipt'] = pixels
        report['native_smoke_passed'] = True
        verify_build(build, root, build_profile=build_profile)  # Reject files or source mutated during execution.
        if not stage_sdk:
            report['runtime_check_passed'] = True
            automation.write_json(report_path, report)
            return report
        fingerprint = hashlib.sha256(json.dumps({'snapshot': build['snapshot'], 'artifacts': build['built_artifacts'],
            'build_profile': build_profile, 'build_configuration': build.get('build_configuration', {})}, sort_keys=True).encode()).hexdigest()[:20]
        version = automation.identity(root)
        destination = root / 'standalone/out/sdk' / (version['version'] + '-' + version['profile'] + '-' + fingerprint)
        records = automation.stage_sdk(output, destination=destination,
                                       resources={key: output / value for key, value in resources.items()}, root=root)
        verify_build(build, root, build_profile=build_profile)
        report.update(runtime_smoke_passed=True, sdk_staged=True,
                      sdk_directory=destination.relative_to(root).as_posix(), artifacts=records)
        automation.write_json(report_path, report)
        return report
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        report.update(runtime_smoke_passed=False, runtime_check_passed=False,
                      validation_error=str(error))
        automation.write_json(report_path, report)
        raise ValueError('Runtime validation failed; see ' + str(report_path) + ': ' + str(error)) from error


def smoke(root=ROOT, report_path=None, timeout=180, build_report_path=None, build_profile='release'):
    """Run the existing, hash-verified release harness and stage its SDK."""
    return _run(root, report_path, timeout, build_report_path, build_profile, stage_sdk=True)


def check(root=ROOT, report_path=None, timeout=180, build_report_path=None, build_profile='release'):
    """Validate an already-built release/dev engine without building or staging."""
    return _run(root, report_path, timeout, build_report_path, build_profile, stage_sdk=False)
