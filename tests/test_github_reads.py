import base64
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from orch.contracts import Rejected, canonical, digest
from orch.github_journal import GitHubJournal
from orch.github_reads import prepare_read_plan, decode_transcript, snapshot_from_transcript
from test_github_preview import intent
from test_github_refs import observations


def transcript_for(plan, seen=None):
    seen = observations(intent()) if seen is None else seen
    responses = {}
    for role, request in plan['binding']['requests'].items():
        raw = json.dumps(seen[role]['body']).encode()
        responses[role] = {'request': copy.deepcopy(request), 'error': None,
                           'response': {'url': request['url'], 'redirected': False, 'status': seen[role]['status'],
                                        'headers': [['Content-Type', 'application/json; charset=utf-8'], ['Content-Length', str(len(raw))]],
                                        'body_base64': base64.b64encode(raw).decode()}}
    return {'kind': 'GitHubRefReadTranscript', 'schema_version': '1.0.0', 'plan_sha256': plan['sha256'], 'responses': responses}


class ReadTranscriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'journal.sqlite'
        self.journal = GitHubJournal(self.path)
        self.saved = self.journal.stage(intent(), 'example/project', 123)
        self.plan = prepare_read_plan(self.journal, self.saved['operation_id'], self.saved['sha256'])
        self.transcript = transcript_for(self.plan)

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def snapshot(self, transcript=None, journal=None):
        return snapshot_from_transcript(journal or self.journal, self.saved['operation_id'], self.saved['sha256'],
                                        self.transcript if transcript is None else transcript, '2026-01-01T00:01:00Z')

    def test_exact_read_plan_and_readonly_snapshot_without_execution(self):
        reader = GitHubJournal(self.path, read_only=True)
        try:
            with patch('socket.socket', side_effect=AssertionError('No network')), patch('subprocess.Popen', side_effect=AssertionError('No process')):
                result = self.snapshot(journal=reader)
            self.assertEqual(reader.db.total_changes, 0)
            self.assertFalse(reader.db.in_transaction)
        finally:
            reader.close()
        self.assertEqual(result['binding']['observations'], observations(intent()))
        self.assertFalse(result['remote_refs_verified'])
        self.assertFalse(result['live_authorized'])
        self.assertEqual(self.plan['sha256'], digest(self.plan['binding']))
        for request in self.plan['binding']['requests'].values():
            self.assertEqual(request['method'], 'GET')
            self.assertEqual(request['headers']['Accept-Encoding'], 'identity')
            self.assertNotIn('Authorization', request['headers'])
            self.assertTrue(request['url'].startswith('https://api.github.com/repos/example/project'))
        self.assertIn('/git/ref/heads/', self.plan['binding']['requests']['head']['url'])
        self.assertEqual(self.journal.get(self.saved['operation_id'])['state'], 'prepared')

    def test_scope_mismatch_reserved_state_and_final_recheck(self):
        with self.assertRaises(Rejected):
            prepare_read_plan(self.journal, self.saved['operation_id'], 'f' * 64)
        original = decode_transcript
        def reserve_then_decode(*args):
            self.journal.reserve(self.saved['operation_id'], self.saved['sha256'])
            return original(*args)
        with patch('orch.github_reads.decode_transcript', side_effect=reserve_then_decode), self.assertRaises(Rejected):
            self.snapshot()
        with self.assertRaises(Rejected):
            prepare_read_plan(self.journal, self.saved['operation_id'], self.saved['sha256'])
        self.assertFalse(self.journal.db.in_transaction)

    def test_closed_envelope_and_plan_binding(self):
        for key, value in (('plan_sha256', 'f' * 64), ('schema_version', '2.0.0'), ('kind', 'other'), ('extra', True)):
            transcript = copy.deepcopy(self.transcript)
            transcript[key] = value
            with self.subTest(key=key), self.assertRaises(Rejected):
                self.snapshot(transcript)
        for role in ('repository', 'base', 'head'):
            transcript = copy.deepcopy(self.transcript)
            del transcript['responses'][role]
            with self.assertRaises(Rejected):
                self.snapshot(transcript)

    def test_request_changes_credentials_redirects_and_foreign_origins_reject(self):
        for key, value in (('method', 'POST'), ('url', 'https://evil.example'), ('body', {}),
                           ('headers', {'Authorization': 'Bearer forbidden'})):
            transcript = copy.deepcopy(self.transcript)
            transcript['responses']['head']['request'][key] = value
            with self.subTest(key=key), self.assertRaises(Rejected):
                self.snapshot(transcript)
        for key, value in (('url', 'https://evil.example'), ('redirected', True), ('redirected', 0), ('status', True), ('status', 600)):
            transcript = copy.deepcopy(self.transcript)
            transcript['responses']['head']['response'][key] = value
            with self.subTest(key=key), self.assertRaises(Rejected):
                self.snapshot(transcript)

    def test_duplicate_encoded_malformed_and_oversized_headers_reject(self):
        for extra in ([['content-type', 'application/json']], [['Content-Encoding', 'gzip']],
                      [['Location', 'https://evil.example']], [['Bad\nName', 'value']], [['X-Value', 'line\r\nInjected']],
                      [['X-Large', 'x' * 8192]], [[str(n), 'value'] for n in range(33)]):
            transcript = copy.deepcopy(self.transcript)
            transcript['responses']['head']['response']['headers'].extend(extra)
            with self.subTest(extra=str(extra)[:45]), self.assertRaises(Rejected):
                self.snapshot(transcript)

    def test_success_body_is_bounded_strict_utf8_json(self):
        for raw in (b'{"id":1,"id":2}', b'{"x":NaN}', b'{"x":1e999}', b'\xff', b'\xff\xfe{\x00}',
                    b'{"x":"\\ud800"}', b'[' * 1100 + b']' * 1100, b'x' * 12289):
            transcript = copy.deepcopy(self.transcript)
            response = transcript['responses']['head']['response']
            response['body_base64'] = base64.b64encode(raw).decode()
            response['headers'][1][1] = str(len(raw))
            with self.subTest(raw=raw[:25]), self.assertRaises(Rejected):
                self.snapshot(transcript)

    def test_length_base64_and_media_type_reject(self):
        for field, value in (('body_base64', '!!'), ('body_base64', 'e30=\n'),
                             ('headers', [['Content-Type', 'text/html']]),
                             ('headers', [['Content-Type', 'application/json'], ['Content-Length', '1']])):
            transcript = copy.deepcopy(self.transcript)
            transcript['responses']['head']['response'][field] = value
            with self.subTest(field=field), self.assertRaises(Rejected):
                self.snapshot(transcript)

    def test_failures_are_unavailable_and_error_bodies_are_not_retained(self):
        for status in (301, 302, 401, 403, 404, 429, 500, 503):
            transcript = copy.deepcopy(self.transcript)
            response = transcript['responses']['head']['response']
            response.update(status=status, body_base64=base64.b64encode(b'private upstream failure').decode(), headers=[])
            result = self.snapshot(transcript)
            self.assertEqual(result['binding']['observations']['head'], {'status': status, 'body': None})
            self.assertNotIn('private upstream failure', json.dumps(result))
        for error in ('timeout', 'connection_failed', 'tls_failed'):
            transcript = copy.deepcopy(self.transcript)
            transcript['responses']['head'].update(error=error, response=None)
            self.assertEqual(self.snapshot(transcript)['binding']['observations']['head'], {'status': 0, 'body': None})
        for error in ('unknown', {}, True):
            transcript = copy.deepcopy(self.transcript)
            transcript['responses']['head']['error'] = error
            with self.assertRaises(Rejected):
                self.snapshot(transcript)

    def test_total_budget_and_invalid_timestamp_reject(self):
        transcript = copy.deepcopy(self.transcript)
        transcript['extra'] = 'x' * 65536
        with self.assertRaises(Rejected):
            self.snapshot(transcript)
        with self.assertRaises(Rejected):
            snapshot_from_transcript(self.journal, self.saved['operation_id'], self.saved['sha256'], self.transcript, 'not a time')

    def test_cli_plan_snapshot_and_rejection_without_partial_output(self):
        from orch.__main__ import main
        source = Path(self.temp.name) / 'transcript.json'
        source.write_bytes(canonical(self.transcript))
        common = ['--journal', str(self.path), '--operation-id', self.saved['operation_id'], '--expected-sha256', self.saved['sha256']]
        for command, extra, expected in [('github-ref-read-plan', [], self.plan),
                ('github-ref-transcript-snapshot', ['--transcript', str(source), '--observed-at', '2026-01-01T00:01:00Z'], self.snapshot())]:
            out = io.StringIO()
            with patch.object(sys, 'argv', ['orch', command, *common, *extra]), redirect_stdout(out):
                main()
            self.assertEqual(json.loads(out.getvalue()), expected)
            self.assertLessEqual(len(out.getvalue().encode()), 65536)
        source.write_text('{"kind":1,"kind":2}')
        out = io.StringIO()
        with patch.object(sys, 'argv', ['orch', 'github-ref-transcript-snapshot', *common, '--transcript', str(source), '--observed-at', '2026-01-01T00:01:00Z']), redirect_stdout(out), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            main()
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(out.getvalue(), '')
