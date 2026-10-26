import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1] / 'tools'
sys.path.insert(0, str(TOOLS))
try:
    SPEC = importlib.util.spec_from_file_location('blink_upstream_proposal', TOOLS / 'upstream_proposal.py')
    proposal = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(proposal)
finally:
    sys.path.pop(0)


class ProposalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        config = self.root / 'standalone/config'
        config.mkdir(parents=True)
        (self.root / 'standalone/gn').mkdir()
        version = b'MAJOR=158\nMINOR=0\nBUILD=8114\nPATCH=0\n'
        (self.root / proposal.automation.VERSION).write_bytes(version)
        (config / 'release.json').write_text(json.dumps({
            'product': 'blink-engine', 'abi_version': 1, 'package_revision': 1,
            'release_track': 'development', 'upstream_channel': 'stable',
            'upstream_platform': 'linux', 'supported_profile': 'linux-x64'}))
        (config / 'upstream.lock.json').write_text(json.dumps({'revision': 'a' * 40}))
        (config / 'engine-adaptations.json').write_text(json.dumps({
            'chromium_revision': 'a' * 40,
            'version_metadata': {'original_sha256': hashlib.sha256(version).hexdigest()}}))
        self.candidate = self.discovery()
        self.base = 'b' * 40
        self.calls = []
        self.responses = []

    def discovery(self, version='159.0.8200.1', revision='d' * 40):
        fields = ('MAJOR', 'MINOR', 'BUILD', 'PATCH')
        raw = ''.join(f'{key}={value}\n' for key, value in zip(fields, version.split('.'))).encode()
        responses = iter([{'versions': [{'version': version}]}, {'commit': revision}])
        return proposal.automation.discover(self.root, lambda _: next(responses), lambda _: base64.b64encode(raw))

    def plan(self):
        return proposal.prepare(self.candidate, self.root)

    def api(self, repository, method, endpoint, payload=None):
        self.calls.append((repository, method, endpoint, payload))
        return self.responses.pop(0)

    def submit(self, plan=None):
        return proposal.submit(plan or self.plan(), 'wavefnd-labs/blink', self.base, self.api)

    def initial_responses(self):
        return [[], {'object': {'sha': self.base}}, []]

    def test_plan_is_deterministic_and_does_not_modify_source_pins(self):
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        first = self.plan()
        self.candidate['checked_at'] = 'another day'
        self.candidate['sources']['version_file'] = 'https://invalid.example/untrusted'
        self.candidate['candidate']['injected'] = 'do not retain arbitrary external fields'
        self.assertEqual(first, self.plan())
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        self.assertTrue(first['eligible'])
        self.assertEqual(first['manifest']['status'], 'awaiting-source-import')
        self.assertFalse(first['manifest']['source_update_performed'])
        self.assertFalse(first['manifest']['compatibility_verified'])
        self.assertEqual(first['path'], 'standalone/config/upstream-update.json')

    def test_older_stable_cannot_downgrade_development_engine(self):
        self.candidate = self.discovery('157.0.8000.1')
        self.assertFalse(self.candidate['automatic_proposal_eligible'])
        self.assertEqual(self.candidate['update_status'], 'older-than-current')
        self.assertEqual(self.submit()['reason'], 'older-than-current')
        self.assertEqual(self.calls, [])

    def test_same_version_retag_requires_manual_review_even_if_input_claims_eligible(self):
        self.candidate = self.discovery('158.0.8114.0')
        self.candidate['automatic_proposal_eligible'] = True
        self.assertEqual(self.plan()['status'], 'same-version-different-revision')
        self.assertFalse(self.plan()['eligible'])

    def test_current_revision_has_no_proposal(self):
        self.candidate = self.discovery('158.0.8114.0', 'a' * 40)
        self.assertEqual(self.plan()['status'], 'current')
        self.assertFalse(self.plan()['eligible'])

    def test_stale_identity_channel_or_hash_is_rejected(self):
        for mutate in (lambda c: c['current'].update(upstream_revision='e' * 40),
                       lambda c: c.update(channel='canary'),
                       lambda c: c.update(candidate_key='f' * 64),
                       lambda c: c['candidate'].update(version='159.0.8200.1\nmalicious')):
            with self.subTest(mutation=mutate):
                candidate = copy.deepcopy(self.candidate)
                mutate(candidate)
                with self.assertRaises(ValueError):
                    proposal.prepare(candidate, self.root)

    def test_created_pr_is_draft_and_only_proposal_file_changes(self):
        self.responses = [*self.initial_responses(), {'tree': {'sha': 'tree-base'}},
                          {'sha': 'blob'}, {'sha': 'tree'}, {'sha': 'commit'}, {},
                          {'html_url': 'https://github.com/wavefnd-labs/blink/pull/1', 'state': 'open'}]
        result = self.submit()
        self.assertTrue(result['submitted'])
        writes = [(endpoint, payload) for _, method, endpoint, payload in self.calls if method != 'GET']
        self.assertEqual([e for e, _ in writes], ['git/blobs', 'git/trees', 'git/commits', 'git/refs', 'pulls'])
        tree = writes[1][1]
        self.assertEqual(tree['base_tree'], 'tree-base')
        self.assertEqual([entry['path'] for entry in tree['tree']], [proposal.PROPOSAL_PATH])
        self.assertEqual(writes[2][1]['parents'], [self.base])
        self.assertTrue(writes[-1][1]['draft'])
        self.assertEqual(writes[-1][1]['base'], 'main')
        self.assertNotIn('force', str(writes))
        self.assertEqual(self.responses, [])

    def test_closed_and_open_prs_are_not_duplicated_or_reopened(self):
        for state in ('open', 'closed'):
            with self.subTest(state=state):
                self.calls = []
                self.responses = [[{'html_url': 'https://github.com/wavefnd-labs/blink/pull/1', 'state': state}]]
                self.assertEqual(self.submit()['reason'], 'existing-proposal')
                self.assertEqual(len(self.calls), 1)

    def test_main_movement_causes_no_writes(self):
        self.responses = [[], {'object': {'sha': 'c' * 40}}]
        self.assertEqual(self.submit()['reason'], 'main-moved-retry-next-run')
        self.assertTrue(all(call[1] == 'GET' for call in self.calls))

    def test_partial_run_recovers_only_exact_proposal_without_force_push(self):
        plan = self.plan()
        content = json.dumps(plan['manifest'], indent=2, sort_keys=True) + '\n'
        self.responses = [[], {'object': {'sha': self.base}},
                          [{'ref': 'refs/heads/' + plan['branch'], 'object': {'sha': 'existing'}}],
                          {'parents': [{'sha': self.base}]}, {'files': [{'filename': proposal.PROPOSAL_PATH}]},
                          {'content': base64.b64encode(content.encode()).decode()},
                          {'html_url': 'https://github.com/wavefnd-labs/blink/pull/1', 'state': 'open'}]
        self.assertTrue(self.submit(plan)['submitted'])
        self.assertEqual([c[2] for c in self.calls if c[1] != 'GET'], ['pulls'])

    def test_existing_branch_with_other_changes_is_never_overwritten(self):
        plan = self.plan()
        self.responses = [[], {'object': {'sha': self.base}},
                          [{'ref': 'refs/heads/' + plan['branch'], 'object': {'sha': 'existing'}}],
                          {'parents': [{'sha': self.base}]}, {'files': [{'filename': 'renderer/core/example.cc'}]}]
        with self.assertRaisesRegex(ValueError, 'unrelated changes'):
            self.submit(plan)
        self.assertTrue(all(c[1] == 'GET' for c in self.calls))


if __name__ == '__main__':
    unittest.main()
