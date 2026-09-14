from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from orch.contracts import Rejected, digest
from orch.github_approval import prepare_approval
from orch.github_evidence import assess_approval_evidence, check_evidence
from orch.github_journal import GitHubJournal
from orch.storage import Store
from test_github_preview import intent


class CombinedAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.journal = GitHubJournal(self.root / 'journal.sqlite')
        self.saved = self.journal.stage(intent(), 'example/project', 123)
        self.store = Store(self.root / 'workflow')
        task = intent()['task_id']
        self.store.db.execute('INSERT INTO tasks VALUES(?,?,?)', (task, '{}', '{}'))
        self.evidence = {'task_id': task}
        for role in ('patch', 'test', 'review', 'policy'):
            self.evidence[role + '_sha256'] = self.store.artifact(task, {'role': role})['sha256']
        with patch('orch.github_approval.now', return_value='2026-01-01T00:00:00Z'):
            self.preview = prepare_approval(self.journal, self.saved['operation_id'], self.saved['sha256'], self.evidence)

    def tearDown(self):
        self.journal.close()
        self.store.close()
        self.temp.cleanup()

    def assess(self):
        return assess_approval_evidence(self.journal, self.store, self.preview, self.preview['sha256'], self.evidence)

    def test_combines_fresh_checks_and_binds_complete_results(self):
        before = (self.root / 'journal.sqlite').read_bytes()
        with patch('orch.github_approval.now', return_value='2026-01-01T00:01:00Z'):
            report = self.assess()
        self.assertEqual(report['status'], 'current')
        self.assertTrue(report['artifact_bytes_verified'])
        self.assertEqual(report['sha256'], digest(report['binding']))
        self.assertEqual(report['binding']['artifact_evidence']['binding']['evidence_sha256'], digest(self.evidence))
        self.assertEqual(report['binding']['approval']['binding']['preview_sha256'], self.preview['sha256'])
        for key in ('evidence_verified', 'live_authorized', 'retry_allowed'):
            self.assertIs(report[key], False)
        self.assertEqual((self.root / 'journal.sqlite').read_bytes(), before)

    def test_initially_blocked_preview_skips_artifact_reads(self):
        with patch('orch.github_approval.now', return_value='2026-01-01T00:15:00Z'), patch('orch.github_evidence.check_evidence', side_effect=AssertionError('No artifact reads')):
            report = self.assess()
        self.assertEqual(report['status'], 'blocked')
        self.assertIsNone(report['binding']['artifact_evidence'])
        self.assertFalse(report['artifact_bytes_verified'])

    def test_expiry_during_artifact_check_is_caught(self):
        with patch('orch.github_approval.now', side_effect=['2026-01-01T00:14:59Z', '2026-01-01T00:15:00Z']):
            report = self.assess()
        self.assertEqual(report['status'], 'blocked')
        self.assertTrue(report['artifact_bytes_verified'])
        self.assertEqual(report['binding']['approval']['binding']['blockers'], ['expired'])

    def test_reservation_during_artifact_check_is_caught(self):
        def check_and_reserve(store, evidence):
            result = check_evidence(store, evidence)
            self.journal.reserve(self.saved['operation_id'], self.saved['sha256'])
            return result

        with patch('orch.github_approval.now', return_value='2026-01-01T00:01:00Z'), patch('orch.github_evidence.check_evidence', side_effect=check_and_reserve):
            report = self.assess()
        self.assertEqual(report['status'], 'blocked')
        self.assertEqual(report['binding']['approval']['binding']['blockers'], ['journal_not_prepared'])

    def test_missing_artifact_rejects_without_partial_assessment(self):
        (self.store.root / 'artifacts' / self.evidence['test_sha256']).unlink()
        with patch('orch.github_approval.now', return_value='2026-01-01T00:01:00Z'), self.assertRaises(Rejected):
            self.assess()
        self.assertFalse(self.store.db.in_transaction)
        self.assertFalse(self.journal.db.in_transaction)

    def test_cli_uses_read_only_stores_and_reports_failure_without_partial_output(self):
        from orch.__main__ import main
        preview_path, evidence_path = self.root / 'preview.json', self.root / 'evidence.json'
        preview_path.write_text(json.dumps(self.preview))
        evidence_path.write_text(json.dumps(self.evidence))
        argv = ['orch', 'github-assess-approval', '--data', str(self.store.root), '--journal', str(self.root / 'journal.sqlite'), '--approval-preview', str(preview_path), '--evidence', str(evidence_path), '--expected-sha256', self.preview['sha256']]
        output = io.StringIO()
        with patch.object(sys, 'argv', argv), redirect_stdout(output), patch('orch.github_approval.now', return_value='2026-01-01T00:01:00Z'), patch('orch.__main__.Engine', side_effect=AssertionError('No workflow')), patch('socket.socket', side_effect=AssertionError('No network')), patch('subprocess.Popen', side_effect=AssertionError('No process')), self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code, 0)
        self.assertEqual(json.loads(output.getvalue())['status'], 'current')
        (self.store.root / 'artifacts' / self.evidence['test_sha256']).unlink()
        output = io.StringIO()
        with patch.object(sys, 'argv', argv), redirect_stdout(output), redirect_stderr(io.StringIO()), patch('orch.github_approval.now', return_value='2026-01-01T00:01:00Z'), self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(output.getvalue(), '')
