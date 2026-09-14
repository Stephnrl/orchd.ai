from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from orch.contracts import Rejected, digest
from orch.github_approval import prepare_approval
from orch.github_journal import GitHubJournal
from test_github_preview import intent


class GitHubApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'journal.sqlite'
        self.journal = GitHubJournal(self.path)
        self.saved = self.journal.stage(intent(), 'example/project', 123)
        self.evidence = {'task_id': intent()['task_id'], 'patch_sha256': '1' * 64,
                         'test_sha256': '2' * 64, 'review_sha256': '3' * 64, 'policy_sha256': '4' * 64}

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def prepare(self, evidence=None, sha=None):
        return prepare_approval(self.journal, self.saved['operation_id'], sha or self.saved['sha256'], self.evidence if evidence is None else evidence)

    def test_complete_scope_evidence_and_expiry_are_bound_without_mutation(self):
        before = self.path.read_bytes()
        report = self.prepare()
        binding = report['binding']
        self.assertEqual(binding['scope'], self.saved['scope'])
        self.assertEqual(binding['scope_sha256'], self.saved['sha256'])
        self.assertEqual(binding['evidence'], self.evidence)
        self.assertEqual(binding['operation_id'], self.saved['operation_id'])
        self.assertEqual(report['sha256'], digest(binding))
        self.assertEqual(datetime.fromisoformat(binding['expires_at']) - datetime.fromisoformat(binding['issued_at']), timedelta(minutes=15))
        self.assertRegex(binding['request_id'], r'^[a-f0-9]{32}$')
        for flag in ('evidence_verified', 'live_authorized', 'retry_allowed'):
            self.assertIs(report[flag], False)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.journal.get(self.saved['operation_id']), self.saved)

    def test_each_evidence_digest_changes_approval_scope(self):
        with patch('orch.github_approval.now', return_value='2026-01-01T00:00:00Z'), patch('orch.github_approval.uid', return_value='c' * 32):
            original = self.prepare()
            for name in ('patch_sha256', 'test_sha256', 'review_sha256', 'policy_sha256'):
                changed = self.prepare({**self.evidence, name: 'f' * 64})
                self.assertNotEqual(changed['sha256'], original['sha256'])
        self.assertNotEqual(self.prepare()['binding']['request_id'], self.prepare()['binding']['request_id'])

    def test_malformed_cross_task_and_extra_evidence_reject(self):
        values = [{}, {**self.evidence, 'task_id': 'c' * 32}, {**self.evidence, 'approved': True}]
        for bad in (True, 'f' * 63, 'F' * 64, 'f' * 64 + '\n', None):
            values.append({**self.evidence, 'policy_sha256': bad})
        for value in values:
            with self.subTest(value=value), self.assertRaises(Rejected):
                self.prepare(value)
            self.assertFalse(self.journal.db.in_transaction)

    def test_wrong_scope_and_consumed_reservation_cannot_prepare(self):
        with self.assertRaises(Rejected):
            self.prepare(sha='f' * 64)
        self.journal.reserve(self.saved['operation_id'], self.saved['sha256'])
        with self.assertRaises(Rejected):
            self.prepare()
        self.assertEqual(self.journal.get(self.saved['operation_id'])['state'], 'uncertain')

    def test_missing_protection_rejects_and_releases_transaction(self):
        self.journal.db.execute('DROP TRIGGER no_delete')
        with self.assertRaises(Rejected):
            self.prepare()
        self.assertFalse(self.journal.db.in_transaction)

    def test_cli_is_read_only_and_missing_journal_is_not_created(self):
        from orch.__main__ import main
        evidence = Path(self.temp.name) / 'evidence.json'
        evidence.write_text(json.dumps(self.evidence))
        before = self.path.read_bytes()
        argv = ['orch', 'github-approval-preview', '--journal', str(self.path), '--operation-id', self.saved['operation_id'], '--expected-sha256', self.saved['sha256'], '--evidence', str(evidence)]
        output = io.StringIO()
        with patch.object(sys, 'argv', argv), redirect_stdout(output), patch('orch.__main__.Engine', side_effect=AssertionError('No workflow')), patch('socket.socket', side_effect=AssertionError('No network')), patch('subprocess.Popen', side_effect=AssertionError('No process')):
            main()
        self.assertEqual(json.loads(output.getvalue())['kind'], 'GitHubApprovalPreview')
        self.assertEqual(self.path.read_bytes(), before)
        missing = Path(self.temp.name) / 'missing' / 'journal.sqlite'
        argv[3] = str(missing)
        output = io.StringIO()
        with patch.object(sys, 'argv', argv), redirect_stdout(output), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(output.getvalue(), '')
        self.assertFalse(missing.parent.exists())
