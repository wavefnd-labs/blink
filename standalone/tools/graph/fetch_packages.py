from pathlib import Path
import hashlib
import json
import sys
import tarfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor

root = Path(__file__).resolve().parents[3]
work = root / 'standalone/work/graph'
sys.path.insert(0, str(work / 'src/third_party/depot_tools'))
import gclient_eval
spec_path = root / 'standalone/tools/dependencies.py'
import importlib.util
spec = importlib.util.spec_from_file_location('dependency_parser', spec_path)
parser = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parser)
variables = parser.parse_deps((work / 'src/DEPS').read_text())['vars']
variables.update(host_os='linux', host_cpu='x64', checkout_linux=True, checkout_x64=True, checkout_android=False, checkout_win=False, checkout_mac=False, checkout_ios=False, checkout_chromeos=False)
entries = json.loads((work / 'declared-deps.json').read_text())['dependencies']

def fetch(name):
    key = 'src/' + name
    entry = entries[key]
    objects = [o for o in entry['objects'] if gclient_eval.EvaluateCondition(o.get('condition', 'True'), variables)]
    for obj in objects:
        digest = obj['sha256sum']
        archive = work / 'packages' / digest
        archive.parent.mkdir(parents=True, exist_ok=True)
        if not archive.exists():
            url = 'https://storage.googleapis.com/' + entry['bucket'] + '/' + obj['object_name'] + '?generation=' + str(obj['generation'])
            temporary = archive.with_suffix('.partial')
            print('Downloading pinned package:', name, obj['size_bytes'], flush=True)
            with urllib.request.urlopen(url, timeout=60) as response, temporary.open('wb') as output:
                while data := response.read(1024 * 1024):
                    output.write(data)
            if temporary.stat().st_size != obj['size_bytes']:
                raise ValueError('Package size mismatch')
            with temporary.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
                    raise ValueError('Package checksum mismatch')
            temporary.rename(archive)
        with archive.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
                raise ValueError('Cached package checksum mismatch')
        destination = work / key
        destination.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive) as tar:
            tar.extractall(destination, filter='data')
        print('Verified and unpacked:', name, digest, flush=True)

with ThreadPoolExecutor(max_workers=3) as pool:
    list(pool.map(fetch, sys.argv[1:]))
