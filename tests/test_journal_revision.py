"""Withdrawing a prepared operation so its task can be prepared again.

Every behaviour is proven against both operation journals, because the rule they share —
one live operation per task, one attempt per operation — is the thing being changed.
"""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from orch.__main__ import main
from orch.contracts import Rejected, canonical, digest
from orch.maintenance import real_path
from orch import journal_revision, journal_succession
from orch.journal_revision import upgrade, withdraw

from orch.github_backup import (backup_journal as github_backup, compare_journal_backup as github_compare,
                                drill_journal_backup as github_drill)
from orch.github_journal import GitHubJournal
from orch.jira_backup import (backup_journal as jira_backup, compare_journal_backup as jira_compare,
                              drill_journal_backup as jira_drill)
from orch.jira_journal import JiraJournal
from orch.jira_actions import preview_action

from test_github_journal_capacity import proposal
from jira_action_support import profile, intent as jira_intent, capture as jira_capture

ROOT = Path(__file__).resolve().parents[1]
REASON = "The base branch moved before dispatch"


class RevisionBase:
    """One journal, its snapshot, and the upgrade that intent revision needs."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # Canonical, because records name journals by path and a hosted Windows runner
        # hands out an 8.3 short %TEMP%.
        self.root = real_path(self.temp.name)
        self.first = self.root / 'one.sqlite'
        self.snapshot = self.root / 'snapshot'
        self.journal = self.open(self.first)

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def snap(self, path=None, destination=None):
        return self.backup(path or self.first, destination or self.snapshot)

    def upgraded(self, path=None, destination=None):
        """Stage nothing new; take a snapshot and rebuild the table revision needs."""
        created = self.snap(path, destination)
        self.journal.close()
        report = upgrade(path or self.first, destination or self.snapshot, created['sha256'], self.label)
        self.journal = self.open(path or self.first)
        return report

    def rows(self, journal):
        return [tuple(row) for row in
                journal.db.execute("SELECT * FROM intents ORDER BY operation_id").fetchall()]

    # The rebuild ---------------------------------------------------------------------------

    def test_upgrade_reproduces_every_record_and_changes_no_audit(self):
        first, second = self.stage(self.journal, 1), self.stage(self.journal, 2)
        self.journal.reserve(*second)
        before, rows = self.journal.audit(), self.rows(self.journal)
        report = self.upgraded()
        self.assertEqual((report['status'], report['records_deleted'], report['records_changed']),
                         ('upgraded', 0, 0))
        self.assertEqual(report['schema_version'], journal_revision.VERSION)
        self.assertEqual(report['audit_sha256'], before['sha256'])
        self.assertEqual(self.journal.audit()['sha256'], before['sha256'])
        self.assertEqual(self.rows(self.journal), rows)
        self.assertEqual(self.journal.db.execute("PRAGMA user_version").fetchone()[0], journal_revision.VERSION)
        self.assertEqual(self.stage(self.journal, 1), first)      # Still idempotent.
        with self.assertRaisesRegex(Rejected, 'already supports'):
            self.upgraded(destination=self.root / 'again')

    def test_upgrade_requires_a_matching_snapshot(self):
        self.stage(self.journal, 1)
        created = self.snap()
        self.stage(self.journal, 2)            # The snapshot is now stale.
        self.journal.close()
        with self.assertRaisesRegex(Rejected, 'matches the journal'):
            upgrade(self.first, self.snapshot, created['sha256'], self.label)
        with self.assertRaises(Rejected):
            upgrade(self.first, self.snapshot, 'f' * 64, self.label)
        self.journal = self.open(self.first)
        self.assertEqual(self.journal.db.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(self.journal.audit()['status'], 'valid')

    def test_an_interrupted_upgrade_leaves_the_journal_exactly_as_it_was(self):
        self.stage(self.journal, 1)
        before, rows = self.journal.audit(), self.rows(self.journal)
        created = self.snap()
        self.journal.close()
        with patch('orch.journal_succession.install', side_effect=OSError('interrupted mid-rebuild')):
            with self.assertRaises(OSError):
                upgrade(self.first, self.snapshot, created['sha256'], self.label)
        self.journal = self.open(self.first)
        self.assertEqual(self.journal.db.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(self.rows(self.journal), rows)
        self.assertEqual(self.journal.audit()['sha256'], before['sha256'])
        self.assertEqual(self.stage(self.journal, 2)[0][:4], self.stage(self.journal, 2)[0][:4])
        self.journal.close()
        again = self.snap(destination=self.root / 'snapshot-two')
        self.journal = self.open(self.first)
        self.journal.close()
        upgrade(self.first, self.root / 'snapshot-two', again['sha256'], self.label)
        self.journal = self.open(self.first)
        self.assertEqual(self.journal.db.execute("PRAGMA user_version").fetchone()[0], journal_revision.VERSION)

    def test_withdrawal_needs_the_rebuilt_schema(self):
        staged = self.stage(self.journal, 1)
        with self.assertRaisesRegex(Rejected, 'upgrade it first'):
            withdraw(self.journal, staged[0], staged[1], REASON)
        self.assertEqual(self.journal.get(staged[0])['state'] if self.label == 'GitHub'
                         else self.journal.inspect(staged[0])['binding']['state'], 'prepared')

    # Withdrawal ------------------------------------------------------------------------------

    def test_withdrawal_frees_the_task_but_never_the_attempt(self):
        staged = self.stage(self.journal, 1)
        self.upgraded()
        report = withdraw(self.journal, staged[0], staged[1], REASON)
        self.assertEqual((report['status'], report['attempt_released'], report['records_deleted']),
                         ('withdrawn', False, 0))
        self.assertEqual(self.record(self.journal, staged[0])['state'], 'withdrawn')
        saved = journal_revision.read_record(self.journal.db, staged[0], self.first)
        self.assertEqual((saved['reason'], saved['scope_sha256'], saved['journal']),
                         (REASON, staged[1], str(self.first)))
        with self.assertRaisesRegex(Rejected, 'withdrawn'):
            self.stage(self.journal, 1)                   # The identifier is never reused.
        with self.assertRaises(Rejected):
            self.journal.reserve(*staged)                 # And it can never be reserved.
        replacement = self.replacement(self.journal, 1, 'f')
        self.assertNotEqual(replacement[0], staged[0])
        self.assertEqual(self.record(self.journal, replacement[0])['state'], 'prepared')
        with self.assertRaises(Rejected):
            self.replacement(self.journal, 1, 'e')        # One live operation per task.
        usage = self.journal.usage()
        self.assertEqual((usage['records'], usage['withdrawn_records']), (2, 1))

    def test_a_consumed_attempt_can_never_be_withdrawn(self):
        staged = self.stage(self.journal, 1)
        self.upgraded()
        self.journal.reserve(*staged)
        for _ in range(2):
            with self.assertRaisesRegex(Rejected, 'reconcile it'):
                withdraw(self.journal, staged[0], staged[1], REASON)
        self.assertEqual(self.record(self.journal, staged[0])['state'], 'uncertain')
        with self.assertRaises(Rejected):
            self.replacement(self.journal, 1, 'f')        # The task stays taken.

    def test_withdrawal_fences_on_the_retained_scope_and_an_audited_reason(self):
        staged = self.stage(self.journal, 1)
        self.upgraded()
        for operation, scope, reason in ((staged[0], 'f' * 64, REASON),
                                         ('a' * 32, staged[1], REASON),
                                         (staged[0], staged[1], ''),
                                         (staged[0], staged[1], 'x' * 501),
                                         (staged[0], staged[1], 'line\nbreak'),
                                         (staged[0], staged[1], 'token=' + 'a' * 40),
                                         (staged[0], staged[1], None)):
            with self.subTest(scope=scope[:8], reason=str(reason)[:12]):
                with self.assertRaises(Rejected):
                    withdraw(self.journal, operation, scope, reason)
        self.assertEqual(self.record(self.journal, staged[0])['state'], 'prepared')
        withdraw(self.journal, staged[0], staged[1], REASON)
        with self.assertRaisesRegex(Rejected, 'already withdrawn'):
            withdraw(self.journal, staged[0], staged[1], REASON)
        reader = self.open(self.first, read_only=True)
        try:
            with self.assertRaises(Rejected):
                withdraw(reader, staged[0], staged[1], REASON)
        finally:
            reader.close()

    # Storage protections ----------------------------------------------------------------------

    def test_storage_refuses_a_second_live_operation_and_any_rewrite(self):
        staged = self.stage(self.journal, 1)
        self.upgraded()
        withdraw(self.journal, staged[0], staged[1], REASON)
        row = self.journal.db.execute("SELECT scope,sha256 FROM intents WHERE operation_id=?", staged[:1]).fetchone()
        self.replacement(self.journal, 1, 'f')
        with self.assertRaises(sqlite3.IntegrityError):   # The partial unique index, not this code.
            self.journal.db.execute("INSERT INTO intents VALUES('c'||?, ?, ?, ?, 'prepared', NULL)",
                                    (staged[0][1:], self.record(self.journal, staged[0])['task_id'],
                                     row['scope'], row['sha256']))
        for statement in ("UPDATE intents SET state='prepared' WHERE state='withdrawn'",
                          "UPDATE intents SET state='uncertain', reserved_at='2026-01-01T00:00:00Z' WHERE state='withdrawn'",
                          "DELETE FROM intents",
                          "UPDATE " + journal_revision.TABLE + " SET record='{}'",
                          "DELETE FROM " + journal_revision.TABLE):
            with self.subTest(statement=statement[:40]):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.journal.db.execute(statement)
        self.assertEqual(self.journal.audit()['status'], 'valid')

    def test_a_tampered_or_foreign_withdrawal_record_is_refused(self):
        staged = self.stage(self.journal, 1)
        self.upgraded()
        withdraw(self.journal, staged[0], staged[1], REASON)
        saved = journal_revision.read_record(self.journal.db, staged[0])
        self.assertEqual(saved['journal'], str(self.first))
        with self.assertRaisesRegex(Rejected, 'names another journal'):
            journal_revision.read_record(self.journal.db, staged[0], self.root / 'elsewhere.sqlite')
        self.journal.db.execute("DROP TRIGGER immutable_withdrawal")
        self.journal.db.execute("UPDATE " + journal_revision.TABLE + " SET sha256='" + 'a' * 64 + "'")
        self.journal.db.execute(journal_revision.WITHDRAWAL_SQL[1])
        with self.assertRaisesRegex(Rejected, 'integrity mismatch'):
            self.journal.audit()

    def test_each_schema_shape_is_exact(self):
        self.stage(self.journal, 1)
        self.upgraded()
        self.assertEqual(self.journal.audit()['status'], 'valid')
        for statement, undo in (("DROP INDEX one_live_operation", journal_revision.SCHEMA_SQL[1:2]),
                                ("DROP TRIGGER retained_withdrawal", journal_revision.WITHDRAWAL_SQL[2:]),
                                # Dropping a table takes its triggers with it.
                                ("DROP TABLE " + journal_revision.TABLE, journal_revision.WITHDRAWAL_SQL)):
            with self.subTest(statement=statement):
                self.journal.db.execute(statement)
                with self.assertRaises(Rejected):
                    self.journal.audit()
                for restore in undo:
                    self.journal.db.execute(restore)
        self.journal.db.execute("PRAGMA user_version=%d" % journal_succession.VERSION)
        with self.assertRaises(Rejected):
            self.journal.audit()
        self.journal.db.execute("PRAGMA user_version=%d" % journal_revision.VERSION)
        self.assertEqual(self.journal.audit()['status'], 'valid')

    # How revision meets the rest of the lifecycle ------------------------------------------------

    def test_a_retired_journal_still_withdraws_and_its_successor_takes_the_task(self):
        staged = self.stage(self.journal, 1)
        self.upgraded()
        successor = self.root / 'two.sqlite'
        self.journal.close()
        created = self.snap(destination=self.root / 'snapshot-retire')
        journal_succession.retire(self.first, self.root / 'snapshot-retire', created['sha256'],
                                  successor, 'Journal full', self.label)
        self.journal = self.open(self.first)
        heir = self.open(successor)
        try:
            with self.assertRaisesRegex(Rejected, 'already retains'):
                self.stage(heir, 1)                      # A live operation still blocks the task.
            withdraw(self.journal, staged[0], staged[1], REASON)   # Retirement stops admission only.
            with self.assertRaisesRegex(Rejected, 'retired'):
                self.replacement(self.journal, 1, 'f')   # But not new work in the retired journal.
            with self.assertRaisesRegex(Rejected, 'already retains'):
                self.stage(heir, 1)                      # That identifier is still spent.
            # The task is free again, under an identifier the chain has never seen.
            self.assertEqual(self.record(heir, self.replacement(heir, 1, 'f')[0])['state'], 'prepared')
        finally:
            heir.close()

    def test_snapshots_and_drills_account_for_withdrawn_records(self):
        staged = self.stage(self.journal, 1)
        second = self.stage(self.journal, 2)
        self.journal.reserve(*second)
        self.upgraded()
        created = self.snap(destination=self.root / 'before-withdrawal')
        withdraw(self.journal, staged[0], staged[1], REASON)
        comparison = self.compare(self.first, self.root / 'before-withdrawal', created['sha256'])
        self.assertEqual(comparison['status'], 'different')
        self.assertEqual(comparison['binding']['differences'],
                         [{'operation_id': staged[0], 'reasons': ['withdrawal_mismatch']}])
        self.journal.close()
        fresh = self.snap(destination=self.root / 'after-withdrawal')
        self.assertEqual(self.compare(self.first, self.root / 'after-withdrawal', fresh['sha256'])['status'],
                         'matches')
        drill = self.drill(self.root / 'after-withdrawal', fresh['sha256'])
        self.assertEqual((drill['status'], drill['binding']['withdrawn_checked']), ('passed', 1))
        self.journal = self.open(self.first)

    def test_revision_survives_closing_and_reopening_the_journal(self):
        staged = self.stage(self.journal, 1)
        self.upgraded()
        withdraw(self.journal, staged[0], staged[1], REASON)
        replacement = self.replacement(self.journal, 1, 'f')
        before = self.journal.audit()
        self.journal.close()
        self.journal = self.open(self.first)
        self.assertEqual(self.journal.audit()['sha256'], before['sha256'])
        self.assertEqual(self.record(self.journal, staged[0])['state'], 'withdrawn')
        self.assertEqual(self.record(self.journal, replacement[0])['state'], 'prepared')
        code = ("from orch.journal_succession import family; import json,sys;"
                "j=family(sys.argv[2])[0](sys.argv[1], read_only=True);"
                "print(json.dumps([r['state'] for r in j.audit()['binding']['records']]))")
        result = subprocess.run([sys.executable, '-c', code, str(self.first), self.label],
                                cwd=ROOT, env={**os.environ, 'PYTHONPATH': str(ROOT)},
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-400:])
        self.assertEqual(sorted(json.loads(result.stdout)), ['prepared', 'withdrawn'])

    def test_cli_upgrades_and_withdraws(self):
        staged = self.stage(self.journal, 1)
        created = self.snap()
        self.journal.close()
        prefix = self.label.lower()

        def cli(*argv, expect=0):
            # In process: a real CLI run without an interpreter start per command.
            output = io.StringIO()
            try:
                with (patch.object(sys, 'argv', ['orch', *argv]), redirect_stdout(output),
                      redirect_stderr(io.StringIO())):
                    main()
            except SystemExit as exit_code:
                self.assertEqual(exit_code.code, expect)
                return None
            self.assertEqual(expect, 0)
            return json.loads(output.getvalue())

        cli(prefix + '-journal-withdraw', '--journal', str(self.first), '--operation-id', staged[0],
            '--expected-sha256', staged[1], '--reason', REASON, expect=2)   # Not upgraded yet.
        report = cli(prefix + '-journal-upgrade', '--journal', str(self.first),
                     '--destination', str(self.snapshot), '--expected-sha256', created['sha256'])
        self.assertEqual(report['status'], 'upgraded')
        cli(prefix + '-journal-withdraw', '--journal', str(self.first), '--operation-id', staged[0],
            '--expected-sha256', staged[1], expect=2)                       # A reason is required.
        withdrawn = cli(prefix + '-journal-withdraw', '--journal', str(self.first), '--operation-id',
                        staged[0], '--expected-sha256', staged[1], '--reason', REASON)
        self.assertEqual((withdrawn['status'], withdrawn['attempt_released']), ('withdrawn', False))
        usage = json.loads(subprocess.check_output(
            [sys.executable, '-m', 'orch', prefix + '-journal-usage', '--journal', str(self.first)],
            cwd=ROOT, env={**os.environ, 'PYTHONPATH': str(ROOT)}, timeout=120))
        self.assertEqual(usage['withdrawn_records'], 1)
        self.journal = self.open(self.first)


class GitHubRevisionTests(RevisionBase, unittest.TestCase):
    label = 'GitHub'

    def open(self, path, read_only=False):
        return GitHubJournal(path, read_only=read_only)

    def stage(self, journal, number):
        saved = journal.stage(proposal(number), 'example/project', 123)
        return saved['operation_id'], saved['sha256']

    def replacement(self, journal, number, character):
        source = proposal(number)
        source['operation_id'] = character * 32
        source['head'] = 'orchd/' + source['task_id'] + '/' + character * 32
        saved = journal.stage(source, 'example/project', 123)
        return saved['operation_id'], saved['sha256']

    def record(self, journal, operation_id):
        return journal.get(operation_id)

    def backup(self, path, destination):
        return github_backup(path, destination)

    def compare(self, path, destination, expected):
        return github_compare(path, destination, expected)

    def drill(self, destination, expected):
        return github_drill(destination, expected)


class JiraRevisionTests(RevisionBase, unittest.TestCase):
    label = 'Jira'

    def open(self, path, read_only=False):
        return JiraJournal(path, read_only=read_only)

    def _stage(self, journal, proposed):
        config = profile()
        observed = jira_capture(proposed, config)
        preview = preview_action(proposed, config, observed)
        saved = journal.stage_action(proposed, config, observed, preview['sha256'])
        return saved['binding']['operation_id'], saved['binding']['sha256']

    def stage(self, journal, number):
        return self._stage(journal, jira_intent('create_issue', number))

    def replacement(self, journal, number, character):
        proposed = jira_intent('create_issue', number)
        proposed['operation_id'] = character * 32
        return self._stage(journal, proposed)

    def record(self, journal, operation_id):
        return journal.inspect(operation_id)['binding']

    def backup(self, path, destination):
        return jira_backup(path, destination)

    def compare(self, path, destination, expected):
        return jira_compare(path, destination, expected)

    def drill(self, destination, expected):
        return jira_drill(destination, expected)


if __name__ == '__main__':
    unittest.main()
