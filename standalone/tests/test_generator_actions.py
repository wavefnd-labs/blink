import hashlib
import importlib.util
import json
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest import mock

SPEC=importlib.util.spec_from_file_location('generator_actions',Path(__file__).resolve().parents[1]/'tools/generator_actions.py')
g=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(g)

class GeneratorActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.view=self.root/'src';self.out=self.view/'out/Engine';self.out.mkdir(parents=True)
        self.support=self.root/'support';(self.support/'config').mkdir(parents=True)
        self.script=self.view/'entry.py';self.script.write_text('from pathlib import Path\nPath("gen/result.h").write_text("generated")\n')
        self.hash=g.digest(self.script);self.graph=self.view.parent/'graph.json';self.graph.write_text('{}')
        (self.support/'config/dependencies.lock.json').write_text(json.dumps({'profile':{'script_executable':sys.executable}}))
        (self.support/'config/build-prerequisites.json').write_text(json.dumps({'audited_action_scripts':{'entry.py':self.hash}}))
        self.patch=mock.patch.object(g,'SUPPORT',self.support);self.patch.start();self.addCleanup(self.patch.stop)
    def plan(self, outputs=None):
        argv=[sys.executable,'../../entry.py'];outs=outputs or ['gen/result.h']
        return {'view':str(self.view),'output':str(self.out),'graph_sha256':g.digest(self.graph),'toolchain_files':{},'entries':[{'id':0,'target':'//:generate','script':'entry.py','script_sha256':self.hash,'command':argv,'command_sha256':hashlib.sha256(shlex.join(argv).encode()).hexdigest(),'outputs':outs,'inputs':[],'rsp':None,'skip':None,'blink_scope':False}]}
    def test_ninja_literals_and_continuations(self):
        self.assertEqual(g.ninja_tokens('a$ b x$:y'),['a b','x:y'])
        self.assertEqual(g.ninja_expand('x$$y$ z$:'),'x$y z:')
        self.assertEqual(list(g.logical_lines('a$\n  b\nc')),['ab','c'])
        with self.assertRaisesRegex(ValueError,'Unresolved'):g.ninja_expand('${unknown}')
    def test_parse_rule_rsp_and_multi_output_edge(self):
        p=self.root/'rules.ninja';p.write_text('rule action\n  command = python entry\n  rspfile = args.rsp\n  rspfile_content = --value a$ b\nbuild gen/a$ b.h gen/c.h: action ../../entry.py | gen/in.h || obj/order.stamp\n')
        rules,edges=g.parse_toolchain(p)
        self.assertEqual(rules['action']['rspfile_content'],'--value a$ b')
        self.assertEqual(edges['gen/a b.h']['outputs'],['gen/a b.h','gen/c.h'])
        self.assertEqual(edges['gen/c.h']['inputs'],['../../entry.py','gen/in.h','obj/order.stamp'])
    def test_rejects_shell_execution_but_preserves_escaped_literals(self):
        for text in ['python entry; clang -c file.cc','python entry && ninja','python entry $(touch bad)','python entry `touch bad`']:
            with self.assertRaises(ValueError):g.reject_shell(text)
        self.assertEqual(g.reject_shell('python entry \\`literal\\`'),['python','entry','`literal`'])
    def test_action_type_does_not_allow_native_assembly(self):
        argv,reason=g.safe_command(shlex.join([sys.executable,'../../entry.py']),'entry.py',{'outputs':['obj/file.o']},self.out,sys.executable,self.hash)
        self.assertEqual(reason,'native-artifact-output')
        self.assertEqual(g.safe_command(shlex.join([sys.executable,'../../entry.py']),'entry.py',{'outputs':['libengine.so.TOC']},self.out,sys.executable,self.hash)[1],'native-artifact-output')
    def test_unknown_binary_helper_cannot_be_compiler(self):
        script='build/gn_run_binary.py';p=self.view/script;p.parent.mkdir();p.write_text('pass\n')
        argv,reason=g.safe_command(shlex.join([sys.executable,'../../'+script,'nasm','input.asm']),script,{'outputs':['gen/misleading.h']},self.out,sys.executable,g.digest(p))
        self.assertIn('unreviewed-compiled-helper',reason)
    def test_output_path_and_source_alias_protected(self):
        for name in ['../../source.h','/tmp/outside.h']:
            with self.assertRaises(ValueError):g.within_output(self.out,name)
        (self.out/'alias').symlink_to(self.view,target_is_directory=True)
        with self.assertRaises(ValueError):g.within_output(self.out,'alias/file.h')
    def test_real_python_generation_and_output_hash(self):
        p=self.plan();r=g.run_plan(p,report_path=self.root/'report.json')
        self.assertEqual(r['counts'],{'passed':1})
        self.assertEqual(r['results'][0]['output_sha256']['gen/result.h'],hashlib.sha256(b'generated').hexdigest())
    def test_exit_zero_without_output_is_failure(self):
        self.script.write_text('pass\n');self.hash=g.digest(self.script)
        (self.support/'config/build-prerequisites.json').write_text(json.dumps({'audited_action_scripts':{'entry.py':self.hash}}))
        r=g.run_plan(self.plan(),report_path=self.root/'report.json')
        self.assertEqual(r['counts'],{'failed-output':1})
    def test_changed_plan_script_hash_cannot_override_review(self):
        p=self.plan();self.script.write_text('raise SystemExit("changed")\n');p['entries'][0]['script_sha256']=g.digest(self.script)
        with self.assertRaisesRegex(ValueError,'reviewed GN owner'):g.run_plan(p,report_path=self.root/'report.json')
    def test_missing_inputs_skip_without_ninja_or_generator(self):
        p=self.plan();p['entries'][0]['inputs']=['gen/unbuilt-helper']
        with mock.patch.object(g.subprocess,'Popen') as run:
            r=g.run_plan(p,report_path=self.root/'report.json');run.assert_not_called()
        self.assertEqual(r['counts'],{'skipped-missing-prerequisite':1})
    def test_rsp_conflict_preserves_existing_bytes(self):
        p=self.plan();data='--flags REVIEWED=true';p['entries'][0]['rsp']={'path':'args.rsp','content':data,'sha256':hashlib.sha256(data.encode()).hexdigest()};(self.out/'args.rsp').write_text('user-owned conflict')
        r=g.run_plan(p,report_path=self.root/'report.json')
        self.assertEqual(r['counts'],{'skipped-rsp-conflict':1});self.assertEqual((self.out/'args.rsp').read_text(),'user-owned conflict')
    def test_dependency_order_retries_without_running_native_targets(self):
        p=self.plan();e=p['entries'][0];e['inputs']=['gen/prerequisite.h']
        second=self.view/'second.py';second.write_text('from pathlib import Path\nPath("gen/prerequisite.h").write_text("ready")\n')
        config={'audited_action_scripts':{'entry.py':self.hash,'second.py':g.digest(second)}};(self.support/'config/build-prerequisites.json').write_text(json.dumps(config))
        p['entries'].append({**e,'id':1,'script':'second.py','script_sha256':g.digest(second),'inputs':[],'outputs':['gen/prerequisite.h'],'command':[sys.executable,'../../second.py']})
        r=g.run_plan(p,report_path=self.root/'report.json');self.assertEqual(r['counts'],{'passed':2});self.assertEqual([x['id'] for x in r['results']],[1,0])

    def test_only_verified_virtual_nodes_ignore_missing_file(self):
        plan=self.plan();entry=plan['entries'][0]
        entry['inputs']=['phony/metadata','gen/real-input.h']
        entry['virtual_inputs']={'phony/metadata':'phony'}
        self.assertEqual(g.prerequisite_files(entry,self.out),['gen/real-input.h'])
        entry['virtual_inputs']={}
        self.assertEqual(g.prerequisite_files(entry,self.out),['phony/metadata','gen/real-input.h'])
    def test_resume_reuses_only_matching_existing_output_receipts(self):
        plan=self.plan();prior=self.root/'prior.json';g.run_plan(plan,report_path=prior)
        modified=(self.out/'gen/result.h').stat().st_mtime_ns
        report=g.run_plan(plan,report_path=self.root/'retry.json',resume=prior)
        self.assertEqual(report['counts'],{'reused-passed':1})
        self.assertEqual((self.out/'gen/result.h').stat().st_mtime_ns,modified)
        (self.out/'gen/result.h').write_text('changed')
        report=g.run_plan(plan,report_path=self.root/'retry2.json',resume=prior)
        self.assertEqual(report['counts'],{'passed':1})
    def test_timeout_is_failure_and_no_success_receipt(self):
        self.script.write_text('import time\ntime.sleep(5)\n');self.hash=g.digest(self.script)
        (self.support/'config/build-prerequisites.json').write_text(json.dumps({'audited_action_scripts':{'entry.py':self.hash}}))
        report=g.run_plan(self.plan(),timeout=0.1,report_path=self.root/'timeout.json')
        self.assertEqual(report['counts'],{'timeout':1})
        self.assertEqual(report['results'][0]['output_sha256'],{})
    def test_native_cache_changes_are_reported(self):
        native=self.out/'existing.o';native.write_bytes(b'existing')
        self.script.write_text('from pathlib import Path\nPath("existing.o").write_bytes(b"changed")\nPath("gen/result.h").write_text("header")\n');self.hash=g.digest(self.script)
        (self.support/'config/build-prerequisites.json').write_text(json.dumps({'audited_action_scripts':{'entry.py':self.hash}}))
        report=g.run_plan(self.plan(),report_path=self.root/'guard.json')
        self.assertFalse(report['native_artifacts_unchanged'])
        self.assertEqual(report['native_artifacts']['changed_or_removed'],['existing.o'])

    def test_timeout_kills_descendant_workers(self):
        import time
        child='import time;from pathlib import Path;time.sleep(0.7);Path("late-child-output").write_text("late")'
        self.script.write_text('import subprocess,sys,time\nsubprocess.Popen([sys.executable,"-c",'+repr(child)+'])\ntime.sleep(10)\n')
        self.hash=g.digest(self.script)
        (self.support/'config/build-prerequisites.json').write_text(json.dumps({'audited_action_scripts':{'entry.py':self.hash}}))
        report=g.run_plan(self.plan(),timeout=0.2,report_path=self.root/'descendant.json')
        self.assertEqual(report['counts'],{'timeout':1});time.sleep(0.8)
        self.assertFalse((self.out/'late-child-output').exists())
    def test_resume_keeps_prior_failure_logs(self):
        p=self.plan();self.script.write_text('raise SystemExit(1)\n');self.hash=g.digest(self.script)
        (self.support/'config/build-prerequisites.json').write_text(json.dumps({'audited_action_scripts':{'entry.py':self.hash}}))
        prior=self.root/'prior-failure.json';r=g.run_plan(self.plan(),report_path=prior)
        log=Path(r['results'][0]['log']);before=log.read_bytes()
        second=g.run_plan(self.plan(),report_path=self.root/'another.json',resume=prior)
        self.assertNotEqual(second['results'][0]['log'],str(log));self.assertEqual(log.read_bytes(),before)

    def test_replayed_identical_header_preserves_original_mtime(self):
        import os
        target=self.out/'gen/result.h';target.parent.mkdir();target.write_text('generated')
        original_ns=1600000000123456789;os.utime(target,ns=(original_ns-100,original_ns))
        old_mode=target.stat().st_mode
        report=g.run_plan(self.plan(),report_path=self.root/'restat.json')
        self.assertEqual(target.stat().st_mtime_ns,original_ns)
        self.assertEqual(target.stat().st_mode,old_mode)
        entry=report['results'][0];self.assertEqual(entry['restat']['restored_mtime'],['gen/result.h'])
        self.assertEqual(entry['outputs_before']['gen/result.h']['mtime_ns'],original_ns)
    def test_changed_and_new_output_keep_their_actual_times(self):
        import os
        target=self.out/'gen/result.h';target.parent.mkdir();target.write_text('old')
        os.utime(target,ns=(1600000000000000000,1600000000000000000))
        new_ns=1800000000123456789
        self.script.write_text('from pathlib import Path\nimport os\nfor name in ["gen/result.h","gen/new.h"]:\n Path(name).write_text("changed")\n os.utime(name,ns=('+str(new_ns)+','+str(new_ns)+'))\n')
        self.hash=g.digest(self.script)
        (self.support/'config/build-prerequisites.json').write_text(json.dumps({'audited_action_scripts':{'entry.py':self.hash}}))
        report=g.run_plan(self.plan(['gen/result.h','gen/new.h']),report_path=self.root/'changed.json')
        self.assertEqual(target.stat().st_mtime_ns,new_ns)
        self.assertEqual((self.out/'gen/new.h').stat().st_mtime_ns,new_ns)
        self.assertEqual(report['results'][0]['restat']['restored_mtime'],[])
        self.assertEqual(report['results'][0]['restat']['retained_current_mtime'],{'gen/result.h':'changed-bytes','gen/new.h':'new-output'})
    def test_shared_output_detaches_without_changing_other_alias(self):
        import os
        original=self.view/'pristine.h';original.write_text('pristine')
        os.utime(original,ns=(1600000000000000000,1600000000000000000))
        target=self.out/'gen/result.h';target.parent.mkdir();os.link(original,target)
        original_before=original.stat()
        report=g.run_plan(self.plan(),report_path=self.root/'shared.json')
        self.assertEqual(report['counts'],{'passed':1})
        self.assertTrue(report['results'][0]['outputs_before']['gen/result.h']['detached_shared_output'])
        self.assertEqual(original.read_text(),'pristine');self.assertEqual(target.read_text(),'generated')
        self.assertEqual(original.stat().st_ino,original_before.st_ino)
        self.assertEqual(original.stat().st_mtime_ns,original_before.st_mtime_ns)
        self.assertEqual(original.stat().st_mode,original_before.st_mode)
        self.assertNotEqual(target.stat().st_ino,original.stat().st_ino)
        self.assertEqual(target.stat().st_nlink,1)
