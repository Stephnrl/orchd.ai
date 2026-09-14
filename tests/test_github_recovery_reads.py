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
from orch.github_recovery_reads import prepare_recovery_plan, reconcile_transcript, _page_request
from test_github_preview import intent
from test_github_reconcile import candidate


class RecoveryTranscriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'journal.sqlite'
        self.journal = GitHubJournal(self.path)
        self.saved = self.journal.stage(intent(), 'example/project', 123)
        self.saved = self.journal.reserve(self.saved['operation_id'], self.saved['sha256'])
        self.plan = prepare_recovery_plan(self.journal, self.saved['operation_id'], self.saved['sha256'])

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def exchange(self, page, body=None, links=None, status=200):
        request = _page_request(self.plan, page)
        raw = json.dumps([candidate(intent())] if body is None else body).encode()
        headers = [['Content-Type', 'application/json'], ['Content-Length', str(len(raw))]]
        if links is not None:
            headers.append(['Link', links])
        return {'request': request, 'error': None, 'response': {'url': request['url'], 'redirected': False,
                'status': status, 'headers': headers, 'body_base64': base64.b64encode(raw).decode()}}

    def link(self, page, relation='next', numeric=False):
        url = _page_request(self.plan, page)['url']
        if numeric:
            url = url.replace('/repos/example/project/pulls', '/repositories/123/pulls')
        return '<' + url + '>; rel="' + relation + '"'

    def transcript(self, *pages):
        return {'kind': 'GitHubRecoveryReadTranscript', 'schema_version': '1.0.0', 'plan_sha256': self.plan['sha256'],
                'pages': list(pages) or [self.exchange(1)]}

    def assess(self, transcript=None, journal=None):
        return reconcile_transcript(journal or self.journal, self.saved['operation_id'], self.saved['sha256'],
                                    self.transcript() if transcript is None else transcript)

    def test_complete_capture_is_readonly_and_never_success_or_retry_authority(self):
        reader = GitHubJournal(self.path, read_only=True)
        before = self.path.read_bytes()
        try:
            with patch('socket.socket', side_effect=AssertionError('No network')), patch('subprocess.Popen', side_effect=AssertionError('No process')):
                result = self.assess(journal=reader)
            self.assertEqual(reader.db.total_changes, 0)
            self.assertFalse(reader.db.in_transaction)
        finally:
            reader.close()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(result['status'], 'candidate_observed')
        self.assertEqual(result['binding']['transcript_sha256'], digest(self.transcript()))
        self.assertEqual(result['binding']['reconciliation']['binding']['reserved_at'], self.saved['reserved_at'])
        for flag in ('retry_allowed', 'remote_effect_confirmed', 'live_authorized'):
            self.assertFalse(result[flag])

    def test_sequential_pages_and_numeric_repository_links(self):
        capture = self.transcript(self.exchange(1, [], self.link(2, numeric=True) + ', ' + self.link(2, 'last')),
                                  self.exchange(2, links=self.link(1, 'prev') + ', ' + self.link(1, 'first')))
        result = self.assess(capture)
        self.assertEqual(result['status'], 'candidate_observed')
        self.assertEqual(result['binding']['pages_checked'], 2)

    def test_truncation_and_page_limit_remain_unresolved(self):
        captures = [self.transcript(self.exchange(1, links=self.link(2))),
                    self.transcript(*[self.exchange(n, [] if n != 1 else None, self.link(n + 1)) for n in range(1, 11)]),
                    self.transcript(self.exchange(1, [], self.link(2) + ', ' + self.link(3, 'last')), self.exchange(2))]
        for capture in captures:
            result = self.assess(capture)
            self.assertEqual(result['status'], 'unresolved')
            self.assertIn('pagination_incomplete_or_inconsistent', result['binding']['reconciliation']['binding']['assessment']['reasons'])
            self.assertFalse(result['retry_allowed'])

    def test_extra_pages_and_page_order_reject(self):
        for capture in (self.transcript(self.exchange(1), self.exchange(2)),
                        self.transcript(self.exchange(2)),
                        self.transcript(self.exchange(1, [], self.link(2)), self.exchange(1))):
            with self.assertRaises(Rejected):
                self.assess(capture)

    def test_changed_and_ambiguous_pagination_links_reject(self):
        valid = self.link(2)
        for link in (valid.replace('api.github.com', 'evil.example'), valid.replace('/example/project/', '/other/project/'),
                     valid.replace('state=all', 'state=open'), valid.replace('per_page=100', 'per_page=1'),
                     valid.replace('page=2', 'page=2&page=3'), valid.replace('>; rel', '#fragment>; rel'),
                     self.link(2, numeric=True).replace('/123/', '/999/'), valid + ', ' + valid,
                     self.link(3), self.link(1), self.link(2, 'prev'), self.link(2, 'first'),
                     self.link(2, 'last'), valid.replace('"next"', '"unknown"'), valid + '; title="extra"'):
            with self.subTest(link=link), self.assertRaises(Rejected):
                self.assess(self.transcript(self.exchange(1, links=link)))

    def test_empty_duplicate_changed_and_closed_candidates_remain_unresolved(self):
        changed = candidate(intent()); changed['head']['sha'] = 'f' * 40
        closed = candidate(intent()); closed['state'] = 'closed'
        for rows in ([], [candidate(intent()), candidate(intent())], [changed], [closed]):
            self.assertEqual(self.assess(self.transcript(self.exchange(1, rows)))['status'], 'unresolved')

    def test_unavailable_pages_and_transport_failures_never_match(self):
        for status in (302, 401, 403, 429, 503):
            capture = self.transcript(self.exchange(1, {'message':'private upstream text'}, status=status))
            result = self.assess(capture)
            self.assertEqual(result['status'], 'unresolved')
            self.assertNotIn('private upstream text', json.dumps(result))
        exchange = self.exchange(1); exchange.update(error='timeout', response=None)
        self.assertEqual(self.assess(self.transcript(exchange))['status'], 'unresolved')

    def test_response_and_request_boundaries_share_strict_decoder(self):
        for field, value in (('redirected', True), ('url', 'https://evil.example'), ('body_base64', '!!'),
                             ('headers', [['Content-Type','application/json'], ['content-type','application/json']]),
                             ('headers', [['Content-Type','text/html']])):
            exchange = self.exchange(1); exchange['response'][field] = value
            with self.assertRaises(Rejected): self.assess(self.transcript(exchange))
        exchange = self.exchange(1); exchange['request']['headers']['Authorization'] = 'Bearer forbidden'
        with self.assertRaises(Rejected): self.assess(self.transcript(exchange))

    def test_closed_shape_plan_digest_and_budgets(self):
        for field, value in (('plan_sha256', 'f' * 64), ('schema_version', '2'), ('extra', True), ('pages', []),
                             ('pages', [self.exchange(n) for n in range(1,12)]), ('extra', 'x' * 65536)):
            capture = self.transcript(); capture[field] = value
            with self.assertRaises(Rejected): self.assess(capture)

    def test_prepared_or_wrong_scope_cannot_plan_recovery(self):
        with self.assertRaises(Rejected): prepare_recovery_plan(self.journal, self.saved['operation_id'], 'f' * 64)
        with patch.object(self.journal, 'get', return_value={**self.saved, 'state':'prepared'}), self.assertRaises(Rejected):
            prepare_recovery_plan(self.journal, self.saved['operation_id'], self.saved['sha256'])
        self.assertFalse(self.journal.db.in_transaction)

    def test_final_schema_and_reservation_checks_reject_changed_journal(self):
        from orch.github_recovery_reads import decode_recovery_transcript
        for failure in ('schema', 'reservation'):
            def change_after_decode(*args):
                value = decode_recovery_transcript(*args)
                if failure == 'schema':
                    self.journal._check_schema = lambda: (_ for _ in ()).throw(Rejected('Changed schema'))
                else:
                    original = self.journal.reconcile
                    def changed(*args):
                        report = original(*args); report['binding']['reserved_at'] = '2020-01-01T00:00:00Z'; return report
                    self.journal.reconcile = changed
                return value
            with patch.object(self.journal, '_check_schema', wraps=self.journal._check_schema), patch.object(self.journal, 'reconcile', wraps=self.journal.reconcile):
                with patch('orch.github_recovery_reads.decode_recovery_transcript', side_effect=change_after_decode), self.assertRaises(Rejected):
                    self.assess()
            self.assertFalse(self.journal.db.in_transaction)

    def test_cli_plan_assessment_and_unresolved_exit(self):
        from orch.__main__ import main
        source = Path(self.temp.name) / 'capture.json'
        common = ['--journal',str(self.path),'--operation-id',self.saved['operation_id'],'--expected-sha256',self.saved['sha256']]
        out = io.StringIO()
        with patch.object(sys,'argv',['orch','github-recovery-read-plan',*common]), redirect_stdout(out): main()
        self.assertEqual(json.loads(out.getvalue()), self.plan)
        for rows, status in (([candidate(intent())], 'candidate_observed'), ([], 'unresolved')):
            source.write_bytes(canonical(self.transcript(self.exchange(1, rows))))
            out = io.StringIO()
            with patch.object(sys,'argv',['orch','github-reconcile-transcript',*common,'--transcript',str(source)]), redirect_stdout(out):
                if status == 'unresolved':
                    with self.assertRaises(SystemExit) as raised: main()
                    self.assertEqual(raised.exception.code,2)
                else: main()
            self.assertEqual(json.loads(out.getvalue())['status'],status)
        source.write_text('{"bad":1}')
        out = io.StringIO()
        with patch.object(sys,'argv',['orch','github-reconcile-transcript',*common,'--transcript',str(source)]), redirect_stdout(out), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit): main()
        self.assertEqual(out.getvalue(),'')
        for extra in (['--repository-id','0'], ['--allow-repository',''], ['--intent','']):
            out = io.StringIO()
            with patch.object(sys,'argv',['orch','github-recovery-read-plan',*common,*extra]), redirect_stdout(out), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit): main()
            self.assertEqual(out.getvalue(),'')
