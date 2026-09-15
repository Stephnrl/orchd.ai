"""Retirement and succession for the GitHub and Jira operation journals.

Every behaviour is proven against both journals, because the point of the shared module
is that one operation journal cannot quietly diverge from the other.
"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from orch.contracts import Rejected, canonical, digest
from orch.maintenance import real_path
from orch import journal_succession
from orch.journal_succession import TABLE, chain, retire

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
REASON = "Journal full; continuing in the successor"


class SuccessionBase:
    """One journal, its snapshot and a successor path, for whichever family is under test."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # Succession records name journals canonically, and a hosted Windows runner has an
        # 8.3 short %TEMP%; compare against the spelling the journal itself would store.
        self.root = real_path(self.temp.name)
        self.first = self.root / 'one.sqlite'
        self.successor = self.root / 'two.sqlite'
        self.snapshot = self.root / 'snapshot'
        self.journal = self.open(self.first)

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def snap(self, path=None, destination=None):
        return self.backup(path or self.first, destination or self.snapshot)

    def retire(self, path=None, successor=None, snapshot=None, expected=None, reason=REASON):
        created = expected or self.snap(path, snapshot)
        return retire(path or self.first, snapshot or self.snapshot,
                      created['sha256'] if isinstance(created, dict) else created,
                      successor or self.successor, reason, self.label)

    def counts(self, journal):
        return journal.db.execute("SELECT count(*) FROM intents").fetchone()[0]

    def forge(self, path, role, record):
        """Write a succession record the way a privileged actor with direct SQL would."""
        connection = sqlite3.connect(path)
        try:
            journal_succession.install(connection)
            connection.execute("INSERT INTO " + TABLE + " VALUES(?,?,?)",
                               (role, canonical(record).decode(), digest(record)))
            connection.commit()
        finally:
            connection.close()

    # Retirement leaves the evidence alone -------------------------------------------------

    def test_retirement_changes_no_record_and_closes_only_admission(self):
        first, second = self.stage(self.journal, 1), self.stage(self.journal, 2)
        self.journal.reserve(*second)
        before = self.journal.audit()
        self.journal.close()
        report = self.retire()
        self.journal = self.open(self.first)
        self.assertEqual((report['status'], report['records_deleted']), ('retired', 0))
        self.assertEqual((report['retained_records'], report['uncertain_records']), (2, 1))
        self.assertFalse(report['restore_allowed'] or report['retry_allowed'] or report['live_authorized'])
        # Nothing moved: the audit binds every record, and its digest is unchanged.
        self.assertEqual(self.journal.audit()['sha256'], before['sha256'])
        self.assertEqual(self.counts(self.journal), 2)
        usage = self.journal.usage()
        self.assertEqual((usage['journal_status'], usage['successor']), ('retired', str(self.successor)))
        with self.assertRaisesRegex(Rejected, 'retired'):
            self.stage(self.journal, 3)
        # Everything an operator still needs keeps working in the retired journal.
        self.assertEqual(self.stage(self.journal, 1), first)
        self.assertEqual(self.state(self.journal.reserve(*first)), 'uncertain')
        self.assertEqual(self.counts(self.journal), 2)

    def test_retired_journal_still_reserves_and_reads_what_it_holds(self):
        first = self.stage(self.journal, 1)
        self.journal.close()
        self.retire()
        self.journal = self.open(self.first)
        reserved = self.journal.reserve(*first)
        self.assertEqual(self.state(reserved), 'uncertain')
        self.assertEqual(self.journal.audit()['status'], 'valid')
        fresh = self.snap(destination=self.root / 'after')
        self.assertEqual(self.compare(self.first, self.root / 'after', fresh['sha256'])['status'], 'matches')

    def test_a_retired_journals_bundle_still_verifies_and_drills(self):
        first = self.stage(self.journal, 1)
        self.stage(self.journal, 2)
        self.journal.reserve(*first)
        self.journal.close()
        self.retire()
        fresh = self.snap(destination=self.root / 'after')
        # Recovery drills run on a disposable copy of a retired journal exactly as before.
        drill = self.drill(self.root / 'after', fresh['sha256'])
        self.assertEqual(drill['status'], 'passed')
        self.assertEqual((drill['binding']['prepared_exercised'],
                          drill['binding']['existing_uncertain_checked']), (1, 1))
        self.journal = self.open(self.first)

    # The property a hand-made replacement journal cannot have ------------------------------

    def test_successor_refuses_an_identifier_its_chain_already_holds(self):
        self.stage(self.journal, 1)
        self.journal.close()
        self.retire()
        heir = self.open(self.successor)
        try:
            with self.assertRaisesRegex(Rejected, 'already retains'):
                self.stage(heir, 1)
            self.assertEqual(self.counts(heir), 0)
            self.stage(heir, 2)
            third = self.root / 'three.sqlite'
            created = self.backup(self.successor, self.root / 'snapshot-two')
        finally:
            heir.close()
        retire(self.successor, self.root / 'snapshot-two', created['sha256'], third, REASON, self.label)
        last = self.open(third)
        try:
            # Two links back is still one attempt: neither predecessor's task returns.
            for number in (1, 2):
                with self.assertRaisesRegex(Rejected, 'already retains'):
                    self.stage(last, number)
            self.stage(last, 3)
            walked = chain(third, self.label)
        finally:
            last.close()
        self.assertEqual([entry['status'] for entry in walked['journals']], ['active', 'retired', 'retired'])
        self.assertEqual(walked['retained_records'], 3)
        self.journal = self.open(self.first)

    def test_admission_fails_closed_when_a_predecessor_cannot_be_read(self):
        self.stage(self.journal, 1)
        self.journal.close()
        self.retire()
        moved = self.root / 'moved.sqlite'
        os.replace(self.first, moved)
        heir = self.open(self.successor)
        try:
            with self.assertRaisesRegex(Rejected, 'missing'):
                self.stage(heir, 2)
            self.assertEqual(self.counts(heir), 0)
            with self.assertRaises(Rejected):
                chain(self.successor, self.label)
            os.replace(moved, self.first)
            self.stage(heir, 2)   # Readable again, and the identifier is genuinely new.
        finally:
            heir.close()
        self.journal = self.open(self.first)

    def test_a_forged_link_denies_admission_but_grants_no_second_attempt(self):
        self.stage(self.journal, 1)
        self.journal.close()
        self.retire()
        elsewhere = self.root / 'elsewhere.sqlite'
        self.open(elsewhere).close()
        # A record copied from another journal names the wrong file and is refused.
        heir = self.open(self.successor)
        try:
            copied = journal_succession.read_record(heir.db, 'follows')
        finally:
            heir.close()
        self.forge(elsewhere, 'follows', copied)
        stranger = self.open(elsewhere)
        try:
            with self.assertRaisesRegex(Rejected, 'names another journal'):
                self.stage(stranger, 5)
        finally:
            stranger.close()
        # A record invented for this journal can only deny it, never re-admit a task.
        invented = self.root / 'invented.sqlite'
        self.open(invented).close()
        self.forge(invented, 'follows', {"schema_version": "1.0.0", "role": "follows",
                                         "journal": str(invented), "predecessor": str(self.first)})
        forged = self.open(invented)
        try:
            with self.assertRaisesRegex(Rejected, 'does not name this journal'):
                self.stage(forged, 1)
        finally:
            forged.close()
        self.journal = self.open(self.first)

    # Retirement's own preconditions --------------------------------------------------------

    def test_retirement_requires_a_matching_snapshot(self):
        self.stage(self.journal, 1)
        created = self.snap()
        self.stage(self.journal, 2)   # The snapshot is now stale.
        self.journal.close()
        with self.assertRaisesRegex(Rejected, 'matches the journal'):
            self.retire(expected=created)
        with self.assertRaisesRegex(Rejected, 'digest'):
            self.retire(expected={'sha256': 'f' * 64})
        self.journal = self.open(self.first)
        self.assertEqual(self.journal.usage()['journal_status'], 'active')
        self.assertFalse(self.successor.exists())
        self.stage(self.journal, 3)   # Still an ordinary, open journal.

    def test_every_invalid_reason_is_refused_before_anything_is_written(self):
        self.stage(self.journal, 1)
        created = self.snap()
        self.journal.close()
        for reason in (None, '', '   ', 'x' * 501, 'line\nbreak', 'token=' + 'a' * 40, 7):
            with self.subTest(reason=reason):
                with self.assertRaises(Rejected):
                    self.retire(expected=created, reason=reason)
        self.assertFalse(self.successor.exists())
        self.journal = self.open(self.first)
        self.assertEqual(self.journal.usage()['journal_status'], 'active')
        self.assertEqual(self.journal.db.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_unsafe_successor_paths_are_refused(self):
        self.stage(self.journal, 1)
        created = self.snap()
        self.journal.close()
        occupied = self.root / 'occupied.sqlite'
        occupied.write_bytes(b'')
        for successor in (self.first, Path(str(self.first) + '-wal'), occupied,
                          self.root / 'missing' / 'heir.sqlite'):
            with self.subTest(successor=successor.name):
                with self.assertRaises(Rejected):
                    self.retire(expected=created, successor=successor)
        self.journal = self.open(self.first)
        self.assertEqual(self.journal.usage()['journal_status'], 'active')

    def test_double_retirement_and_a_claimed_successor_are_refused(self):
        self.stage(self.journal, 1)
        self.journal.close()
        self.retire()
        with self.assertRaisesRegex(Rejected, 'already retired'):
            self.retire(expected={'sha256': 'f' * 64})
        other = self.root / 'other.sqlite'
        third = self.open(other)
        try:
            self.stage(third, 7)
            created = self.backup(other, self.root / 'snapshot-other')
        finally:
            third.close()
        # The successor already follows this journal's predecessor.
        with self.assertRaisesRegex(Rejected, 'already exist'):
            retire(other, self.root / 'snapshot-other', created['sha256'], self.successor, REASON, self.label)
        heir = self.open(self.successor)
        try:
            self.assertEqual(journal_succession.state(heir)['predecessor'], str(self.first))
        finally:
            heir.close()
        self.journal = self.open(self.first)

    # Storage protections -------------------------------------------------------------------

    def test_succession_records_are_append_only_and_schema_bound(self):
        self.stage(self.journal, 1)
        self.journal.close()
        self.retire()
        self.journal = self.open(self.first)
        self.assertEqual(self.journal.db.execute("PRAGMA user_version").fetchone()[0], journal_succession.VERSION)
        for statement in ("UPDATE " + TABLE + " SET record='{}'", "DELETE FROM " + TABLE):
            with self.assertRaises(sqlite3.IntegrityError):
                self.journal.db.execute(statement)
        self.journal.db.execute("DROP TRIGGER immutable_succession")
        with self.assertRaises(Rejected):
            self.journal.audit()
        self.journal.db.execute(journal_succession.SCHEMA_SQL[1])
        self.assertEqual(self.journal.audit()['status'], 'valid')
        self.journal.db.execute("PRAGMA user_version=1")   # The table without its version.
        with self.assertRaises(Rejected):
            self.journal.audit()
        self.journal.db.execute("PRAGMA user_version=%d" % journal_succession.VERSION)
        self.assertEqual(self.journal.audit()['status'], 'valid')

    def test_a_succession_table_without_its_version_is_not_accepted(self):
        self.stage(self.journal, 1)
        for statement in journal_succession.SCHEMA_SQL:
            self.journal.db.execute(statement)
        with self.assertRaises(Rejected):
            self.journal.audit()
        with self.assertRaises(Rejected):
            self.stage(self.journal, 2)
        for name in ('retained_succession', 'immutable_succession'):
            self.journal.db.execute('DROP TRIGGER ' + name)
        self.journal.db.execute('DROP TABLE ' + TABLE)
        self.assertEqual(self.journal.audit()['status'], 'valid')

    def test_a_tampered_record_fails_the_audit_that_reads_it(self):
        self.stage(self.journal, 1)
        self.journal.close()
        self.retire()
        connection = sqlite3.connect(self.first)
        try:
            connection.execute("DROP TRIGGER immutable_succession")
            connection.execute("UPDATE " + TABLE + " SET sha256='" + 'a' * 64 + "'")
            connection.execute(journal_succession.SCHEMA_SQL[1])
            connection.commit()
        finally:
            connection.close()
        self.journal = self.open(self.first)
        with self.assertRaisesRegex(Rejected, 'integrity mismatch'):
            self.journal.audit()

    # Interruption and restart ---------------------------------------------------------------

    def test_an_interrupted_retirement_leaves_the_journal_open_and_the_heir_inert(self):
        self.stage(self.journal, 1)
        created = self.snap()
        self.journal.close()
        real, calls = journal_succession.install, []

        def failing(db):
            calls.append(db)
            if len(calls) == 2:      # The successor is written first; crash before the journal.
                raise OSError('interrupted between the two records')
            return real(db)

        with patch('orch.journal_succession.install', failing):
            with self.assertRaises(OSError):
                self.retire(expected=created)
        self.journal = self.open(self.first)
        self.assertEqual(self.journal.usage()['journal_status'], 'active')
        self.stage(self.journal, 2)   # The predecessor never closed.
        heir = self.open(self.successor)
        try:
            # The orphan names a predecessor that does not name it back, so it admits nothing.
            with self.assertRaisesRegex(Rejected, 'does not name this journal'):
                self.stage(heir, 3)
        finally:
            heir.close()
        with self.assertRaisesRegex(Rejected, 'already exist'):
            self.retire(expected=created)
        self.journal.close()
        again = self.snap(destination=self.root / 'snapshot-two')
        report = self.retire(expected=again, snapshot=self.root / 'snapshot-two',
                             successor=self.root / 'three.sqlite')
        self.assertEqual(report['successor'], str(self.root / 'three.sqlite'))
        self.journal = self.open(self.first)

    def test_succession_survives_closing_and_reopening_every_journal(self):
        self.stage(self.journal, 1)
        self.journal.close()
        self.retire()
        walked = chain(self.successor, self.label)
        reopened = chain(self.successor, self.label)
        self.assertEqual({k: v for k, v in walked.items() if k != 'generated_at'},
                         {k: v for k, v in reopened.items() if k != 'generated_at'})
        code = ("from orch.journal_succession import chain; import json,sys;"
                "print(json.dumps(chain(sys.argv[1], sys.argv[2])['journals'][0]['predecessor']))")
        result = subprocess.run([sys.executable, '-c', code, str(self.successor), self.label],
                                cwd=ROOT, env={**os.environ, 'PYTHONPATH': str(ROOT)},
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr[-400:])
        self.assertEqual(json.loads(result.stdout), str(self.first))
        self.journal = self.open(self.first)

    # Snapshots and the CLI --------------------------------------------------------------------

    def test_a_snapshot_taken_before_retirement_no_longer_matches(self):
        self.stage(self.journal, 1)
        created = self.snap()
        self.journal.close()
        self.assertEqual(self.compare(self.first, self.snapshot, created['sha256'])['status'], 'matches')
        self.retire(expected=created)
        comparison = self.compare(self.first, self.snapshot, created['sha256'])
        self.assertEqual(comparison['status'], 'different')
        self.assertEqual(comparison['binding']['differences'], [])   # No record changed.
        self.assertTrue(comparison['binding']['succession_changed'])
        self.assertEqual(comparison['binding']['backup_succession']['status'], 'active')
        self.assertEqual(comparison['binding']['journal_succession']['successor'], str(self.successor))
        after = self.snap(destination=self.root / 'after')
        self.assertEqual(self.compare(self.first, self.root / 'after', after['sha256'])['status'], 'matches')
        self.journal = self.open(self.first)

    def test_cli_retires_walks_the_chain_and_reports_usage(self):
        self.stage(self.journal, 1)
        created = self.snap()
        self.journal.close()
        prefix = self.label.lower()
        env = {**os.environ, 'PYTHONPATH': str(ROOT)}

        def cli(*args, expect=0):
            result = subprocess.run([sys.executable, '-m', 'orch', *args], cwd=ROOT, env=env,
                                    capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, expect, result.stderr[-400:])
            return json.loads(result.stdout) if result.returncode == 0 else result

        retire_command, chain_command = prefix + '-journal-retire', prefix + '-journal-chain'
        cli(retire_command, '--journal', str(self.first), '--destination', str(self.snapshot),
            '--expected-sha256', created['sha256'], '--successor', str(self.successor), expect=2)
        report = cli(retire_command, '--journal', str(self.first), '--destination', str(self.snapshot),
                     '--expected-sha256', created['sha256'], '--successor', str(self.successor),
                     '--reason', REASON)
        self.assertEqual((report['status'], report['records_deleted']), ('retired', 0))
        walked = cli(chain_command, '--journal', str(self.successor))
        self.assertEqual(walked['journal_count'], 2)
        self.assertEqual(walked['journals'][0]['predecessor'], str(self.first))
        usage = cli(prefix + '-journal-usage', '--journal', str(self.first))
        self.assertEqual(usage['journal_status'], 'retired')
        cli(chain_command, '--journal', str(self.root / 'absent.sqlite'), expect=2)
        self.journal = self.open(self.first)


class GitHubSuccessionTests(SuccessionBase, unittest.TestCase):
    label = 'GitHub'

    def open(self, path, read_only=False):
        return GitHubJournal(path, read_only=read_only)

    def stage(self, journal, number):
        saved = journal.stage(proposal(number), 'example/project', 123)
        return saved['operation_id'], saved['sha256']

    def state(self, record):
        return record['state']

    def backup(self, path, destination):
        return github_backup(path, destination)

    def compare(self, path, destination, expected):
        return github_compare(path, destination, expected)

    def drill(self, destination, expected):
        return github_drill(destination, expected)


class JiraSuccessionTests(SuccessionBase, unittest.TestCase):
    label = 'Jira'

    def open(self, path, read_only=False):
        return JiraJournal(path, read_only=read_only)

    def stage(self, journal, number):
        config = profile()
        proposed = jira_intent('create_issue', number)
        observed = jira_capture(proposed, config)
        preview = preview_action(proposed, config, observed)
        saved = journal.stage_action(proposed, config, observed, preview['sha256'])
        return saved['binding']['operation_id'], saved['binding']['sha256']

    def state(self, record):
        return record['binding']['state']

    def backup(self, path, destination):
        return jira_backup(path, destination)

    def compare(self, path, destination, expected):
        return jira_compare(path, destination, expected)

    def drill(self, destination, expected):
        return jira_drill(destination, expected)


if __name__ == '__main__':
    unittest.main()
