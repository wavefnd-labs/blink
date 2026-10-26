from pathlib import Path
import importlib.util
import json
import sys
root = Path(__file__).resolve().parents[3]
src = root / 'standalone/work/graph/src'
sys.path.insert(0, str(src / 'third_party/depot_tools'))
import gclient_eval
spec = importlib.util.spec_from_file_location('dependency_tools', root / 'standalone/tools/dependencies.py')
deps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deps)
parsed = deps.parse_deps((src / 'DEPS').read_text())
variables = {**parsed['vars'], 'checkout_linux': True, 'checkout_win': False, 'checkout_android': False, 'checkout_mac': False, 'checkout_chromeos': False, 'checkout_ios': False, 'checkout_x64': True, 'host_os': 'linux', 'host_cpu': 'x64'}
lines = ['# Generated from pinned DEPS using official gclient condition evaluation.']
for name in parsed['gclient_gn_args']:
    val = variables[name]
    if isinstance(val, str) and not isinstance(val, deps.LiteralString):
        val = gclient_eval.EvaluateCondition(val, variables)
    if isinstance(val, bool):
        encoded = str(val).lower()
    elif isinstance(val, str):
        encoded = json.dumps(str(val)).replace('$', '\\$')
    else:
        raise ValueError(f'Unsupported GN argument {name}: {val!r}')
    lines.append(f'{name} = {encoded}')
out = src / 'build/config/gclient_args.gni'
out.write_text('\n'.join(lines) + '\n')
print(out.read_text())
