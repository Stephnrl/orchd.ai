from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from orch.contracts import Rejected, canonical, digest
from orch.github_backup import backup_journal, verify_journal_backup, file_digest
from orch.github_journal import GitHubJournal
from test_github_preview import intent


class JournalBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.source = Path(self.temp.name) / 'source.sqlite'
        self.destination = Path(self.temp.name) / 'backup'
        self.writer = GitHubJournal(self.source)
        self.saved = self.writer.stage(intent(), 'example/project', 123)

    def tearDown(self):
        self.writer.close()
        self.temp.cleanup()

    def test_backup_reopens_with_consumed_reservation_and_verifies(self):
        saved = self.writer.reserve(self.saved['operation_id'], self.saved['sha256'])
        before = self.source.read_bytes()
        result = backup_journal(self.source, self.destination)
        self.assertEqual(result, verify_journal_backup(self.destination))
        self.assertEqual(result['record_count'], 1)
        for flag in ('restore_allowed', 'retry_allowed', 'live_authorized'):
            self.assertIs(result[flag], False)
        self.assertEqual(self.source.read_bytes(), before)
        copy = GitHubJournal(self.destination / 'journal.sqlite', read_only=True)
        try:
            self.assertEqual(copy.get(saved['operation_id']), saved)
            self.assertEqual(copy.audit(), self.writer.audit())
        finally:
            copy.close()

    def test_existing_destination_is_never_reused(self):
        self.destination.mkdir()
        with self.assertRaises(FileExistsError):
            backup_journal(self.source, self.destination)
        self.assertEqual(list(self.destination.iterdir()), [])
        with self.assertRaises(FileExistsError):
            backup_journal(self.source, self.source)

    def test_timeout_leaves_no_completion_manifest(self):
        with patch('orch.github_backup.time.monotonic', side_effect=[0, 31]):
            with self.assertRaisesRegex(Rejected, 'timed out'):
                backup_journal(self.source, self.destination)
        self.assertFalse((self.destination / 'manifest.json').exists())
        with self.assertRaises(Rejected):
            verify_journal_backup(self.destination)
        self.assertEqual(self.writer.get(self.saved['operation_id']), self.saved)

    def test_corrupt_source_and_page_budget_fail_before_output(self):
        with patch('orch.github_backup.MAX_DATABASE_BYTES', 1):
            with self.assertRaises(Rejected):
                backup_journal(self.source, self.destination)
        self.assertFalse(self.destination.exists())
        self.writer.db.execute('DROP TRIGGER immutable_scope')
        self.writer.db.execute('DROP TRIGGER one_reservation')
        self.writer.db.execute("UPDATE intents SET scope='{}'")
        with self.assertRaises(Rejected):
            backup_journal(self.source, self.destination)
        self.assertFalse(self.destination.exists())

    def test_rehashed_corrupt_database_is_not_valid_evidence(self):
        backup_journal(self.source, self.destination)
        database = self.destination / 'journal.sqlite'
        connection = sqlite3.connect(database)
        try:
            connection.execute('DROP TRIGGER immutable_scope')
            connection.execute('DROP TRIGGER one_reservation')
            connection.execute("UPDATE intents SET sha256='invalid'")
            connection.commit()
        finally:
            connection.close()
        path = self.destination / 'manifest.json'
        manifest = json.loads(path.read_bytes())
        sha, size = file_digest(database)
        manifest['binding'].update(database_sha256=sha, database_bytes=size)
        manifest['sha256'] = digest(manifest['binding'])
        path.write_bytes(canonical(manifest))
        with self.assertRaises(Rejected):
            verify_journal_backup(self.destination)

    def test_manifest_and_unexpected_sidecars_reject(self):
        backup_journal(self.source, self.destination)
        path = self.destination / 'manifest.json'
        before = path.read_bytes()
        for raw in (b'{}', b'{"x":1,"x":1}', b'x' * (1024 * 1024 + 1)):
            path.write_bytes(raw)
            with self.assertRaises(Rejected):
                verify_journal_backup(self.destination)
        path.write_bytes(before)
        (self.destination / 'journal.sqlite-wal').write_bytes(b'')
        with self.assertRaises(Rejected):
            verify_journal_backup(self.destination)

    def test_wal_writer_cannot_change_pinned_backup_snapshot(self):
        self.writer.db.execute('PRAGMA journal_mode=WAL')
        calls = 0

        def clock():
            nonlocal calls
            calls += 1
            if calls == 2:
                self.writer.reserve(self.saved['operation_id'], self.saved['sha256'])
            return 0

        with patch('orch.github_backup.time.monotonic', side_effect=clock):
            backup_journal(self.source, self.destination)
        self.assertEqual(self.writer.get(self.saved['operation_id'])['state'], 'uncertain')
        copy = GitHubJournal(self.destination / 'journal.sqlite', read_only=True)
        try:
            self.assertEqual(copy.get(self.saved['operation_id'])['state'], 'prepared')
        finally:
            copy.close()

    def test_cli_backup_and_verification_are_isolated(self):
        from orch.__main__ import main
        for command in ('github-journal-backup', 'github-verify-journal-backup'):
            argv = ['orch', command, '--journal', str(self.source), '--destination', str(self.destination)]
            output = io.StringIO()
            with patch.object(sys, 'argv', argv), redirect_stdout(output), patch('orch.__main__.Engine', side_effect=AssertionError('No workflow')), patch('socket.socket', side_effect=AssertionError('No network')), patch('subprocess.Popen', side_effect=AssertionError('No process')):
                main()
            self.assertEqual(json.loads(output.getvalue())['status'], 'valid')
        argv = ['orch', 'github-journal-backup', '--journal', str(self.source), '--destination', str(self.destination)]
        output = io.StringIO()
        with patch.object(sys, 'argv', argv), redirect_stdout(output), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(output.getvalue(), '')
