#!/usr/bin/env python3
"""Keep rustfmt's unchanged ignore configuration inside the engine source view.

The pinned formatter resolves a symlinked configuration to the source cache, then
panics when an engine-view generated file is outside that ignore-pattern root.
This layout adaptation copies the original bytes; formatting stays enabled.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
SUPPORT = ROOT / 'standalone'
BASE = SUPPORT / 'work/graph/src'
RECIPE = SUPPORT / 'config/rustfmt-layout.json'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_recipe(path=RECIPE):
    recipe = json.loads(Path(path).read_text())
    if recipe.get('schema') != 1:
        raise ValueError('Unsupported rustfmt layout recipe')
    return recipe


def configuration_inputs(base, revision, recipe=None):
    """Return reviewed, byte-identical source-view copies for engine.prepare()."""
    recipe = read_recipe() if recipe is None else recipe
    if recipe['chromium_revision'] != revision:
        raise ValueError('Rustfmt layout and upstream revision differ')
    inputs = {}
    for relative, entry in recipe['files'].items():
        path = PurePosixPath(relative)
        if (path.is_absolute() or '..' in path.parts or not path.parts
                or str(path) != relative or entry.get('mode') != 'copy'):
            raise ValueError('Invalid rustfmt layout copy: ' + relative)
        source = Path(base) / relative
        data = source.read_bytes()
        if digest(data) != entry['original_sha256']:
            raise ValueError('Pristine rustfmt configuration changed: ' + relative)
        inputs[relative] = data
    return inputs


def verify_view(view, inputs):
    for relative, data in inputs.items():
        path = Path(view) / relative
        if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
            raise ValueError('Rustfmt configuration must be an unchanged local copy: ' + relative)


def regression_check(base=BASE, recipe=None):
    """Run only rustfmt: demonstrate the failing layout and the repaired layout."""
    recipe = read_recipe() if recipe is None else recipe
    inputs = configuration_inputs(base, recipe['chromium_revision'], recipe)
    executable = Path(base) / recipe['regression_tool']['path']
    if digest(executable.read_bytes()) != recipe['regression_tool']['sha256']:
        raise ValueError('Rustfmt executable differs from the pinned regression tool')
    results = {}
    with tempfile.TemporaryDirectory(prefix='blink-rustfmt-layout-') as temporary:
        view = Path(temporary) / 'src'
        output = view / 'out/Engine'
        output.mkdir(parents=True)
        generated = output / 'gen/bindings.rs'
        generated.parent.mkdir()
        config = view / '.rustfmt.toml'
        config.symlink_to((Path(base) / '.rustfmt.toml').resolve())
        command = [str(executable), 'gen/bindings.rs',
                   '--config=normalize_doc_attributes=true']
        for mode in ('symlink', 'copy'):
            generated.write_text('pub fn answer( )->u32{42}\n')
            if mode == 'copy':
                config.unlink()
                for relative, data in inputs.items():
                    (view / relative).write_bytes(data)
                verify_view(view, inputs)
            result = subprocess.run(command, cwd=output, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            results[mode] = {
                'exit_code': result.returncode,
                'outside_root_panic': 'path is expected to be under the root' in result.stdout,
                'formatted_source': generated.read_text(),
            }
        if (results['symlink']['exit_code'] != 101
                or not results['symlink']['outside_root_panic']):
            raise ValueError('Pinned rustfmt no longer reproduces the reviewed layout failure')
        if (results['copy']['exit_code'] != 0
                or results['copy']['formatted_source'] != 'pub fn answer() -> u32 {\n    42\n}\n'):
            raise ValueError('Copied rustfmt configuration did not format the generated source')
    return {
        'schema': 1,
        'chromium_revision': recipe['chromium_revision'],
        'configuration_files': {name: digest(data) for name, data in inputs.items()},
        'rustfmt_sha256': recipe['regression_tool']['sha256'],
        'command': [recipe['regression_tool']['path'], 'gen/bindings.rs',
                    '--config=normalize_doc_attributes=true'],
        'working_directory': '$TEMP/src/out/Engine',
        'cases': results,
        'verified': True,
        'formatting_enabled': True,
        'full_compilation': False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['check'])
    parser.add_argument('--report', type=Path,
                        default=SUPPORT / 'reports/linux-x64-rustfmt-layout.json')
    args = parser.parse_args()
    result = regression_check()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + '\n')
    print('Verified pinned rustfmt with byte-identical configuration in the source view')


if __name__ == '__main__':
    main()
