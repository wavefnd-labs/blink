#!/usr/bin/env python3
"""Project-local pinned ccache, isolated from user/global cache configuration.

Only install downloads an official prebuilt tool. Probe compiles tiny synthetic
fixtures with the pinned Clang; it never invokes GN/Ninja or engine compilation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / 'standalone/config/compiler-cache.json'
WORK = ROOT / 'standalone/work/compiler-cache'


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def load():
    config = json.loads(CONFIG.read_text())
    if (config['schema'] != 1 or config['backend'] != 'ccache'
            or config['version'] != '4.14.2'
            or config['platform'] != 'linux-x86_64-glibc'
            or config['settings']['sloppiness'] != 'modules'
            or config['settings']['depend_mode'] != 'true'
            or config['settings']['direct_mode'] != 'true'
            or config['settings']['compiler_check'] != 'content'
            or config['settings']['hard_link'] != 'false'):
        raise ValueError('Unreviewed compiler-cache policy')
    return config


def ordinary(path):
    """Reject aliases before reading or replacing managed configuration/tool files."""
    path = Path(path)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError('Expected private regular cache file: ' + str(path))
    if path.exists() and path.stat().st_nlink != 1:
        raise ValueError('Shared cache tool/config file: ' + str(path))
    return path


def managed_directory(path):
    path = Path(path)
    # All project-controlled cache/tool paths stay under WORK. No source aliases.
    if '..' in path.parts or not path.absolute().is_relative_to(WORK.absolute()):
        raise ValueError('Cache path is outside project cache workspace')
    cursor = WORK
    for part in path.absolute().relative_to(WORK.absolute()).parts:
        if cursor.is_symlink():
            raise ValueError('Cache directory is a symlink: ' + str(cursor))
        cursor /= part
    if cursor.is_symlink():
        raise ValueError('Cache directory is a symlink: ' + str(cursor))
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_bytes(path, data, mode=0o644):
    path = ordinary(path)
    managed_directory(path.parent)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.cache-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            temporary.chmod(mode)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def install(config, download=False):
    tool = WORK / 'tools' / ('ccache-' + config['version'])
    managed_directory(tool.parent)
    if tool.is_symlink():
        raise ValueError('Cache directory is a symlink: ' + str(tool))
    if tool.exists():
        managed_directory(tool)
        for name, expected in config['files'].items():
            path = ordinary(tool / name)
            if not path.is_file() or digest(path) != expected:
                raise ValueError('Pinned ccache installation changed: ' + str(path))
        if not os.access(tool / 'ccache', os.X_OK):
            raise ValueError('Pinned ccache is not executable')
        return tool
    managed_directory(WORK / 'downloads')
    archive = ordinary(WORK / 'downloads' / Path(config['archive_url']).name)
    if not archive.exists():
        if not download:
            raise ValueError('Pinned ccache is not installed; run standalone/tools/compiler_cache.py install')
        managed_directory(archive.parent)
        with urllib.request.urlopen(config['archive_url'], timeout=60) as response:
            # Bound downloads to the pinned size, including an overrun byte.
            data = response.read(config['archive_size'] + 1)
        if len(data) != config['archive_size'] or hashlib.sha256(data).hexdigest() != config['archive_sha256']:
            raise ValueError('Downloaded ccache archive does not match official pin')
        atomic_bytes(archive, data)
    if archive.stat().st_size != config['archive_size'] or digest(archive) != config['archive_sha256']:
        raise ValueError('Cached ccache archive differs from official pin')
    managed_directory(tool.parent)
    with tempfile.TemporaryDirectory(prefix='.ccache-install-', dir=tool.parent) as directory:
        stage = Path(directory) / 'tool'
        stage.mkdir()
        with tarfile.open(archive) as tar:
            for name, expected in config['files'].items():
                member = tar.getmember(config['archive_root'] + '/' + name)
                if not member.isfile():
                    raise ValueError('Pinned ccache member must be a regular file')
                data = tar.extractfile(member).read()
                if hashlib.sha256(data).hexdigest() != expected:
                    raise ValueError('Pinned ccache member checksum mismatch: ' + name)
                path = stage / name
                path.write_bytes(data)
                path.chmod(0o755 if name == 'ccache' else 0o644)
        stage.replace(tool)
    return tool


def configuration(config, cache_directory):
    if any('\n' in str(value) or '\r' in str(value) for value in [cache_directory, *config['settings'].values()]):
        raise ValueError('Invalid compiler-cache configuration value')
    settings = {**config['settings'], 'cache_dir': str(cache_directory)}
    return ''.join(f'{key} = {value}\n' for key, value in sorted(settings.items())).encode()


def prepare(download=False):
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'amd64'):
        raise ValueError('Pinned compiler cache currently supports Linux x86_64 only')
    config = load()
    tool = install(config, download=download)
    cache_directory = managed_directory(WORK / 'data')
    path = ordinary(WORK / 'ccache.conf')
    data = configuration(config, cache_directory)
    if not path.exists() or path.read_bytes() != data:
        atomic_bytes(path, data)
    return {'backend': 'ccache', 'binary': str(tool / 'ccache'), 'config': str(path),
            'wrapper': shlex.join([sys.executable, '-B', str(Path(__file__).resolve()), 'wrap']),
            'wrapper_sha256': digest(Path(__file__)),
            'cache_directory': str(cache_directory), 'version': config['version'],
            'binary_sha256': config['files']['ccache'], 'archive_sha256': config['archive_sha256'],
            'settings_sha256': hashlib.sha256(data).hexdigest(),
            'license': str(tool / 'LICENSE.md'), 'gpl_license': str(tool / 'GPL-3.0.txt')}


def environment(base=None, prepared=None):
    prepared = prepared or prepare()
    result = {key: value for key, value in (os.environ if base is None else base).items()
              if not key.startswith('CCACHE_')}
    result['CCACHE_CONFIGPATH'] = prepared['config']
    return result


def invoke(command, env, cwd=None):
    result = subprocess.run(command, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=60)
    if result.returncode:
        raise ValueError('Compiler-cache command failed: ' + repr(command) + '\n' + result.stdout)
    return result.stdout


def statistics(prepared, env):
    raw = invoke([prepared['binary'], '--print-stats'], env)
    return {key: int(value) for line in raw.splitlines() for key, value in [line.split()]}


def wrapper_command(arguments, prepared):
    """Track system headers too; never silently cache implicit module state."""
    if not arguments or arguments[0].startswith('-'):
        raise ValueError('Cache wrapper requires a compiler followed by its exact arguments')
    flags = arguments[1:]
    implicit = ('-fprebuilt-implicit-modules' in flags
                or ('-fmodules' in flags and not all(flag in flags for flag in
                    ['-fno-implicit-modules', '-fno-implicit-module-maps'])))
    # Response-file dependency options need separate review before rewriting.
    if implicit or any(flag.startswith('@') for flag in flags):
        return list(arguments)
    # -MMD omits -isystem/sysroot files. Depend-mode ccache therefore requires
    # -MD, without ignoring system headers or weakening header/time checks.
    return [prepared['binary'], arguments[0], *['-MD' if flag == '-MMD' else flag for flag in flags]]


def wrap(arguments):
    prepared = prepare()
    requested = Path(os.environ.get('CCACHE_CONFIGPATH', prepared['config']))
    if requested != Path(prepared['config']):
        # Permit only our isolated synthetic probe configuration, byte-equal to
        # the same reviewed policy. Other user/global CCACHE settings are ignored.
        if requested.name != 'ccache.conf' or requested.parent.parent != WORK / 'probes':
            raise ValueError('Cache wrapper requires project-managed configuration')
        managed_directory(requested.parent)
        if ordinary(requested).read_bytes() != configuration(load(), requested.parent / 'data'):
            raise ValueError('Compiler-cache probe configuration changed')
        prepared = {**prepared, 'config': str(requested)}
    command = wrapper_command(arguments, prepared)
    os.execvpe(command[0], command, environment(prepared=prepared))


def probe():
    """Prove miss/hit, explicit PCM change invalidation, then another hit.

    Two valid PCMs differ only in an unused module-build macro. Consumer source,
    header, module map and command stay identical, so a cached object cannot be
    invalidated merely by an ordinary source/header change. The probe uses the
    same quoted GN module naming and explicit-module options as this engine.
    """
    prepared = prepare()
    clang = ROOT / 'standalone/work/graph/src/third_party/llvm-build/Release+Asserts/bin/clang++'
    if not clang.is_file():
        raise ValueError('Materialize the pinned Chromium Clang before cache probe')
    parent = managed_directory(WORK / 'probes')
    directory = Path(tempfile.mkdtemp(prefix='explicit-modules-', dir=parent))
    header = directory / 'value.h'
    modulemap = directory / 'module.modulemap'
    source = directory / 'consumer.cc'
    system = directory / 'system'
    system.mkdir()
    system_header = system / 'system_value.h'
    system_header.write_text('#define SYSTEM_PROBE_VALUE 4\n')
    header.write_text('inline int cached_value() { return 7; }\n')
    modulemap.write_text('module "//probe:explicit" { header "value.h" export * }\n')
    source.write_text('#include "value.h"\n#include <system_value.h>\n'
                      'int probe() { return cached_value() + SYSTEM_PROBE_VALUE; }\n')
    # The probe has its own empty cache, so it never resets or evicts user entries.
    local_config = directory / 'ccache.conf'
    local_config.write_bytes(configuration(load(), directory / 'data'))
    env = environment(prepared={**prepared, 'config': str(local_config)})
    flags = ['-std=c++23', '-O1', '-g0', '-fmodules', '-fno-implicit-module-maps',
             '-fno-implicit-modules', '-fmodule-map-file=module.modulemap',
             '-Xclang', '-fmodule-file-home-is-cwd',
             '-Xclang', '-fmodules-disable-diagnostic-validation']
    compiler_arguments = [str(clang), *flags, '-isystem', 'system',
                '-fmodule-file=//probe:explicit=module.pcm', '-MMD', '-MF', 'consumer.d',
                '-c', 'consumer.cc', '-o', 'consumer.o']
    consumer = [sys.executable, '-B', str(Path(__file__).resolve()), 'wrap', *compiler_arguments]
    inputs = {path.name: digest(path) for path in [header, modulemap, source]}
    report = {'schema': 1, 'scope': 'Tiny synthetic Clang explicit-module consumer only; no engine compilation',
              'directory': str(directory), 'backend': prepared, 'clang': str(clang),
              'clang_sha256': digest(clang), 'clang_version': invoke([str(clang), '--version'], env),
              'ccache_version': invoke([prepared['binary'], '--version'], env),
              'input_sha256': inputs, 'consumer_command': consumer, 'steps': []}
    report['effective_compiler_arguments'] = wrapper_command(compiler_arguments, prepared)
    report['system_header_before_sha256'] = digest(system_header)
    previous = statistics(prepared, env)
    pcm_hashes = []
    for definition in ['1', '2']:
        command = [str(clang), *flags, '-DUNUSED_CACHE_PROBE=' + definition,
                   '-fmodule-name=//probe:explicit', '-x', 'c++', '-Xclang', '-emit-module',
                   '-c', 'module.modulemap', '-o', 'module.pcm']
        invoke(command, env, directory)
        pcm_hashes.append(digest(directory / 'module.pcm'))
        for expected in ['miss', 'hit']:
            output = invoke(consumer, env, directory)
            current = statistics(prepared, env)
            delta = {key: current.get(key, 0) - previous.get(key, 0)
                     for key in ['direct_cache_hit', 'preprocessed_cache_hit', 'cache_miss']}
            actual = 'hit' if delta['direct_cache_hit'] == 1 else 'miss' if delta['cache_miss'] == 1 else 'unsupported'
            if actual != expected or sum(delta.values()) != 1:
                raise ValueError('Cache probe did not prove expected ' + expected + ': ' + repr(delta))
            report['steps'].append({'module_variant': definition, 'module_command': command,
                                    'pcm_sha256': pcm_hashes[-1], 'expected': expected,
                                    'statistics_delta': delta, 'compiler_output': output,
                                    'object_sha256': digest(directory / 'consumer.o')})
            previous = current
    if pcm_hashes[0] == pcm_hashes[1]:
        raise ValueError('Cache probe did not change explicit module bytes')
    if inputs != {path.name: digest(path) for path in [header, modulemap, source]}:
        raise ValueError('Cache probe changed non-module inputs')
    if len({step['object_sha256'] for step in report['steps']}) != 1:
        raise ValueError('Equivalent synthetic modules produced inconsistent cached objects')
    if 'system/system_value.h' not in (directory / 'consumer.d').read_text():
        raise ValueError('Cache wrapper did not add system headers to compiler dependencies')
    system_header.write_text('#define SYSTEM_PROBE_VALUE 9\n')
    system_steps = []
    for expected in ['miss', 'hit']:
        invoke(consumer, env, directory)
        current = statistics(prepared, env)
        delta = {key: current.get(key, 0) - previous.get(key, 0)
                 for key in ['direct_cache_hit', 'preprocessed_cache_hit', 'cache_miss']}
        if (delta['cache_miss'] != (expected == 'miss')
                or delta['direct_cache_hit'] != (expected == 'hit') or sum(delta.values()) != 1):
            raise ValueError('System-header change did not produce expected cache ' + expected)
        system_steps.append({'expected': expected, 'statistics_delta': delta,
                             'object_sha256': digest(directory / 'consumer.o')})
        previous = current
    if (system_steps[0]['object_sha256'] == report['steps'][0]['object_sha256']
            or system_steps[0]['object_sha256'] != system_steps[1]['object_sha256']):
        raise ValueError('System-header cache invalidation did not preserve real compiler output')
    report['system_header_after_sha256'] = digest(system_header)
    report['system_header_steps'] = system_steps
    report['passed'] = True
    report['compiler_cache_tool_sha256'] = digest(Path(__file__))
    receipt = directory / 'receipt.json'
    receipt.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['install', 'status', 'probe', 'wrap'])
    parser.add_argument('compiler_arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command == 'wrap':
        wrap(args.compiler_arguments)
        return
    if args.compiler_arguments:
        parser.error('Compiler arguments are accepted only by wrap')
    if args.command == 'probe':
        print(json.dumps(probe(), indent=2, sort_keys=True))
        return
    prepared = prepare(download=args.command == 'install')
    env = environment(prepared=prepared)
    if args.command == 'status':
        prepared['statistics'] = statistics(prepared, env)
    print(json.dumps(prepared, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
