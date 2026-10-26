#!/usr/bin/env python3
"""Replay reviewed GN generator actions directly, never Ninja compile/link edges."""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
SUPPORT = ROOT / 'standalone'
UNSAFE_SCRIPTS = {
    'build/modules/unified/generate_system_modulemap.py': 'invokes native compiler',
    'build/rust/gni_impl/run_build_script.py': 'arbitrary compiled Cargo build script',
    'build/rust/gni_impl/cpp_api_from_rust_wrapper.py': 'Crubit drives rustc',
    'third_party/dawn/tools/generate-sources-gn.py': 'go run compiles a helper',
    'v8/tools/metagen/metagen.py': 'libclang/secondary GN generation needs separate review',
}
BINARY_WRAPPERS = {
    'build/gn_run_binary.py': {'flatc', 'root_store_tool', 'signer_set_tool',
        'top_domain_generator', 'transport_security_state_generator',
        'character_data_generator', 'v8_context_snapshot_generator'},
    'third_party/perfetto/gn/run_binary.py': {'protozero_descriptor_diff'},
    'v8/tools/run.py': {'bytecode_builtins_list_generator', 'gen-regexp-special-case',
        'mksnapshot', 'torque'},
}
NATIVE_OUTPUT = re.compile(r'(?:\.(?:o|obj|a|so|dylib|dll|exe|pcm|rmeta|rlib)(?:\.|$))')
FORBIDDEN_PROGRAMS = {'cc', 'c++', 'gcc', 'g++', 'clang', 'clang++', 'rustc',
    'cargo', 'go', 'nasm', 'yasm', 'as', 'ld', 'ld.lld', 'lld', 'mold',
    'ar', 'llvm-ar', 'ninja', 'autoninja', 'make', 'cmake'}


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def ninja_expand(text, variables=None):
    """Expand only Ninja's escaped literals and explicit reviewed variables."""
    variables = variables or {}
    def replace(match):
        name = match.group(1)
        if name in (' ', ':', '$'):
            return name
        key = name[1:-1] if name.startswith('{') else name
        if key not in variables:
            raise ValueError('Unresolved Ninja variable: ' + key)
        return variables[key]
    return re.sub(r'\$(\{[^}]+\}|[A-Za-z0-9_]+|[ $:])', replace, text)


def ninja_tokens(text):
    # Path tokens use Ninja escapes, not shell quoting.
    sentinel = '\x00'
    if sentinel in text:
        raise ValueError('NUL in Ninja path')
    protected = text.replace('$ ', sentinel)
    return [ninja_expand(x.replace(sentinel, '$ ')) for x in protected.split()]


def logical_lines(text):
    pending = ''
    for line in text.splitlines():
        line = pending + (line.lstrip() if pending else line)
        if line.endswith('$') and not line.endswith('$$'):
            pending = line[:-1]
            continue
        yield line
        pending = ''
    if pending:
        raise ValueError('Unterminated Ninja continuation')


def parse_toolchain(path):
    rules, edges = {}, {}
    current = None
    for line in logical_lines(Path(path).read_text()):
        if line.startswith('rule '):
            current = line[5:]
            rules[current] = {}
        elif line.startswith('  ') and current and ' = ' in line:
            key, value = line.strip().split(' = ', 1)
            rules[current][key] = value
        elif line.startswith('build '):
            current = None
            match = re.match(r'build (.*?)(?<!\$): (\S+)(?: (.*))?$', line)
            if not match:
                raise ValueError('Unsupported Ninja build edge')
            outputs = [x for x in ninja_tokens(match[1]) if x != '|']
            inputs = [x for x in ninja_tokens(match[3] or '') if x not in ('|', '||')]
            edge = {'rule':match[2], 'outputs':outputs, 'inputs':inputs}
            for out in outputs:
                edges[out] = edge
        elif not line.startswith('  '):
            current = None
    return rules, edges


def within_output(output, name):
    path = Path(os.path.abspath(output / name))
    if not path.is_relative_to(output):
        raise ValueError('Generator output/rsp escapes build directory: ' + name)
    # Do not let a pre-existing output symlink redirect writes to pristine source.
    if path.is_symlink() or not path.resolve().is_relative_to(output.resolve()):
        raise ValueError('Generator output/rsp is a source/external alias: ' + name)
    return path


def reject_shell(command):
    lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|<>')
    lexer.whitespace_split = True
    tokens = list(lexer)
    if any(t and all(c in ';&|<>' for c in t) for t in tokens):
        raise ValueError('Shell operators are not generator commands')
    # Command substitution must not be interpreted by a shell, and is rejected
    # rather than silently changing the upstream action's meaning.
    quote=None;escaped=False
    for index,char in enumerate(command):
        if escaped:
            escaped=False;continue
        if char=='\\' and quote!="'":
            escaped=True;continue
        if char in ("'", '"'):
            if quote is None:quote=char
            elif quote==char:quote=None
        elif quote!="'" and (char=='`' or command[index:index+2]=='$('):
            raise ValueError('Shell substitution is forbidden')
    return shlex.split(command)


def safe_command(command, script, node, output, python, approved_hash):
    argv = reject_shell(command)
    if len(argv) < 2 or argv[0] != python:
        raise ValueError('Generator must use the dependency profile Python')
    expected = Path(os.path.abspath(output / ('../../' + script)))
    actual = Path(os.path.abspath(output / argv[1]))
    if actual != expected or not actual.is_file() or digest(actual) != approved_hash:
        raise ValueError('Generator script does not match reviewed GN owner')
    if any(NATIVE_OUTPUT.search(x) for x in node['outputs']):
        return argv, 'native-artifact-output'
    if script in UNSAFE_SCRIPTS:
        return argv, UNSAFE_SCRIPTS[script]
    if script in BINARY_WRAPPERS:
        if len(argv) < 3 or Path(argv[2]).name not in BINARY_WRAPPERS[script]:
            return argv, 'unreviewed-compiled-helper (includes native assemblers)'
        helper=within_output(output,argv[2])
        if not helper.is_file():
            return argv, 'missing-compiled-helper: ' + argv[2]
    if Path(argv[0]).name in FORBIDDEN_PROGRAMS:
        raise ValueError('Native compiler/link/build command is forbidden')
    return argv, None


def output_name(output, value):
    if not value.startswith('//out/Engine/'):
        raise ValueError('Action output is outside selected build profile: ' + value)
    return value.removeprefix('//out/Engine/')


def load_plan(view):
    view = Path(view).absolute()
    output = view / 'out/Engine'
    graph_path = view.parent / 'graph.json'
    graph = json.loads(graph_path.read_text())
    dependencies = json.loads((SUPPORT/'config/dependencies.lock.json').read_text())
    audit = json.loads((SUPPORT/'config/build-prerequisites.json').read_text())
    python = dependencies['profile']['script_executable']
    ninja = view/'third_party/ninja/ninja'
    toolchains = [output/'toolchain.ninja', *sorted(output.glob('*/toolchain.ninja'))]
    rules, edges = {}, {}
    for path in toolchains:
        a,b=parse_toolchain(path);rules.update(a);edges.update(b)
    owners = {}
    for name,node in graph.items():
        if node.get('type') not in ('action','action_foreach'):
            continue
        for value in node.get('outputs',[]):
            key=output_name(output,value)
            if key in owners and owners[key]!=name:
                raise ValueError('Ambiguous GN action output: '+key)
            owners[key]=name
    # Restrict compdb itself to GN action-owned rules, avoiding all native
    # compile/link command records as well as their very large output.
    action_rules=sorted({edges[name]['rule'] for name in owners if name in edges})
    raw=subprocess.check_output([str(ninja), '-C', str(output), '-t', 'compdb', *action_rules], text=True, errors='surrogateescape')
    commands=json.loads(raw)
    target_text=subprocess.check_output([str(ninja), '-C', str(output), '-t', 'targets', 'all'], text=True)
    virtual_targets={line.rsplit(': ',1)[0]:line.rsplit(': ',1)[1] for line in target_text.splitlines()
                     if ': ' in line and line.rsplit(': ',1)[1] in ('phony','stamp')}
    entries=[];seen=set()
    for command in commands:
        name=owners.get(command['output'])
        if not name:continue
        if command['output'] in seen:raise ValueError('Duplicate action command')
        seen.add(command['output']);node=graph[name];script=node['script'].removeprefix('//')
        expected=audit['audited_action_scripts'].get(script)
        if expected is None:raise ValueError('Selected generator script is unreviewed: '+script)
        edge=edges.get(command['output'])
        if edge is None:raise ValueError('Action edge not in generated toolchain: '+command['output'])
        try:
            argv,skip=safe_command(command['command'],script,edge,output,python,expected)
        except ValueError as error:
            raise ValueError(name + ': ' + str(error) + '; command=' + command['command']) from error
        rule=rules[edge['rule']];rsp=None
        if 'rspfile' in rule:
            # GN's selected action rsp bindings are literal Ninja strings. Do
            # not approximate an unknown foreach/edge variable.
            try:
                rsp={'path':ninja_expand(rule['rspfile']),
                     'content':ninja_expand(rule['rspfile_content'])}
                within_output(output,rsp['path'])
                rsp['sha256']=hashlib.sha256(rsp['content'].encode()).hexdigest()
            except ValueError as error:skip='unresolved-rsp: '+str(error)
        for value in edge['outputs']:within_output(output,value)
        helper=None
        if script in BINARY_WRAPPERS and not skip:
            path=within_output(output,argv[2]);helper={'path':argv[2],'sha256':digest(path)}
        entries.append({'compiled_helper':helper,'id':len(entries),'target':name,'script':script,
            'script_sha256':expected,'rule':edge['rule'],'command':argv,
            'command_sha256':hashlib.sha256(command['command'].encode()).hexdigest(),
            'outputs':edge['outputs'],'inputs':edge['inputs'],
            'virtual_inputs':{name:virtual_targets[name] for name in edge['inputs'] if name in virtual_targets},
            'rsp':rsp,'skip':skip,
            'blink_scope':script.startswith('third_party/blink/')})
    found={x['target'] for x in entries}
    selected={name for name,node in graph.items() if node.get('type')in('action','action_foreach')}
    if found!=selected:raise ValueError('Selected GN action missing exact Ninja command: '+repr(sorted(selected-found)))
    return {'schema':1,'view':str(view),'output':str(output),'graph_sha256':digest(graph_path),
        'ninja_sha256':digest(ninja),'ninja_targets_sha256':hashlib.sha256(target_text.encode()).hexdigest(),'dependency_lock_sha256':digest(SUPPORT/'config/dependencies.lock.json'),
        'prerequisite_config_sha256':digest(SUPPORT/'config/build-prerequisites.json'),
        'toolchain_files':{str(p.relative_to(output)):digest(p) for p in toolchains},
        'selected_action_targets':len(selected),'commands':len(entries),'entries':entries,
        'engine_compiled':False,'note':'Direct reviewed Python generator commands only; Ninja is used only with -t compdb. Native compile/link/assembly outputs and unsafe wrappers are skipped.'}


def prerequisite_files(entry, output):
    missing=[]
    for value in entry['inputs']:
        p=Path(os.path.abspath(output/value))
        # Ordering stamps do not carry data. They are not executed by this tool;
        # concrete generated and compiled inputs remain mandatory.
        if entry.get('virtual_inputs',{}).get(value) in ('phony','stamp'):continue
        if not p.exists():missing.append(value)
    return missing


def detach_shared_output(path, metadata, expected_hash):
    """Detach this owned output name; never write through its other hardlinks."""
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(prefix='.generator-restat-',dir=path.parent,
                                         delete=False) as stream:
            temporary=Path(stream.name)
            with path.open('rb') as original:shutil.copyfileobj(original,stream)
        if digest(temporary)!=expected_hash:
            raise ValueError('Shared generator output changed while copying')
        current=path.stat(follow_symlinks=False)
        fields=('st_dev','st_ino','st_mode','st_size','st_mtime_ns','st_ctime_ns')
        if any(getattr(current,k)!=getattr(metadata,k) for k in fields):
            raise ValueError('Shared generator output changed before detaching')
        os.chmod(temporary,stat.S_IMODE(metadata.st_mode))
        os.utime(temporary,ns=(metadata.st_atime_ns,metadata.st_mtime_ns))
        os.replace(temporary,path);temporary=None
        if path.stat(follow_symlinks=False).st_nlink!=1:
            raise ValueError('Generator output is still shared after detaching')
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)


def snapshot_outputs(output, names):
    """Remember output bytes/times and privately detach shared output names."""
    before={}
    for name in names:
        path=within_output(output,name)
        if not path.exists():continue
        metadata=path.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError('Generator output is not a regular file: '+name)
        expected=digest(path)
        shared=metadata.st_nlink>1
        if shared:detach_shared_output(path,metadata,expected)
        before[name]={'sha256':expected,'mode':stat.S_IMODE(metadata.st_mode),
                      'atime_ns':metadata.st_atime_ns,'mtime_ns':metadata.st_mtime_ns,
                      'detached_shared_output':shared}
    return before


def restat_outputs(output, names, before, hashes):
    """Preserve old mtime only when an action reproduced unchanged bytes/mode."""
    restored=[];retained={}
    for name in names:
        path=within_output(output,name)
        original=before.get(name)
        if not path.is_file():retained[name]='missing-output';continue
        metadata=path.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink!=1:
            raise ValueError('Generator output became a shared/non-regular file: '+name)
        if original is None:retained[name]='new-output';continue
        if hashes[name]!=original['sha256']:retained[name]='changed-bytes';continue
        if stat.S_IMODE(metadata.st_mode)!=original['mode']:
            retained[name]='changed-mode';continue
        # Keep the post-action/access atime. Restat changes only mtime back to
        # the recorded value; mode and changed/new output timestamps are kept.
        os.utime(path,ns=(metadata.st_atime_ns,original['mtime_ns']),follow_symlinks=False)
        restored.append(name)
    return {'restored_mtime':restored,'retained_current_mtime':retained}


def native_snapshot(output):
    return {str(p.relative_to(output)):(p.stat().st_size,p.stat().st_mtime_ns)
            for p in output.rglob('*') if p.is_file() and NATIVE_OUTPUT.search(p.name)}


def run_plan(plan, *, scope='non-blink', timeout=180, report_path=None, resume=None):
    output=Path(plan['output']);view=Path(plan['view'])
    if digest(view.parent/'graph.json')!=plan['graph_sha256']:
        raise ValueError('GN graph changed after planning')
    for name,sha in plan['toolchain_files'].items():
        if digest(output/name)!=sha:raise ValueError('Ninja rules changed after planning')
    directory=Path(report_path).parent if report_path else SUPPORT/'work/diagnostics/generator-sweep'
    run_id=str(time.time_ns())
    stem=Path(report_path).stem if report_path else 'run'
    logs=directory/(stem+'-'+run_id+'-logs');logs.mkdir(parents=True,exist_ok=True)
    env=os.environ.copy();env.pop('PYTHONPATH',None)
    env.update(PYTHONDONTWRITEBYTECODE='1',PYTHONPYCACHEPREFIX=str(SUPPORT/'work/engine/python-cache'))
    native_before=native_snapshot(output)
    result={'schema':1,'run_id':run_id,'view':plan['view'],'graph_sha256':plan['graph_sha256'],'scope':scope,
            'engine_compiled':False,'native_compile_or_link_commands':0,'runner_sha256':digest(Path(__file__)),
            'output_restat':'unchanged bytes and mode preserve pre-action mtime only','results':[]}
    approved=json.loads((SUPPORT/'config/build-prerequisites.json').read_text())['audited_action_scripts']
    selected=[e for e in plan['entries'] if scope=='all' or (scope=='non-blink' and not e['blink_scope']) or(scope=='blink' and e['blink_scope'])]
    result['planned_commands']=len(selected)
    result['planned_targets']=len({e['target'] for e in selected})
    previous={}
    if resume:
        old=json.loads(Path(resume).read_text())
        if old['view']!=plan['view'] or old['graph_sha256']!=plan['graph_sha256']:
            raise ValueError('Resume receipt belongs to another view/graph')
        previous={r['id']:r for r in old['results'] if r['status'] in ('passed','reused-passed')}
    pending=[]
    for entry in selected:
        if entry['skip']:result['results'].append({'id':entry['id'],'target':entry['target'],'status':'skipped-policy','reason':entry['skip']})
        else:
            old=previous.get(entry['id']);hashes=old.get('output_sha256',{}) if old else {}
            if (old and old.get('command_sha256')==entry['command_sha256']
                    and set(hashes)==set(entry['outputs'])
                    and all((output/name).is_file() and digest(output/name)==sha for name,sha in hashes.items())):
                result['results'].append({**old,'status':'reused-passed','source_receipt':str(resume)})
            else:pending.append(entry)
    def save():
        result['counts']=dict(Counter(x['status'] for x in result['results']))
        result['pending']=len(pending)
        if report_path:Path(report_path).write_text(json.dumps(result,indent=2)+'\n')
    save()
    while pending:
        progressed=False;next_pending=[]
        for entry in pending:
            missing=prerequisite_files(entry,output)
            if missing:next_pending.append(entry);continue
            # Recheck original script and native-output policy immediately before
            # execution; an edited plan cannot turn this into compiler replay.
            node={'outputs':entry['outputs']}
            argv,skip=safe_command(shlex.join(entry['command']),entry['script'],node,output,
                json.loads((SUPPORT/'config/dependencies.lock.json').read_text())['profile']['script_executable'],approved[entry['script']])
            helper=entry.get('compiled_helper')
            if helper and digest(within_output(output,helper['path']))!=helper['sha256']:
                raise ValueError('Compiled generator helper changed after planning')
            if skip:result['results'].append({'id':entry['id'],'target':entry['target'],'status':'skipped-policy','reason':skip});progressed=True;continue
            rsp=entry.get('rsp');rsp_record=None
            if rsp:
                dest=within_output(output,rsp['path']);data=rsp['content'].encode()
                if hashlib.sha256(data).hexdigest()!=rsp['sha256']:raise ValueError('RSP plan changed')
                if dest.exists() and dest.read_bytes()!=data:
                    result['results'].append({'id':entry['id'],'target':entry['target'],'status':'skipped-rsp-conflict','path':rsp['path']});progressed=True;continue
                created=not dest.exists();dest.parent.mkdir(parents=True,exist_ok=True)
                if created:dest.write_bytes(data)
                rsp_record={'path':rsp['path'],'sha256':digest(dest),'created':created}
            for name in entry['outputs']:within_output(output,name).parent.mkdir(parents=True,exist_ok=True)
            before_outputs=snapshot_outputs(output,entry['outputs'])
            log=logs/(str(entry['id'])+'.log');start=time.monotonic()
            with log.open('w') as stream:
                process=subprocess.Popen(argv,cwd=output,env=env,stdout=stream,
                    stderr=subprocess.STDOUT,start_new_session=True)
                try:
                    code=process.wait(timeout=timeout)
                    status='passed' if code==0 else 'failed'
                except subprocess.TimeoutExpired:
                    # A Python action can spawn worker/helper children. Kill the
                    # complete private process group, not just its wrapper.
                    try:os.killpg(process.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                    process.wait();status='timeout';code=None
            out={name:digest(output/name) for name in entry['outputs'] if (output/name).is_file()}
            missing_outputs=[name for name in entry['outputs'] if not(output/name).exists()]
            if status=='passed' and missing_outputs:status='failed-output'
            restat={'restored_mtime':[],'retained_current_mtime':{}}
            if status=='passed':restat=restat_outputs(output,entry['outputs'],before_outputs,out)
            result['results'].append({'id':entry['id'],'target':entry['target'],'status':status,'exit_code':code,
                'elapsed_seconds':round(time.monotonic()-start,3),'command_sha256':entry['command_sha256'],
                'rsp':rsp_record,'log':str(log),'log_sha256':digest(log),'output_sha256':out,
                'missing_outputs':missing_outputs,'outputs_before':before_outputs,'restat':restat})
            progressed=True
            if len(result['results'])%50==0:
                save();print('Generator progress:',len(result['results']),flush=True)
        pending=next_pending;save()
        if not progressed:break
    for entry in pending:
        result['results'].append({'id':entry['id'],'target':entry['target'],'status':'skipped-missing-prerequisite',
            'missing':prerequisite_files(entry,output)})
    pending=[]
    native_after=native_snapshot(output)
    result['native_artifacts']={'before_count':len(native_before),'after_count':len(native_after),
        'changed_or_removed':[p for p,s in native_before.items() if native_after.get(p)!=s],
        'new':[p for p in native_after if p not in native_before]}
    result['native_artifacts_unchanged']=native_before==native_after
    save();return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('plan','run'))
    parser.add_argument('--view',type=Path)
    parser.add_argument('--scope',choices=('non-blink','blink','all'),default='non-blink')
    parser.add_argument('--resume',type=Path,help='Reuse exact passed command/output receipts from a prior sweep')
    parser.add_argument('--timeout',type=int,default=180)
    parser.add_argument('--report',type=Path,default=SUPPORT/'work/diagnostics/generator-sweep/report.json')
    args=parser.parse_args()
    if not 1<=args.timeout<=1800:parser.error('timeout must be between 1 and 1800 seconds')
    view=args.view or Path(json.loads((SUPPORT/'work/engine/current.json').read_text())['view'])
    plan=load_plan(view);args.report.parent.mkdir(parents=True,exist_ok=True)
    plan_path=args.report.with_suffix('.plan.json');plan_path.write_text(json.dumps(plan,indent=2)+'\n')
    if args.command=='plan':print(json.dumps({'plan':str(plan_path),'targets':plan['selected_action_targets'],'commands':plan['commands'],'skip':dict(Counter(e['skip'] for e in plan['entries'] if e['skip']))},indent=2));return 0
    report=run_plan(plan,scope=args.scope,timeout=args.timeout,report_path=args.report,resume=args.resume)
    print(json.dumps({'report':str(args.report),'counts':report['counts']},indent=2))
    return 1 if not report['native_artifacts_unchanged'] or any(x['status']in('failed','failed-output','timeout') for x in report['results']) else 0

if __name__=='__main__':
    try:sys.exit(main())
    except (OSError,ValueError,subprocess.SubprocessError) as error:
        print('error:',error,file=sys.stderr);sys.exit(2)
