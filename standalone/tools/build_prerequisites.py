#!/usr/bin/env python3
"""Check reviewed dependencies GN cannot infer, without compiling the engine."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
SUPPORT = ROOT / 'standalone'
CONFIG = SUPPORT / 'config/build-prerequisites.json'


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def relative_path(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or not path.parts or str(path) != name:
        raise ValueError('Invalid prerequisite path: ' + name)
    return path


def verify_inventory(view, graph, config):
    selected = {node['script'].removeprefix('//') for node in graph.values()
                if node.get('type') in ('action', 'action_foreach') and node.get('script')}
    if selected != set(config['audited_action_scripts']):
        raise ValueError('Selected action scripts changed; review indirect prerequisites')
    for name, expected in config['audited_action_scripts'].items():
        path = Path(view) / relative_path(name)
        if not path.is_file() or sha256(path) != expected:
            raise ValueError('Audited action script missing or changed: ' + name)
    if not set(config['python_help_entries']).issubset(selected):
        raise ValueError('Import probe is not a selected action script')
    return len(selected)


def safe_arguments(arguments):
    if arguments not in (['--version'], ['--help'], ['version']):
        raise ValueError('Prerequisite probes may only request version/help')
    return arguments


def check(view, graph=None, report_path=None, diagnostics_dir=None):
    view = Path(view)
    # Engine callers pass their profile context explicitly. Also isolate a
    # manually supplied profile view so --view cannot overwrite release logs.
    isolated = view.absolute().is_relative_to((SUPPORT / 'work/engine/profiles').absolute())
    default_report = (view.parent / 'reports/linux-x64-build-prerequisites.json' if isolated
                      else SUPPORT / 'reports/linux-x64-build-prerequisites.json')
    default_diagnostics = (view.parent / 'diagnostics/prerequisites' if isolated
                           else SUPPORT / 'work/diagnostics/prerequisites')
    report_path = Path(report_path or default_report)
    isolate_nested_reports = (isolated or report_path.absolute() !=
        (SUPPORT / 'reports/linux-x64-build-prerequisites.json').absolute() or
        diagnostics_dir is not None and Path(diagnostics_dir).absolute() !=
        (SUPPORT / 'work/diagnostics/prerequisites').absolute())
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps({'schema': 1, 'view': str(view), 'passed': False,
                                       'status': 'checking', 'engine_compiled': False}) + '\n')
    config = json.loads(CONFIG.read_text())
    upstream = json.loads((SUPPORT / 'config/upstream.lock.json').read_text())
    if config.get('schema') != 1 or config['chromium_revision'] != upstream['revision']:
        raise ValueError('Prerequisite review uses another upstream revision')
    for name, expected in config['lock_sha256'].items():
        if sha256(SUPPORT / 'config' / relative_path(name)) != expected:
            raise ValueError('Prerequisite dependency pins changed; review: ' + name)
    python = json.loads((SUPPORT / 'config/dependencies.lock.json').read_text())['profile']['script_executable']
    graph = json.loads((view.parent / 'graph.json').read_text()) if graph is None else graph
    count = verify_inventory(view, graph, config)
    output = Path(diagnostics_dir or default_diagnostics)
    output.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.pop('PYTHONPATH', None)
    environment.update(PYTHONDONTWRITEBYTECODE='1',
                       PYTHONPYCACHEPREFIX=str(output / 'python-cache' if isolate_nested_reports
                                              else SUPPORT / 'work/engine/python-cache'),
                       LD_LIBRARY_PATH=str(view / relative_path(config['library_search_directory'])))
    probes = []

    def run(name, command):
        result = subprocess.run(command, cwd=view / 'out/Engine', env=environment,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=120)
        log = output / (name + '.log')
        log.write_text(result.stdout)
        probes.append({'name': name, 'command': command, 'exit_code': result.returncode,
                       'log': str(log.relative_to(ROOT)), 'log_sha256': sha256(log)})
        if result.returncode:
            raise ValueError('Build prerequisite failed: ' + name + '; see ' + str(log))

    for index, name in enumerate(config['python_help_entries']):
        run('python-' + str(index), [python, '-B', str(view / relative_path(name)), '--help'])
    for name, probe in config['executable_probes'].items():
        path = view / relative_path(probe['path'])
        if not path.is_file() or sha256(path) != probe['sha256']:
            raise ValueError('Pinned indirect executable missing or changed: ' + name)
        run(name, [str(path), *safe_arguments(probe['arguments'])])
    for index, (name, expected) in enumerate(config['dynamic_libraries'].items()):
        path = view / relative_path(name)
        if not path.is_file() or sha256(path) != expected:
            raise ValueError('Pinned indirect library missing or changed: ' + name)
        run('dynamic-library-' + str(index), [python, '-B', '-c',
            'import ctypes,sys; ctypes.CDLL(sys.argv[1]); print("library loaded")', str(path)])
    for tool, argument in (('materialize_packages.py', '--check-tools'),
                           ('materialize_implicit_sources.py', '--verify'),
                           ('materialize_nested_sources.py', '--verify')):
        command = [python, '-B', str(SUPPORT / 'tools' / tool), argument]
        if isolate_nested_reports:
            if tool == 'materialize_packages.py':
                command.extend(['--view', str(view), '--tools-report',
                                str(output / 'materialize-build-tools.json')])
            else:
                command.extend(['--report-path', str(output / (tool.removesuffix('.py') + '.json'))])
        run(tool.removesuffix('.py'), command)
    report = {'schema': 1, 'chromium_revision': upstream['revision'], 'view': str(view),
              'audited_action_scripts': count, 'probes': probes, 'passed': True,
              'engine_compiled': False, 'runtime_validated': False,
              'limitation': 'Reviewed import/help, executable, library and nested-source checks; '
                            'not proof of every dynamic dependency or compiler include.'}
    report_path.write_text(json.dumps(report, indent=2) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--view', type=Path)
    parser.add_argument('--report-path', type=Path)
    parser.add_argument('--diagnostics-dir', type=Path)
    args = parser.parse_args()
    view = args.view or Path(json.loads((SUPPORT / 'work/engine/current.json').read_text())['view'])
    report = check(view, report_path=args.report_path, diagnostics_dir=args.diagnostics_dir)
    print('Build prerequisites passed:', report['audited_action_scripts'], 'reviewed scripts,',
          len(report['probes']), 'safe probes; no engine compilation')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('error:', error, file=sys.stderr)
        sys.exit(2)
