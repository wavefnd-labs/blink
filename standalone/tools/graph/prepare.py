"""Prepare the pinned Linux x64 GN investigation workspace, without compilation."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import platform
import subprocess
import sys
import urllib.request
import zipfile
import hydrate

ROOT, WORK, SRC, REV, CACHE = hydrate.ROOT, hydrate.WORK, hydrate.SRC, hydrate.REV, hydrate.CACHE
TOOLS = ROOT / 'standalone/tools'
if platform.system() != 'Linux' or platform.machine() != 'x86_64':
    raise SystemExit('This graph profile currently supports only Linux x64 hosts')
WORK.mkdir(parents=True, exist_ok=True)
# Source equality protects the real Blink tree, which is only linked read-only
# by convention into this separate, ignored upstream build layout.
subprocess.run([sys.executable, '-B', str(TOOLS / 'blink.py'), 'verify'], check=True)
if not CACHE.exists():
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', 'init', '--bare', '--quiet', str(CACHE)], check=True)
    for key, value in {'remote.origin.url': 'https://github.com/chromium/chromium.git', 'remote.origin.promisor': 'true', 'remote.origin.partialclonefilter': 'blob:none'}.items():
        hydrate.git(CACHE, 'config', key, value)
    hydrate.git(CACHE, 'fetch', '--filter=blob:none', '--depth=1', '--no-tags', 'origin', REV)
    hydrate.git(CACHE, 'update-ref', 'refs/heads/upstream', REV)
inputs = WORK / 'inputs'
inputs.mkdir(parents=True, exist_ok=True)
for name in ('DEPS', '.gn'):
    (inputs / name).write_bytes(hydrate.git(CACHE, 'show', REV + ':' + name))
spec = importlib.util.spec_from_file_location('dependency_parser', TOOLS / 'dependencies.py')
parser = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parser)
declared = parser.inventory(inputs / 'DEPS', REV)
verified = parser.verify_gitlinks(declared, hydrate.git(CACHE, 'ls-tree', '-r', '-z', REV))
(WORK / 'declared-deps.json').write_text(json.dumps(declared, indent=2) + '\n')
hydrate.DEPS = declared['dependencies']
print('Verified root Git dependency pins:', verified, flush=True)
if not SRC.exists():
    subprocess.run(['git', 'clone', '--shared', '--no-checkout', '--branch=upstream', str(CACHE), str(SRC)], check=True)
    subprocess.run(['git', '-C', str(SRC), 'update-ref', 'HEAD', REV], check=True)
if subprocess.check_output(['git', '-C', str(SRC), 'rev-parse', 'HEAD'], text=True).strip() != REV:
    raise SystemExit('Investigation workspace has a different HEAD; use a fresh workspace')
hydrate.hydrate(CACHE, REV, SRC, skip_blink=True)
link = SRC / 'third_party/blink'
if not link.exists():
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(ROOT, target_is_directory=True)
if not link.is_symlink() or link.resolve() != ROOT:
    raise SystemExit('Unexpected Blink source link in investigation workspace')
hydrate.dependency('third_party/depot_tools')
gn_tag = declared['variables']['gn_version']
url = 'https://chrome-infra-packages.appspot.com/dl/gn/gn/linux-amd64/+/' + gn_tag
bin_dir = WORK / 'tools'
bin_dir.mkdir(parents=True, exist_ok=True)
archive = bin_dir / 'gn.zip'
if not archive.exists():
    archive.write_bytes(urllib.request.urlopen(url, timeout=60).read())
with zipfile.ZipFile(io.BytesIO(archive.read_bytes())) as package:
    data = package.read('gn')
lock_path = ROOT / 'standalone/config/dependencies.lock.json'
if lock_path.exists():
    lock = json.loads(lock_path.read_text())
    if lock['chromium_revision'] == REV and hashlib.sha256(data).hexdigest() != lock['profile']['gn_sha256']:
        raise SystemExit('GN binary differs from the recorded pinned tool')
(bin_dir / 'gn').write_bytes(data)
(bin_dir / 'gn').chmod(0o755)
for script, arguments in (
    ('generate_gclient_args.py', []),
    ('fetch_packages.py', ['third_party/rust-toolchain', 'third_party/llvm-build/Release+Asserts', 'build/linux/debian_bullseye_amd64-sysroot']),
):
    subprocess.run([sys.executable, '-B', str(Path(__file__).parent / script), *arguments], check=True)
subprocess.run([sys.executable, '-B', str(SRC / 'build/util/lastchange.py'), '-o', str(SRC / 'build/util/LASTCHANGE')], check=True)
print('GN investigation workspace is ready; no engine or browser has been compiled', flush=True)
