import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from orch.contracts import Rejected, digest
from orch.github_preview import prepare_pull_request
from orch.github_reconcile import reconcile_pull_request
from test_github_preview import intent


def candidate(source):
    root = 'https://api.github.com/repos/' + source['repository']
    result = {'id': 456, 'number': 7, 'url': root + '/pulls/7',
              'html_url': 'https://github.com/' + source['repository'] + '/pull/7',
              'title': source['title'], 'body': source['body'], 'state': 'open', 'draft': True, 'merged_at': None}
    for kind in ('head', 'base'):
        result[kind] = {'ref': source[kind], 'sha': source['expected_' + kind + '_sha'],
                        'repo': {'id': 123, 'full_name': source['repository'], 'url': root}}
    return result


def seen(source, rows):
    return {'preview_sha256': prepare_pull_request(source, source['repository'])['sha256'],
            'pages': [{'page': 1, 'status': 200, 'has_next': False, 'body': rows}]}


class ReconciliationTests(unittest.TestCase):
    def assess(self, observations):
        return reconcile_pull_request(intent(), 'example/project', 123, observations)

    def test_one_match_is_an_observation_never_a_success_or_retry(self):
        source = intent()
        observations = seen(source, [candidate(source)])
        before = copy.deepcopy(observations)
        report = self.assess(observations)
        self.assertEqual(observations, before)
        self.assertEqual(report['status'], 'candidate_observed')
        self.assertEqual(report['candidate']['number'], 7)
        self.assertEqual(report['sha256'], digest(report['binding']))
        for key in ('retry_allowed', 'remote_effect_confirmed', 'live_authorized'):
            self.assertIs(report[key], False)
        query = parse_qs(urlsplit(report['read_request']['url']).query)
        self.assertEqual(query['state'], ['all'])
        self.assertNotIn('base', query)  # A changed base must remain discoverable.
        self.assertEqual(query['head'], ['example:' + source['head']])

    def test_empty_duplicate_conflicting_and_incomplete_never_retry(self):
        source = intent()
        for rows, reason in (([], 'no_matching_candidate'), ([candidate(source)] * 2, 'multiple_candidates'), ([None], 'candidate_malformed')):
            report = self.assess(seen(source, rows))
            self.assertIn(reason, report['reasons'])
            self.assertFalse(report['retry_allowed'])
            self.assertIsNone(report['candidate'])
        observations = seen(source, [candidate(source)])
        observations['pages'][0]['has_next'] = True
        self.assertIn('pagination_incomplete_or_inconsistent', self.assess(observations)['reasons'])
        observations['pages'].append({'page': 2, 'status': 200, 'has_next': False, 'body': []})
        self.assertEqual(self.assess(observations)['status'], 'candidate_observed')
        observations['pages'][1]['page'] = 3
        self.assertEqual(self.assess(observations)['status'], 'unresolved')
        for status in (301, 401, 404, 422, 500):
            observations = seen(source, [candidate(source)])
            observations['pages'][0]['status'] = status
            self.assertIn('page_unavailable', self.assess(observations)['reasons'])
        observations = seen(source, [candidate(source)])
        observations['preview_sha256'] = 'f' * 64
        self.assertIn('preview_mismatch', self.assess(observations)['reasons'])

    def test_changed_pr_fields_and_cross_repo_refs_remain_unresolved(self):
        source = intent()
        changes = [('id', True), ('number', -1), ('url', 'https://evil.example'), ('html_url', 'https://evil.example'),
                   ('title', 'other'), ('body', 'other'), ('draft', False), ('state', 'closed'), ('merged_at', 'time')]
        for key, value in changes:
            pr = candidate(source)
            pr[key] = value
            with self.subTest(key=key):
                self.assertIn('candidate_scope_or_state_mismatch', self.assess(seen(source, [pr]))['reasons'])
        for kind in ('base', 'head'):
            for key, value in [('ref', 'other'), ('sha', 'f' * 40), ('repo', None), ('repo', {'id': 999, 'full_name': source['repository']})]:
                pr = candidate(source)
                pr[kind][key] = value
                self.assertEqual(self.assess(seen(source, [candidate(source), pr]))['status'], 'unresolved')

    def test_invalid_envelopes_and_budgets_reject(self):
        source = intent()
        for observations in ({}, {'preview_sha256': 'x', 'pages': [None]}, {'preview_sha256': 'x', 'pages': [{}] * 11}):
            with self.assertRaises(Rejected):
                self.assess(observations)
        observations = seen(source, [candidate(source)])
        observations['pages'][0]['body'][0]['extra'] = 'x' * 65537
        with self.assertRaises(Rejected):
            self.assess(observations)
        with self.assertRaises(Rejected):
            reconcile_pull_request(source, source['repository'], True, seen(source, []))

    def test_cli_has_no_effects_and_reports_uncertainty(self):
        from orch.__main__ import main
        with tempfile.TemporaryDirectory() as root:
            source = intent()
            proposal, observation_file = Path(root) / 'intent.json', Path(root) / 'observations.json'
            proposal.write_text(json.dumps(source))
            for rows, expected in (([], 2), ([candidate(source)], 0)):
                observation_file.write_text(json.dumps(seen(source, rows)))
                output = io.StringIO()
                argv = ['orch', 'github-reconcile', '--intent', str(proposal), '--allow-repository', source['repository'], '--repository-id', '123', '--observations', str(observation_file)]
                with patch.object(sys, 'argv', argv), redirect_stdout(output), patch('socket.socket', side_effect=AssertionError('No network')), patch('subprocess.Popen', side_effect=AssertionError('No process')), patch('orch.__main__.Engine', side_effect=AssertionError('No store')):
                    with self.assertRaises(SystemExit) as result:
                        main()
                self.assertEqual(result.exception.code, expected)
                self.assertFalse(json.loads(output.getvalue())['retry_allowed'])
            self.assertEqual(len(list(Path(root).iterdir())), 2)
