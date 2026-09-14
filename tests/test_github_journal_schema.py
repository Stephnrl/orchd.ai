import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from orch.contracts import Rejected, canonical, digest
from orch.github_backup import backup_journal, verify_journal_backup, drill_journal_backup, file_digest
from orch.github_journal import GitHubJournal, SCHEMA_SQL
from test_github_journal_capacity import proposal


class JournalSchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'journal.sqlite'
        self.journal = GitHubJournal(self.path)
        self.saved = self.journal.stage(proposal(1), 'example/project', 123)

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def assert_gated(self):
        before = self.path.read_bytes()
        for action in (self.journal.audit,
                       lambda: self.journal.stage(proposal(2), 'example/project', 123),
                       lambda: self.journal.stage(proposal(1), 'example/project', 123),
                       lambda: self.journal.reserve(self.saved['operation_id'], self.saved['sha256'])):
            with self.assertRaisesRegex(Rejected, 'schema or constraint'):
                action()
            self.assertFalse(self.journal.db.in_transaction)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.journal.get(self.saved['operation_id']), self.saved)

    def test_each_missing_trigger_blocks_audit_and_writes_without_repair(self):
        for name, sql in zip(('immutable_scope', 'no_delete', 'one_reservation'), SCHEMA_SQL[1:]):
            with self.subTest(name=name):
                self.journal.db.execute('DROP TRIGGER ' + name)
                self.assert_gated()
                reader = GitHubJournal(self.path, read_only=True)
                try:
                    with self.assertRaises(Rejected):
                        reader.audit()
                finally:
                    reader.close()
                self.assertIsNone(self.journal.db.execute('SELECT name FROM sqlite_master WHERE name=?', (name,)).fetchone())
                self.journal.db.execute(sql)

    def test_same_name_noop_trigger_is_not_accepted(self):
        self.journal.db.execute('DROP TRIGGER no_delete')
        self.journal.db.execute('CREATE TRIGGER no_delete BEFORE DELETE ON intents BEGIN SELECT 1; END')
        self.assert_gated()

    def test_additional_schema_objects_and_temp_shadowing_reject(self):
        for create, remove in (
            ('CREATE TABLE extra(value TEXT)', 'DROP TABLE extra'),
            ('CREATE INDEX extra ON intents(state)', 'DROP INDEX extra'),
            ('CREATE TEMP TABLE intents(value TEXT)', 'DROP TABLE temp.intents')):
            self.journal.db.execute(create)
            with self.assertRaises(Rejected):
                self.journal.audit()
            with self.assertRaises(Rejected):
                self.journal.reserve(self.saved['operation_id'], self.saved['sha256'])
            self.journal.db.execute(remove)
        self.assertEqual(self.journal.audit()['status'], 'valid')

    def test_changed_constraints_and_identity_reject(self):
        self.journal.db.execute('PRAGMA ignore_check_constraints=ON')
        self.assert_gated()
        self.journal.db.execute('PRAGMA ignore_check_constraints=OFF')
        self.journal.db.execute('PRAGMA application_id=0')
        self.assert_gated()
        self.journal.db.execute('PRAGMA application_id=1330792264')
        self.journal.db.execute('DROP TABLE intents')
        self.journal.db.execute(SCHEMA_SQL[0].replace('task_id TEXT NOT NULL UNIQUE', 'task_id TEXT NOT NULL'))
        for sql in SCHEMA_SQL[1:]:
            self.journal.db.execute(sql)
        with self.assertRaises(Rejected):
            self.journal.audit()

    def test_rehashed_backup_with_missing_trigger_fails_verification_and_drill(self):
        destination = Path(self.temp.name) / 'backup'
        backup_journal(self.path, destination)
        database = destination / 'journal.sqlite'
        connection = sqlite3.connect(database)
        try:
            connection.execute('DROP TRIGGER no_delete')
        finally:
            connection.close()
        path = destination / 'manifest.json'
        manifest = json.loads(path.read_bytes())
        sha, size = file_digest(database)
        manifest['binding'].update(database_sha256=sha, database_bytes=size)
        manifest['sha256'] = digest(manifest['binding'])
        path.write_bytes(canonical(manifest))
        with self.assertRaises(Rejected):
            verify_journal_backup(destination)
        with patch('orch.github_backup.tempfile.TemporaryDirectory', side_effect=AssertionError('No scratch')):
            with self.assertRaises(Rejected):
                drill_journal_backup(destination, manifest['sha256'])

    def test_external_schema_change_is_rechecked_after_connection_open(self):
        connection = sqlite3.connect(self.path)
        try:
            connection.execute('DROP TRIGGER one_reservation')
        finally:
            connection.close()
        self.assert_gated()
