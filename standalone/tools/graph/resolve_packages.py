"""Freeze selected CIPD references to immutable content hashes and select GCS objects."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import hashlib
import json
from pathlib import Path
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[3]
WORK = ROOT / 'standalone/work/graph'
sys.path.insert(0, str(WORK / 'src/third_party/depot_tools'))
import gclient_eval
path = ROOT / 'standalone/config/dependencies.lock.json'
lock = json.loads(path.read_text())
variables = json.loads((WORK / 'declared-deps.json').read_text())['variables']
variables.update(host_os='linux', host_cpu='x64', checkout_linux=True, checkout_x64=True, checkout_android=False, checkout_win=False, checkout_mac=False, checkout_ios=False, checkout_chromeos=False)


def resolve(item):
    name, entry = item
    entry['selected_for_profile'] = gclient_eval.EvaluateCondition(entry.get('condition', 'True'), variables)
    if entry.get('dep_type') == 'gcs':
        entry['selected_objects'] = [obj for obj in entry['objects'] if entry['selected_for_profile'] and gclient_eval.EvaluateCondition(obj.get('condition', 'True'), variables)]
    if entry.get('dep_type') == 'cipd' and entry['selected_for_profile']:
        for package in entry['packages']:
            expanded = package['package'].replace('${{platform}}', 'linux-amd64').replace('${{arch}}', 'amd64')
            if '$' in expanded or '{' in expanded:
                raise ValueError('Unresolved package platform: ' + expanded)
            request = urllib.request.Request('https://chrome-infra-packages.appspot.com/prpc/cipd.Repository/ResolveVersion', data=json.dumps({'package': expanded, 'version': package['version']}).encode(), headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode()
            result = json.loads(raw.removeprefix(")]}'\n"))
            if result['package'] != expanded or not result.get('instance', {}).get('hexDigest'):
                raise ValueError('Invalid CIPD resolution for ' + expanded)
            package['resolved_package'] = expanded
            package['instance'] = result['instance']
            if name == 'buildtools/linux64' and result['instance'].get('hashAlgo') == 'SHA256':
                if hashlib.sha256((WORK / 'tools/gn.zip').read_bytes()).hexdigest() != result['instance']['hexDigest']:
                    raise ValueError('Downloaded GN package differs from resolved CIPD content')
    return name, entry


with ThreadPoolExecutor(max_workers=5) as pool:
    lock['dependencies'] = dict(pool.map(resolve, lock['dependencies'].items()))
lock['package_resolution'] = 'CIPD ObjectRef content hashes; GCS generation, size and SHA256 retained for selected Linux objects'
path.write_text(json.dumps(lock, indent=2) + '\n')
print('Frozen package identities:', sum(len(e.get('packages', [])) for e in lock['dependencies'].values() if e.get('dep_type') == 'cipd' and e['selected_for_profile']))
