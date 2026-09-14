from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from orch.contracts import Rejected, digest
from orch.github_backup import backup_journal, compare_journal_backup
from orch.github_journal import GitHubJournal
from test_github_journal_capacity import proposal


class BackupComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.source = Path(self.temp.name) / 'journal.sqlite'
        self.bundle = Path(self.temp.name) / 'backup'
        self.writer = GitHubJournal(self.source)
        self.saved = self.writer.stage(proposal(1), 'example/project', 123)
        self.backup = backup_journal(self.source, self.bundle)

    def tearDown(self):
        self.writer.close()
        self.temp.cleanup()

    def compare(self, source=None):
        return compare_journal_backup(source or self.source, self.bundle, self.backup['sha256'])

    def test_matches_bind_both_audits_without_modification_or_authority(self):
        before = {path: path.read_bytes() for path in [self.source, *self.bundle.iterdir()]}
        report = self.compare()
        self.assertEqual(report['status'], 'matches')
        self.assertEqual(report['binding']['differences'], [])
        self.assertEqual(report['binding']['backup_audit_sha256'], self.writer.audit()['sha256'])
        self.assertEqual(report['binding']['journal_audit_sha256'], self.writer.audit()['sha256'])
        self.assertEqual(report['sha256'], digest(report['binding']))
        for key in ('restore_allowed', 'retry_allowed', 'live_authorized'):
            self.assertIs(report[key], False)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_later_reservation_and_new_operation_are_reported(self):
        self.writer.reserve(self.saved['operation_id'], self.saved['sha256'])
        new = self.writer.stage(proposal(2), 'example/project', 123)
        report = self.compare()
        self.assertEqual(report['status'], 'different')
        self.assertEqual(report['binding']['differences'], [
            {'operation_id': self.saved['operation_id'], 'reasons': ['reservation_mismatch']},
            {'operation_id': new['operation_id'], 'reasons': ['missing_from_backup']}])
        self.assertNotIn(proposal(1)['body'], json.dumps(report))

    def test_backup_only_and_changed_scope_are_not_treated_as_safe(self):
        other = Path(self.temp.name) / 'other.sqlite'
        journal = GitHubJournal(other)
        try:
            report = self.compare(other)
            self.assertEqual(report['binding']['differences'][0]['reasons'], ['backup_only_operation'])
            journal.stage({**proposal(1), 'title': 'Other scope'}, 'example/project', 123)
            report = self.compare(other)
            self.assertEqual(report['binding']['differences'][0]['reasons'], ['scope_mismatch'])
            self.assertFalse(report['restore_allowed'])
        finally:
            journal.close()

    def test_different_consumed_timestamps_are_detected(self):
        reserved = self.writer.reserve(self.saved['operation_id'], self.saved['sha256'])
        # Two independently valid histories with the same scope are still different.
        second_bundle = Path(self.temp.name) / 'reserved-backup'
        result = backup_journal(self.source, second_bundle)
        other = Path(self.temp.name) / 'other.sqlite'
        journal = GitHubJournal(other)
        try:
            journal.stage(proposal(1), 'example/project', 123)
            with patch('orch.github_journal.now', return_value='2000-01-01T00:00:00Z'):
                journal.reserve(self.saved['operation_id'], self.saved['sha256'])
            report = compare_journal_backup(other, second_bundle, result['sha256'])
            self.assertNotEqual(reserved['reserved_at'], '2000-01-01T00:00:00Z')
            self.assertEqual(report['binding']['differences'][0]['reasons'], ['reservation_mismatch'])
        finally:
            journal.close()

    def test_wrong_bundle_digest_rejects_before_opening_source(self):
        missing = Path(self.temp.name) / 'missing' / 'journal.sqlite'
        with self.assertRaisesRegex(Rejected, 'Unexpected journal backup digest'):
            compare_journal_backup(missing, self.bundle, 'f' * 64)
        self.assertFalse(missing.parent.exists())

    def test_invalid_bundle_or_current_record_rejects(self):
        manifest = self.bundle / 'manifest.json'
        original = manifest.read_bytes()
        manifest.write_bytes(b'{}')
        with self.assertRaises(Rejected):
            self.compare()
        manifest.write_bytes(original)
        self.writer.db.execute('DROP TRIGGER immutable_scope')
        self.writer.db.execute('DROP TRIGGER one_reservation')
        self.writer.db.execute("UPDATE intents SET sha256='invalid'")
        with self.assertRaises(Rejected):
            self.compare()

    def test_cli_status_and_isolation(self):
        from orch.__main__ import main
        argv = ['orch', 'github-compare-journal-backup', '--journal', str(self.source), '--destination', str(self.bundle), '--expected-sha256', self.backup['sha256']]
        for expected in (0, 2):
            if expected == 2:
                self.writer.reserve(self.saved['operation_id'], self.saved['sha256'])
            output = io.StringIO()
            with patch.object(sys, 'argv', argv), redirect_stdout(output), patch('orch.__main__.Engine', side_effect=AssertionError('No workflow')), patch('socket.socket', side_effect=AssertionError('No network')), patch('subprocess.Popen', side_effect=AssertionError('No process')), self.assertRaises(SystemExit) as error:
                main()
            self.assertEqual(error.exception.code, expected)
            self.assertFalse(json.loads(output.getvalue())['restore_allowed'])
        argv[-1] = 'invalid'
        output = io.StringIO()
        with patch.object(sys, 'argv', argv), redirect_stdout(output), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(output.getvalue(), '')
