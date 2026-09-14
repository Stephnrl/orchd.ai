import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from orch.contracts import Rejected, digest
from orch.github_preview import prepare_pull_request
from orch.github_refs import assess_refs
from test_github_preview import intent


def observations(source):
    root = 'https://api.github.com/repos/' + source['repository']
    result = {'preview_sha256': prepare_pull_request(source, source['repository'])['sha256'],
              'repository': {'status': 200, 'body': {'id': 123, 'full_name': source['repository'],
                              'url': root, 'archived': False, 'disabled': False}}}
    for key in ('base', 'head'):
        sha = source['expected_' + key + '_sha']
        result[key] = {'status': 200, 'body': {'ref': 'refs/heads/' + source[key],
                      'url': root + '/git/refs/heads/' + source[key],
                      'object': {'type': 'commit', 'sha': sha, 'url': root + '/git/commits/' + sha}}}
    return result


class GitHubRefTests(unittest.TestCase):
    def test_matching_observations_are_bound_but_never_authorized(self):
        source = intent()
        seen = observations(source)
        original = copy.deepcopy(seen)
        report = assess_refs(source, source['repository'], 123, seen)
        self.assertEqual(seen, original)
        self.assertEqual(report['status'], 'matches')
        self.assertEqual(report['blockers'], [])
        self.assertFalse(report['live_authorized'])
        self.assertFalse(report['remote_refs_verified'])
        self.assertEqual(report['sha256'], digest(report['binding']))
        self.assertEqual(report['binding']['observations_sha256'], digest(seen))
        self.assertTrue(all(r['method'] == 'GET' for r in report['read_requests'].values()))
        self.assertEqual(report['read_requests']['head']['url'], 'https://api.github.com/repos/example/project/git/ref/heads/' + source['head'])

    def test_repository_and_ref_drift_block(self):
        source = intent()
        for key, value in [('id', 999), ('id', True), ('full_name', 'elsewhere/project'), ('url', 'https://evil.example'), ('archived', True), ('disabled', True), ('archived', 'false')]:
            seen = observations(source)
            seen['repository']['body'][key] = value
            with self.subTest(key=key):
                self.assertEqual(assess_refs(source, source['repository'], 123, seen)['status'], 'blocked')
        for kind in ('base', 'head'):
            for field, value in [('ref', 'refs/tags/main'), ('url', 'https://evil.example'), ('sha', 'f' * 40), ('type', 'tag'), ('object_url', 'https://evil.example')]:
                seen = observations(source)
                body = seen[kind]['body']
                if field in ('sha', 'type', 'object_url'):
                    body['object']['url' if field == 'object_url' else field] = value
                else:
                    body[field] = value
                with self.subTest(kind=kind, field=field):
                    self.assertIn(kind + '_ref_mismatch', assess_refs(source, source['repository'], 123, seen)['blockers'])

    def test_unavailable_incomplete_and_wrong_preview_block(self):
        source = intent()
        for kind in ('repository', 'base', 'head'):
            for code in (301, 401, 403, 404, 409, 500):
                seen = observations(source)
                seen[kind] = {'status': code, 'body': {'error': 'untrusted data'}}
                self.assertIn(kind + '_unavailable', assess_refs(source, source['repository'], 123, seen)['blockers'])
            for body in ({}, [], None):
                seen = observations(source)
                seen[kind]['body'] = body
                self.assertEqual(assess_refs(source, source['repository'], 123, seen)['status'], 'blocked')
        seen = observations(source)
        seen['preview_sha256'] = 'f' * 64
        self.assertIn('preview_mismatch', assess_refs(source, source['repository'], 123, seen)['blockers'])
        self.assertIn('preview_mismatch', assess_refs({**source, 'title': 'Changed scope'}, source['repository'], 123, observations(source))['blockers'])
        for value in (True, 0, -1, '123', 9007199254740992):
            with self.assertRaises(Rejected):
                assess_refs(source, source['repository'], value, observations(source))
        with self.assertRaises(Rejected):
            assess_refs(source, source['repository'], 123, {})
        for response in ({'status': '200', 'body': {}}, {'status': 200}, {'status': 200, 'body': {}, 'redirect': True}):
            seen = observations(source)
            seen['base'] = response
            with self.assertRaises(Rejected):
                assess_refs(source, source['repository'], 123, seen)
        seen = observations(source)
        seen['repository']['body']['extra'] = 'x' * 65537
        with self.assertRaises(Rejected):
            assess_refs(source, source['repository'], 123, seen)

    def test_cli_is_offline_and_exit_codes_distinguish_match(self):
        from orch.__main__ import main
        with tempfile.TemporaryDirectory() as root:
            source = intent()
            intent_file, responses = Path(root) / 'intent.json', Path(root) / 'seen.json'
            intent_file.write_text(json.dumps(source))
            for matches in (True, False):
                seen = observations(source)
                if not matches:
                    seen['head']['status'] = 404
                responses.write_text(json.dumps(seen))
                output = io.StringIO()
                argv = ['orch', 'github-check-refs', '--intent', str(intent_file), '--allow-repository', source['repository'], '--repository-id', '123', '--observations', str(responses)]
                with patch.object(sys, 'argv', argv), redirect_stdout(output), patch('socket.socket', side_effect=AssertionError('No network')), patch('subprocess.Popen', side_effect=AssertionError('No process')), patch('orch.__main__.Engine', side_effect=AssertionError('No store')):
                    with self.assertRaises(SystemExit) as exit_code:
                        main()
                self.assertEqual(exit_code.exception.code, 0 if matches else 2)
                self.assertFalse(json.loads(output.getvalue())['remote_refs_verified'])
            self.assertEqual(len(list(Path(root).iterdir())), 2)
