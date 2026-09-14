from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from orch.contracts import Rejected, digest
from orch.github_journal import GitHubJournal
from test_github_journal_capacity import proposal


class JournalAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'journal.sqlite'
        self.writer = GitHubJournal(self.path)

    def tearDown(self):
        self.writer.close()
        self.temp.cleanup()

    def stage(self, number):
        return self.writer.stage(proposal(number), 'example/project', 123)

    def audit(self):
        reader = GitHubJournal(self.path, read_only=True)
        try:
            return reader.audit()
        finally:
            reader.close()

    def test_empty_and_complete_audits_are_deterministic_and_read_only(self):
        self.assertEqual(self.audit()['binding']['record_count'], 0)
        second = self.stage(2)
        first = self.stage(1)
        self.writer.reserve(second['operation_id'], second['sha256'])
        before = self.path.read_bytes()
        report = self.audit()
        self.assertEqual(report, self.audit())
        self.assertEqual(report['sha256'], digest(report['binding']))
        self.assertEqual(report['binding']['record_count'], 2)
        self.assertEqual(report['binding']['scope_bytes'], self.writer.usage()['scope_bytes'])
        rows = report['binding']['records']
        self.assertEqual([row['operation_id'] for row in rows], [first['operation_id'], second['operation_id']])
        self.assertEqual([row['state'] for row in rows], ['prepared', 'uncertain'])
        self.assertNotIn(proposal(1)['body'], json.dumps(report))
        self.assertIs(report['live_authorized'], False)
        self.assertIs(report['retry_allowed'], False)
        self.assertEqual(self.path.read_bytes(), before)

    def test_reservation_changes_audit_digest_without_changing_scope_digest(self):
        saved = self.stage(1)
        before = self.audit()
        self.writer.reserve(saved['operation_id'], saved['sha256'])
        after = self.audit()
        self.assertNotEqual(before['sha256'], after['sha256'])
        self.assertEqual(before['binding']['records'][0]['sha256'], after['binding']['records'][0]['sha256'])

    def test_corrupt_second_record_rejects_entire_audit_and_releases_transaction(self):
        self.stage(1)
        self.stage(2)
        self.writer.db.execute('DROP TRIGGER immutable_scope')
        self.writer.db.execute('DROP TRIGGER one_reservation')
        self.writer.db.execute('UPDATE intents SET sha256=? WHERE operation_id=?', ('invalid', proposal(2)['operation_id']))
        with self.assertRaises(Rejected):
            self.writer.audit()
        self.assertFalse(self.writer.db.in_transaction)
        self.assertEqual(self.writer.usage()['records'], 2)

    def test_audit_budget_rejects_before_scanning_records(self):
        self.stage(1)
        for name in ('MAX_RECORDS', 'MAX_TOTAL_SCOPE_BYTES'):
            with patch('orch.github_journal.' + name, 0), patch.object(self.writer, 'get', side_effect=AssertionError('No record scan')):
                with self.assertRaisesRegex(Rejected, 'audit budget'):
                    self.writer.audit()
                self.assertFalse(self.writer.db.in_transaction)

    def test_concurrent_reservation_does_not_mix_snapshot_states(self):
        self.writer.db.execute('PRAGMA journal_mode=WAL')
        self.stage(1)
        second = self.stage(2)
        reader = GitHubJournal(self.path, read_only=True)
        try:
            original = reader.get
            changed = False

            def get_and_reserve(operation):
                nonlocal changed
                result = original(operation)
                if not changed:
                    changed = True
                    self.writer.reserve(second['operation_id'], second['sha256'])
                return result

            with patch.object(reader, 'get', side_effect=get_and_reserve):
                report = reader.audit()
            self.assertEqual([row['state'] for row in report['binding']['records']], ['prepared', 'prepared'])
            self.assertEqual(reader.audit()['binding']['records'][1]['state'], 'uncertain')
        finally:
            reader.close()

    def cli(self):
        from orch.__main__ import main
        output = io.StringIO()
        argv = ['orch', 'github-journal-audit', '--journal', str(self.path)]
        with patch.object(sys, 'argv', argv), redirect_stdout(output), redirect_stderr(io.StringIO()), patch('orch.__main__.Engine', side_effect=AssertionError('No workflow')), patch('socket.socket', side_effect=AssertionError('No network')), patch('subprocess.Popen', side_effect=AssertionError('No process')):
            try:
                main()
            except SystemExit as error:
                return error.code, output.getvalue()
        return 0, output.getvalue()

    def test_cli_success_and_failure_never_emit_partial_reports(self):
        self.stage(1)
        code, output = self.cli()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)['status'], 'valid')
        self.writer.db.execute('DROP TRIGGER immutable_scope')
        self.writer.db.execute('DROP TRIGGER one_reservation')
        self.writer.db.execute("UPDATE intents SET scope='{}'")
        self.assertEqual(self.cli(), (2, ''))

    def test_missing_journal_is_not_created(self):
        self.path = self.path.parent / 'missing' / 'journal.sqlite'
        self.assertEqual(self.cli(), (2, ''))
        self.assertFalse(self.path.parent.exists())
