from contextlib import redirect_stdout, redirect_stderr
import copy
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from orch.contracts import Rejected, canonical, digest
from orch.github_journal import GitHubJournal
from test_github_preview import intent


class JournalInspectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'github.sqlite'
        self.source = intent()
        journal = GitHubJournal(self.path)
        self.saved = journal.stage(self.source, 'example/project', 123)
        journal.close()

    def tearDown(self):
        self.temp.cleanup()

    def inspect(self):
        journal = GitHubJournal(self.path, read_only=True)
        try:
            return journal.get(self.source['operation_id'])
        finally:
            journal.close()

    def test_prepared_and_uncertain_inspection_preserve_files(self):
        for state in ('prepared', 'uncertain'):
            if state == 'uncertain':
                journal = GitHubJournal(self.path)
                try:
                    self.saved = journal.reserve(self.source['operation_id'], self.saved['sha256'])
                finally:
                    journal.close()
            before = self.path.read_bytes()
            files = set(self.path.parent.iterdir())
            self.assertEqual(self.inspect(), self.saved)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(set(self.path.parent.iterdir()), files)

    def test_sqlite_and_public_methods_are_read_only(self):
        journal = GitHubJournal(self.path, read_only=True)
        try:
            with self.assertRaisesRegex(Rejected, 'read-only'):
                journal.stage(self.source, 'example/project', 123)
            with self.assertRaisesRegex(Rejected, 'read-only'):
                journal.reserve(self.source['operation_id'], self.saved['sha256'])
            with self.assertRaises(sqlite3.OperationalError):
                journal.db.execute('CREATE TABLE forbidden(value TEXT)')
        finally:
            journal.close()
        self.assertEqual(self.inspect(), self.saved)

    def test_missing_and_unrelated_database_are_not_initialized(self):
        missing = self.path.parent / 'missing' / 'journal.sqlite'
        with self.assertRaises(sqlite3.OperationalError):
            GitHubJournal(missing, read_only=True)
        self.assertFalse(missing.parent.exists())
        other = self.path.parent / 'empty.sqlite'
        sqlite3.connect(other).close()
        before = other.read_bytes()
        with self.assertRaises(Rejected):
            GitHubJournal(other, read_only=True)
        self.assertEqual(other.read_bytes(), before)

    def corrupt(self, sql, args):
        db = sqlite3.connect(self.path)
        try:
            db.execute('DROP TRIGGER IF EXISTS immutable_scope')
            db.execute('DROP TRIGGER IF EXISTS one_reservation')
            db.execute(sql, args)
            db.commit()
        finally:
            db.close()

    def test_rehashed_invalid_scopes_are_rejected(self):
        for change in ('draft', 'url', 'extra', 'flag_type', 'repository_id', 'nested_digest'):
            with self.subTest(change=change):
                scope = copy.deepcopy(self.saved['scope'])
                preview = scope['preview']
                request = preview['binding']['request']
                if change == 'draft':
                    request['body']['draft'] = False
                elif change == 'url':
                    request['url'] = request['url'].replace('api.github.com', 'example.com')
                elif change == 'extra':
                    scope['extra'] = 'unexpected'
                elif change == 'flag_type':
                    preview['live_authorized'] = 0
                elif change == 'repository_id':
                    scope['repository_id'] = True
                preview['sha256'] = 'f' * 64 if change == 'nested_digest' else digest(preview['binding'])
                self.corrupt('UPDATE intents SET scope=?, sha256=?', (canonical(scope).decode(), digest(scope)))
                before = self.path.read_bytes()
                with self.assertRaisesRegex(Rejected, 'Invalid GitHub journal record'):
                    self.inspect()
                self.assertEqual(self.path.read_bytes(), before)

    def test_row_identity_and_noncanonical_json_are_rejected(self):
        self.corrupt('UPDATE intents SET task_id=?', ('c' * 32,))
        with self.assertRaises(Rejected):
            self.inspect()
        self.corrupt('UPDATE intents SET task_id=?, scope=?', (self.source['task_id'], json.dumps(self.saved['scope'], indent=2)))
        with self.assertRaises(Rejected):
            self.inspect()

    def test_inconsistent_reservation_evidence_is_rejected(self):
        for state, reserved in [('prepared', '2026-01-01T00:00:00Z'), ('uncertain', None), ('uncertain', 'invalidZ'), ('uncertain', '2026-01-01T00:00:00')]:
            with self.subTest(state=state, reserved=reserved):
                self.corrupt('UPDATE intents SET state=?, reserved_at=?', (state, reserved))
                with self.assertRaises(Rejected):
                    self.inspect()

    def test_cli_inspects_without_workflow_network_or_mutation(self):
        from orch.__main__ import main
        before = self.path.read_bytes()
        argv = ['orch', 'github-inspect', '--journal', str(self.path), '--operation-id', self.source['operation_id']]
        output = io.StringIO()
        with patch.object(sys, 'argv', argv), redirect_stdout(output), patch('socket.socket', side_effect=AssertionError('No network')), patch('subprocess.Popen', side_effect=AssertionError('No process')), patch('orch.__main__.Engine', side_effect=AssertionError('No workflow')):
            main()
        self.assertEqual(json.loads(output.getvalue()), self.saved)
        self.assertEqual(self.path.read_bytes(), before)
        argv[-1] = 'unknown'
        with patch.object(sys, 'argv', argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(self.path.read_bytes(), before)
