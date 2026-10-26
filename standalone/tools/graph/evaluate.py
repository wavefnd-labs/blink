from pathlib import Path
import json
import re
import subprocess
import sys
from hydrate import CACHE, DEPS, REV, SRC, WORK, dependency, hydrate

GN = WORK / 'tools/gn'
OUT = SRC / 'out/BlinkGraph'
ARGS = 'target_os="linux" target_cpu="x64" is_debug=false is_component_build=false use_sysroot=true'
command = [str(GN), 'gen', str(OUT), '--root=' + str(SRC), '--root-target=//third_party/blink/public:blink', '--root-pattern=//third_party/blink/public:blink', '--script-executable=/usr/bin/python3', '--tracelog=' + str(WORK / 'gn-trace.json'), '--args=' + ARGS]
owners = sorted((name.removeprefix('src/') for name,e in DEPS.items() if e.get('dep_type') == 'git'), key=len, reverse=True)
seen = set()
for attempt in range(100):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    (WORK / 'gn-gen.log').write_text(result.stdout)
    if result.returncode == 0:
        print(result.stdout, flush=True)
        (WORK / 'generation-command.json').write_text(json.dumps(command, indent=2) + '\n')
        break
    print(result.stdout[:2200], flush=True)
    matches = re.findall(r"[\"']" + re.escape(str(SRC)) + r"/([^\"'\n]+)[\"']", result.stdout)
    missing = next((m.rstrip('.') for m in matches if not (SRC / m.rstrip('.')).exists()), None)
    if missing is None or missing in seen:
        print('Requires explicit handling; stopped without changing build semantics', flush=True)
        sys.exit(result.returncode)
    seen.add(missing)
    owner = next((p for p in owners if missing == p or missing.startswith(p + '/')), None)
    with (WORK / 'materialization-events.jsonl').open('a') as log:
        log.write(json.dumps({'missing': missing, 'owner': owner or 'chromium'}) + '\n')
    print('Resolving original build input:', missing, flush=True)
    if owner:
        dependency(owner)
        if not (SRC / missing).exists():
            dependency(owner, [missing[len(owner) + 1:]])
    else:
        hydrate(CACHE, REV, SRC, [missing])
    if not (SRC / missing).exists():
        print('Input not in original Git tree; generator or package is required:', missing, flush=True)
        sys.exit(2)
else:
    raise SystemExit('Too many unresolved inputs')
