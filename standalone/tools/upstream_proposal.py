#!/usr/bin/env python3
"""Prepare an upstream compatibility proposal; submit a draft PR only on request.

The proposal is an inactive manifest, not an imported source tree or a successful
compatibility check. No active lock, original Blink file or release is changed.
GitHub writes require --submit and an authenticated GitHub CLI.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.parse

import automation

PROPOSAL_PATH = 'standalone/config/upstream-update.json'
OUTPUT = automation.ROOT / 'standalone/out/automation/upstream-proposal.json'


def prepare(candidate, root=automation.ROOT):
    """Recheck the discovery input against this checkout before planning writes."""
    current = automation.identity(root)
    if candidate['current'] != current:
        raise ValueError('Discovery belongs to a different source/configuration pin')
    target = candidate['candidate']
    version, revision = target['version'], target['revision']
    if (not automation.VERSION_NUMBER.fullmatch(version) or
            not automation.REVISION.fullmatch(revision) or
            not re.fullmatch(r'[0-9a-f]{64}', target['version_sha256'])):
        raise ValueError('Invalid candidate identity')
    config = automation.read_json(root / automation.CONFIG)
    if (candidate['channel'] != config['upstream_channel'] or
            candidate['platform'] != config['upstream_platform']):
        raise ValueError('Discovery does not match configured tracking channel')
    # Treat external JSON as data. Do not trust its eligibility, links, prose or
    # timestamps; reconstruct the proposal from validated numeric/hash fields.
    key = hashlib.sha256((version + '\n' + revision).encode()).hexdigest()
    if candidate['candidate_key'] != key:
        raise ValueError('Candidate key does not match immutable identity')
    old = tuple(map(int, current['upstream_version'].split('.')))
    new = tuple(map(int, version.split('.')))
    status = ('current' if revision == current['upstream_revision'] else
              'older-than-current' if new < old else
              'same-version-different-revision' if new == old else 'newer-version')
    eligible = status == 'newer-version'
    manifest = {'schema': 1, 'status': 'awaiting-source-import',
                'candidate_key': key, 'channel': candidate['channel'],
                'platform': candidate['platform'], 'current': current,
                'candidate': {name: target[name] for name in ('version', 'revision', 'version_sha256')},
                'source_update_performed': False, 'compatibility_verified': False}
    title = f'Propose Blink upstream {version}'
    body = f'''Track the official {candidate['channel']} Chromium/Blink candidate **{version}**
at `{revision}`. Current engine pin: **{current['upstream_version']}**
at `{current['upstream_revision']}`.

This draft changes only an inactive proposal manifest. Source import,
dependency refresh, engine compatibility and release readiness are pending.
Do not merge this manifest alone as an upstream update.

- [ ] Import the full Blink history and commit-ID mapping, retaining legacy history.
- [ ] Refresh pristine source inventory, upstream README and license bytes at this SHA.
- [ ] Refresh pinned dependencies, implicit tools/headers and reproducible adaptations.
- [ ] Pass pristine verification, support tests and engine-only graph/input checks.
- [ ] Compile the engine and pass native rendering, JS, input/IME, navigation,
      child-frame, storage and worker checks on the exact candidate.
- [ ] Exercise the embedding ABI with wave-browser; review any API compatibility changes.
- [ ] Remove the inactive proposal manifest when completing the real source update.

No automatic merge, tag, SDK publication or browser application change is performed.
Full builds remain restricted to the trusted main workflow; use an isolated build
environment for candidate testing before review and merge.

[Official Chromium source]({automation.OFFICIAL_SOURCE}/+/{revision})
'''
    return {'schema': 1, 'eligible': eligible, 'status': status,
            'branch': f'auto/upstream/{version}-{revision[:12]}',
            'path': PROPOSAL_PATH, 'manifest': manifest, 'title': title, 'body': body}


def gh_api(repository, method, endpoint, payload=None):
    command = ['gh', 'api', '--method', method, f'repos/{repository}/{endpoint}']
    if payload is not None:
        command += ['--input', '-']
    result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                            text=True, stdout=subprocess.PIPE, check=True, timeout=90)
    return json.loads(result.stdout)


def submit(plan, repository, expected_base, api=gh_api):
    """Create exactly one immutable proposal branch/PR, recovering partial runs.

    Never force-push or update an existing branch. Even closed proposals count
    as handled, so a rejected version is not reopened by the daily schedule.
    """
    if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) or
            not automation.REVISION.fullmatch(expected_base)):
        raise ValueError('Expected GitHub owner/repository and full checkout commit')
    if not plan['eligible']:
        return {'submitted': False, 'reason': plan['status']}
    owner = repository.split('/')[0]
    branch = plan['branch']
    query = urllib.parse.urlencode({'state': 'all', 'head': owner + ':' + branch,
                                    'base': 'main', 'per_page': 100})
    pulls = api(repository, 'GET', 'pulls?' + query)
    if pulls:
        return {'submitted': False, 'reason': 'existing-proposal',
                'url': pulls[0]['html_url'], 'state': pulls[0]['state']}
    base = api(repository, 'GET', 'git/ref/heads/main')['object']['sha']
    if base != expected_base:
        return {'submitted': False, 'reason': 'main-moved-retry-next-run'}
    refs = api(repository, 'GET', 'git/matching-refs/heads/' + branch)
    exact = [ref for ref in refs if ref['ref'] == 'refs/heads/' + branch]
    content = json.dumps(plan['manifest'], indent=2, sort_keys=True) + '\n'
    if exact:
        # A previous run may have created the branch but failed to open the PR.
        # Only recover the exact single-file commit this tool would have made.
        head = api(repository, 'GET', 'git/commits/' + exact[0]['object']['sha'])
        if [parent['sha'] for parent in head['parents']] != [base]:
            raise ValueError('Existing proposal branch has a different base; refusing to overwrite')
        comparison = api(repository, 'GET', 'compare/' + base + '...' + exact[0]['object']['sha'])
        files = comparison.get('files', [])
        if len(files) != 1 or files[0]['filename'] != PROPOSAL_PATH:
            raise ValueError('Existing proposal branch contains unrelated changes')
        file = api(repository, 'GET', 'contents/' + PROPOSAL_PATH + '?ref=' + urllib.parse.quote(branch, safe=''))
        if base64.b64decode(file['content']).decode() != content:
            raise ValueError('Existing proposal branch contains a different proposal')
    else:
        base_commit = api(repository, 'GET', 'git/commits/' + base)
        blob = api(repository, 'POST', 'git/blobs', {'content': content, 'encoding': 'utf-8'})
        tree = api(repository, 'POST', 'git/trees', {'base_tree': base_commit['tree']['sha'],
                   'tree': [{'path': PROPOSAL_PATH, 'mode': '100644', 'type': 'blob', 'sha': blob['sha']}]})
        commit = api(repository, 'POST', 'git/commits', {'message': plan['title'],
                     'tree': tree['sha'], 'parents': [base]})
        api(repository, 'POST', 'git/refs', {'ref': 'refs/heads/' + branch, 'sha': commit['sha']})
    pull = api(repository, 'POST', 'pulls', {'title': plan['title'], 'body': plan['body'],
               'head': branch, 'base': 'main', 'draft': True, 'maintainer_can_modify': True})
    return {'submitted': True, 'url': pull['html_url'], 'state': pull['state']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=OUTPUT)
    parser.add_argument('--github-output', action='store_true')
    parser.add_argument('--submit', action='store_true')
    parser.add_argument('--repository', default=os.environ.get('GITHUB_REPOSITORY', ''))
    args = parser.parse_args(argv)
    plan = prepare(automation.read_json(args.candidate))
    if args.submit:
        head = subprocess.check_output(['git', '-C', str(automation.ROOT), 'rev-parse', 'HEAD'], text=True).strip()
        plan['submission'] = submit(plan, args.repository, head)
    automation.write_json(args.output, plan)
    if args.github_output:
        with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
            output.write('eligible=' + str(plan['eligible']).lower() + '\n')
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with Path(summary).open('a') as output:
            output.write(f"### Official Blink upstream: {plan['status']}\n\n")
            output.write(plan['body'])
            if 'submission' in plan:
                output.write('\nSubmission: ' + json.dumps(plan['submission']) + '\n')
    print(json.dumps(plan, indent=2))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print('error:', error, file=sys.stderr)
        sys.exit(2)
