import copy
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from orch.contracts import Rejected, digest
from orch.github_approval import prepare_approval, check_approval
from orch.github_journal import GitHubJournal
from test_github_preview import intent


class ApprovalValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'journal.sqlite'
        self.journal = GitHubJournal(self.path)
        self.saved = self.journal.stage(intent(), 'example/project', 123)
        self.evidence = {'task_id': intent()['task_id'], 'patch_sha256': '1' * 64,
                         'test_sha256': '2' * 64, 'review_sha256': '3' * 64, 'policy_sha256': '4' * 64}
        with patch('orch.github_approval.now', return_value='2026-01-01T00:00:00Z'):
            self.preview = prepare_approval(self.journal, self.saved['operation_id'], self.saved['sha256'], self.evidence)

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def check(self, at='2026-01-01T00:01:00Z', evidence=None):
        with patch('orch.github_approval.now', return_value=at):
            return check_approval(self.journal, self.preview, self.preview['sha256'], self.evidence if evidence is None else evidence)

    def test_current_is_read_only_and_never_approval(self):
        before = self.path.read_bytes()
        report = self.check()
        self.assertEqual(report['status'], 'current')
        self.assertEqual(report['sha256'], digest(report['binding']))
        self.assertEqual(report['binding']['preview_sha256'], self.preview['sha256'])
        for flag in ('evidence_verified', 'live_authorized', 'retry_allowed'):
            self.assertIs(report[flag], False)
        self.assertEqual(self.path.read_bytes(), before)

    def test_time_window_includes_issue_and_excludes_exact_expiry(self):
        for at, blockers in [('2025-12-31T23:59:59Z', ['not_yet_valid']),
                             ('2026-01-01T00:00:00Z', []),
                             ('2026-01-01T00:14:59Z', []),
                             ('2026-01-01T00:15:00Z', ['expired'])]:
            with self.subTest(at=at):
                self.assertEqual(self.check(at)['binding']['blockers'], blockers)

    def test_changed_evidence_and_consumed_journal_block(self):
        self.journal.reserve(self.saved['operation_id'], self.saved['sha256'])
        report = self.check(evidence={**self.evidence, 'policy_sha256': 'f' * 64})
        self.assertEqual(report['status'], 'blocked')
        self.assertEqual(report['binding']['blockers'], ['journal_not_prepared', 'evidence_mismatch'])
        self.assertFalse(self.journal.db.in_transaction)

    def test_rehashed_flags_extra_fields_and_extended_expiry_reject(self):
        original = copy.deepcopy(self.preview)
        for change in ('flag', 'extra', 'expiry', 'task', 'scope'):
            self.preview = copy.deepcopy(original)
            if change == 'flag':
                self.preview['live_authorized'] = 0
            elif change == 'extra':
                self.preview['binding']['approved'] = True
            elif change == 'expiry':
                self.preview['binding']['expires_at'] = '2026-01-02T00:00:00Z'
            elif change == 'task':
                self.preview['binding']['task_id'] = 'c' * 32
            else:
                self.preview['binding']['scope']['preview']['binding']['request']['body']['draft'] = False
            self.preview['sha256'] = digest(self.preview['binding'])
            with self.subTest(change=change), self.assertRaises(Rejected):
                self.check()

    def test_wrong_digest_and_invalid_evidence_reject(self):
        with self.assertRaises(Rejected):
            check_approval(self.journal, self.preview, 'f' * 64, self.evidence)
        with self.assertRaises(Rejected):
            self.check(evidence={**self.evidence, 'task_id': []})
        with self.assertRaises(Rejected):
            check_approval(self.journal, {}, 'f' * 64, self.evidence)

    def test_different_valid_journal_scope_is_reported(self):
        other = GitHubJournal(Path(self.temp.name) / 'other.sqlite')
        try:
            other.stage({**intent(), 'title': 'Different title'}, 'example/project', 123)
            with patch('orch.github_approval.now', return_value='2026-01-01T00:01:00Z'):
                report = check_approval(other, self.preview, self.preview['sha256'], self.evidence)
            self.assertEqual(report['binding']['blockers'], ['scope_mismatch'])
        finally:
            other.close()

    def test_cli_current_expired_and_invalid_without_effects(self):
        from orch.__main__ import main
        preview_path, evidence_path = Path(self.temp.name) / 'preview.json', Path(self.temp.name) / 'evidence.json'
        preview_path.write_text(json.dumps(self.preview))
        evidence_path.write_text(json.dumps(self.evidence))
        argv = ['orch', 'github-check-approval', '--journal', str(self.path), '--approval-preview', str(preview_path), '--evidence', str(evidence_path), '--expected-sha256', self.preview['sha256']]
        before = self.path.read_bytes()
        for at, code in [('2026-01-01T00:01:00Z', 0), ('2026-01-01T00:15:00Z', 2)]:
            output = io.StringIO()
            with patch.object(sys, 'argv', argv), redirect_stdout(output), patch('orch.github_approval.now', return_value=at), patch('orch.__main__.Engine', side_effect=AssertionError('No workflow')), patch('socket.socket', side_effect=AssertionError('No network')), patch('subprocess.Popen', side_effect=AssertionError('No process')), self.assertRaises(SystemExit) as error:
                main()
            self.assertEqual(error.exception.code, code)
            self.assertFalse(json.loads(output.getvalue())['live_authorized'])
        preview_path.write_text('{}')
        output = io.StringIO()
        with patch.object(sys, 'argv', argv), redirect_stdout(output), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(self.path.read_bytes(), before)
