from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from orch.contracts import Rejected, canonical, digest
from orch.jira_backup import (backup_journal, verify_journal_backup, compare_journal_backup,
                              drill_journal_backup, file_digest)
from orch.jira_journal import JiraJournal
from orch.jira_preview import prepare_comment, review_issue
from test_jira_preview import target, observation, intent


class JiraBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.destination = self.root/'source.sqlite', self.root/'backup'
        self.writer = JiraJournal(self.source)
        self.addCleanup(self.writer.close)
        self.target, self.issue = target(), observation()
        self.intent = intent(review_issue(self.target, self.issue))
        self.saved = self.stage(self.intent)
        self.operation = self.intent['operation_id']
        self.sha = self.saved['binding']['sha256']

    def stage(self, proposal):
        preview = prepare_comment(proposal, self.target, self.issue)
        return self.writer.stage(proposal, self.target, self.issue, preview['sha256'], 'author-key')

    def backup(self):
        return backup_journal(self.source, self.destination)

    def files(self):
        return {p.name: p.read_bytes() for p in self.destination.iterdir()}

    def test_round_trip_preserves_scope_and_reservation_without_mutation(self):
        saved = self.writer.reserve(self.operation, self.sha)
        before = self.source.read_bytes()
        report = self.backup()
        bundle = self.files()
        self.assertEqual(report, verify_journal_backup(self.destination, report['sha256']))
        reader = JiraJournal(self.destination/'journal.sqlite', read_only=True)
        try:
            self.assertEqual(reader.inspect(self.operation), saved)
            self.assertEqual(reader.get(self.operation), self.writer.get(self.operation))
        finally:
            reader.close()
        comparison = compare_journal_backup(self.source, self.destination, report['sha256'])
        self.assertEqual(comparison['status'], 'matches')
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(self.files(), bundle)
        self.assertEqual(report['record_count'], 1)
        for result in (report, comparison):
            for flag in ('restore_allowed', 'retry_allowed', 'live_authorized', 'remote_effect_confirmed'):
                self.assertFalse(result[flag])
            self.assertNotIn(self.intent['body'], canonical(result).decode())
            self.assertNotIn('Untrusted task notes.', canonical(result).decode())

    def test_mixed_state_drill_reopens_without_touching_active_or_backup(self):
        self.writer.reserve(self.operation, self.sha)
        self.stage({**self.intent, 'task_id': 'c'*32, 'operation_id': 'd'*32})
        report = self.backup()
        before, bundle = self.source.read_bytes(), self.files()
        with patch('socket.socket', side_effect=AssertionError('No network')), patch('subprocess.Popen', side_effect=AssertionError('No process')):
            drill = drill_journal_backup(self.destination, report['sha256'])
        self.assertEqual(drill['status'], 'passed')
        self.assertEqual(drill['binding']['prepared_exercised'], 1)
        self.assertEqual(drill['binding']['existing_uncertain_checked'], 1)
        self.assertEqual(drill['binding']['repeat_reservations_rejected'], 2)
        self.assertEqual(drill['binding']['recovery_plans_checked'], 2)
        self.assertEqual(drill['sha256'], digest(drill['binding']))
        self.assertFalse(drill['restore_allowed'])
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(self.files(), bundle)

    def test_comparison_exposes_stale_reservations_and_missing_operations(self):
        report = self.backup()
        self.writer.reserve(self.operation, self.sha)
        self.stage({**self.intent, 'task_id': 'c'*32, 'operation_id': 'd'*32})
        result = compare_journal_backup(self.source, self.destination, report['sha256'])
        self.assertEqual(result['status'], 'different')
        self.assertEqual(result['binding']['differences'], [
            {'operation_id': self.operation, 'reasons': ['reservation_mismatch']},
            {'operation_id': 'd'*32, 'reasons': ['missing_from_backup']}])

    def test_comparison_reports_backup_only_and_conflicting_scope(self):
        report = self.backup()
        other = self.root/'other.sqlite'
        journal = JiraJournal(other)
        try:
            result = compare_journal_backup(other, self.destination, report['sha256'])
            self.assertEqual(result['binding']['differences'][0]['reasons'], ['backup_only_operation'])
            changed = {**self.intent, 'body': 'Different proposed comment'}
            preview = prepare_comment(changed, self.target, self.issue)
            journal.stage(changed, self.target, self.issue, preview['sha256'], 'author-key')
            result = compare_journal_backup(other, self.destination, report['sha256'])
            self.assertEqual(result['binding']['differences'][0]['reasons'], ['scope_mismatch'])
        finally:
            journal.close()

    def test_expected_digest_required_for_review_operations(self):
        self.backup()
        for action in (lambda: verify_journal_backup(self.destination, 'f'*64),
                       lambda: compare_journal_backup(self.source, self.destination, 'f'*64),
                       lambda: drill_journal_backup(self.destination, 'f'*64)):
            with self.assertRaises(Rejected): action()

    def test_existing_destination_never_reused_and_source_budget_precedes_output(self):
        with patch('orch.jira_backup.MAX_DATABASE_BYTES', 1):
            with self.assertRaises(Rejected): self.backup()
        self.assertFalse(self.destination.exists())
        self.destination.mkdir()
        with self.assertRaises(FileExistsError): self.backup()
        self.assertEqual(list(self.destination.iterdir()), [])
        with self.assertRaises(FileExistsError): backup_journal(self.source, self.source)

    def test_timeout_or_invalid_source_has_no_completion_manifest(self):
        with patch('orch.jira_backup.time.monotonic', side_effect=[0, 31]):
            with self.assertRaisesRegex(Rejected, 'timed out'): self.backup()
        self.assertFalse((self.destination/'manifest.json').exists())
        with self.assertRaises(Rejected): verify_journal_backup(self.destination)
        self.writer.db.execute('DROP TRIGGER no_delete')
        invalid = self.root/'invalid'
        with self.assertRaises(Rejected): backup_journal(self.source, invalid)
        self.assertFalse(invalid.exists())

    def test_manifest_shape_encoding_budget_and_sidecars_reject(self):
        self.backup()
        manifest = self.destination/'manifest.json'
        original = manifest.read_bytes()
        for raw in (b'{}', original+b' ', b'{"a":1,"a":1}', b'x'*1048577):
            manifest.write_bytes(raw)
            with self.assertRaises(Rejected): verify_journal_backup(self.destination)
        manifest.write_bytes(original)
        for name in ('journal.sqlite-wal', 'journal.sqlite-journal', 'unexpected'):
            extra = self.destination/name
            extra.write_bytes(b'')
            with self.assertRaises(Rejected): verify_journal_backup(self.destination)
            extra.unlink()

    def test_rehashed_tampering_and_wrong_database_are_rejected(self):
        self.backup()
        database = self.destination/'journal.sqlite'
        connection = sqlite3.connect(database)
        try:
            connection.execute('DROP TRIGGER no_delete')
            connection.commit()
        finally:
            connection.close()
        path = self.destination/'manifest.json'
        manifest = json.loads(path.read_bytes())
        sha, size = file_digest(database)
        manifest['binding'].update(database_sha256=sha, database_bytes=size)
        manifest['sha256'] = digest(manifest['binding'])
        path.write_bytes(canonical(manifest))
        with self.assertRaises(Rejected): verify_journal_backup(self.destination)
        from orch.github_journal import GitHubJournal
        other = self.root/'github.sqlite'
        journal = GitHubJournal(other)
        journal.close()
        with self.assertRaises(Rejected): backup_journal(other, self.root/'wrong-backup')

    def test_wal_writer_does_not_change_pinned_snapshot(self):
        self.writer.db.execute('PRAGMA journal_mode=WAL')
        calls = 0
        def clock():
            nonlocal calls
            calls += 1
            if calls == 2:
                self.writer.reserve(self.operation, self.sha)
            return 0
        with patch('orch.jira_backup.time.monotonic', side_effect=clock):
            report = self.backup()
        reader = JiraJournal(self.destination/'journal.sqlite', read_only=True)
        try:
            self.assertEqual(reader.get(self.operation)['state'], 'prepared')
        finally:
            reader.close()
        self.assertEqual(self.writer.get(self.operation)['state'], 'uncertain')
        self.assertEqual(compare_journal_backup(self.source, self.destination, report['sha256'])['status'], 'different')

    def test_path_links_and_reparse_points_are_rejected(self):
        self.backup()
        info = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        with patch.object(Path, 'lstat', return_value=info):
            for action in (lambda: verify_journal_backup(self.destination),
                           lambda: backup_journal(self.source, self.root/'linked')):
                with self.assertRaises(Rejected): action()
        link = self.root/'linked'
        try:
            link.symlink_to(self.destination, target_is_directory=True)
        except OSError:
            return
        with self.assertRaises(Rejected): verify_journal_backup(link)

    def test_drill_timeout_cleans_scratch_and_keeps_bundle(self):
        report = self.backup()
        before = self.files()
        factory = tempfile.TemporaryDirectory
        created = []
        def scratch(**kwargs):
            result = factory(dir=self.root, **kwargs)
            created.append(Path(result.name))
            return result
        with patch('orch.jira_backup.tempfile.TemporaryDirectory', side_effect=scratch), patch('orch.jira_backup.time.monotonic', side_effect=[0, 31]):
            with self.assertRaisesRegex(Rejected, 'timed out'): drill_journal_backup(self.destination, report['sha256'])
        self.assertEqual(len(created), 2)
        self.assertTrue(all(not path.exists() for path in created))
        self.assertEqual(self.files(), before)

    def test_cli_complete_flow_and_failures(self):
        from orch.__main__ import main
        def run(command, extra):
            out = io.StringIO()
            with patch.object(sys, 'argv', ['orch', command, '--destination', str(self.destination), *extra]), redirect_stdout(out), redirect_stderr(io.StringIO()):
                try:
                    main()
                    code = 0
                except SystemExit as exc:
                    code = exc.code
            return code, out.getvalue()
        with patch('orch.__main__.Engine', side_effect=AssertionError('No workflow')), patch('socket.socket', side_effect=AssertionError('No network')):
            code, out = run('jira-journal-backup', ['--journal', str(self.source)])
            self.assertEqual(code, 0)
            expected = ['--expected-sha256', json.loads(out)['sha256']]
            for command, extra in (('jira-verify-journal-backup', expected), ('jira-journal-recovery-drill', expected),
                                   ('jira-compare-journal-backup', [*expected, '--journal', str(self.source)])):
                code, out = run(command, extra)
                self.assertEqual(code, 0)
                self.assertFalse(json.loads(out)['restore_allowed'])
            self.writer.reserve(self.operation, self.sha)
            code, out = run('jira-compare-journal-backup', [*expected, '--journal', str(self.source)])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(out)['status'], 'different')
            for extra in ([], [*expected, '--journal', str(self.source)], [*expected, '--jira-author-key', 'override'], ['--expected-sha256', 'f'*64]):
                code, out = run('jira-journal-recovery-drill', extra)
                self.assertEqual(code, 2)
                self.assertEqual(out, '')
