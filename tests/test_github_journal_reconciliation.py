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

from orch.contracts import Rejected, digest
from orch.github_journal import GitHubJournal
from test_github_preview import intent
from test_github_reconcile import candidate, seen


class JournalReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'journal.sqlite'
        self.source = intent()
        journal = GitHubJournal(self.path)
        try:
            self.saved = journal.stage(self.source, 'example/project', 123)
        finally:
            journal.close()

    def tearDown(self):
        self.temp.cleanup()

    def reserve(self):
        journal = GitHubJournal(self.path)
        try:
            self.saved = journal.reserve(self.source['operation_id'], self.saved['sha256'])
        finally:
            journal.close()

    def assess(self, observations, expected=None):
        journal = GitHubJournal(self.path, read_only=True)
        try:
            return journal.reconcile(self.source['operation_id'], expected or self.saved['sha256'], observations)
        finally:
            journal.close()

    def test_report_binds_retained_reservation_and_complete_assessment(self):
        self.reserve()
        observations = seen(self.source, [candidate(self.source)])
        original = copy.deepcopy(observations)
        before = self.path.read_bytes()
        report = self.assess(observations)
        self.assertEqual(report['status'], 'candidate_observed')
        binding = report['binding']
        for key in ('operation_id', 'task_id', 'state', 'reserved_at'):
            self.assertEqual(binding[key], self.saved[key])
        self.assertEqual(binding['scope_sha256'], self.saved['sha256'])
        self.assertEqual(binding['assessment']['binding']['repository_id'], 123)
        self.assertEqual(binding['assessment']['binding']['observations_sha256'], digest(observations))
        self.assertEqual(report['sha256'], digest(binding))
        altered = copy.deepcopy(binding)
        altered['assessment']['candidate']['number'] += 1
        self.assertNotEqual(report['sha256'], digest(altered))
        for key in ('retry_allowed', 'live_authorized', 'remote_effect_confirmed'):
            self.assertIs(report[key], False)
        self.assertEqual(observations, original)
        self.assertEqual(self.path.read_bytes(), before)
        journal = GitHubJournal(self.path)
        try:
            self.assertEqual(journal.get(self.source['operation_id']), self.saved)
            with self.assertRaises(Rejected):
                journal.reserve(self.source['operation_id'], self.saved['sha256'])
        finally:
            journal.close()

    def test_prepared_and_wrong_digest_reject_before_classification(self):
        observations = seen(self.source, [])
        with patch('orch.github_reconcile.reconcile_pull_request', side_effect=AssertionError('Must reject before classification')):
            with self.assertRaises(Rejected):
                self.assess(observations)
            self.reserve()
            with self.assertRaises(Rejected):
                self.assess(observations, 'f' * 64)

    def test_unresolved_observations_never_reset_reservation(self):
        self.reserve()
        other = {**self.source, 'title': 'Different proposal'}
        observations = [seen(self.source, []), seen(self.source, [candidate(self.source)] * 2), seen(other, [candidate(other)])]
        foreign = candidate(self.source)
        foreign['head']['repo']['id'] = 999
        observations.append(seen(self.source, [foreign]))
        before = self.path.read_bytes()
        for value in observations:
            with self.subTest(value=value):
                report = self.assess(value)
                self.assertEqual(report['status'], 'unresolved')
                self.assertIsNone(report['binding']['assessment']['candidate'])
                self.assertIs(report['retry_allowed'], False)
                self.assertEqual(self.path.read_bytes(), before)

    def test_corrupt_record_rejects_before_classification(self):
        self.reserve()
        db = sqlite3.connect(self.path)
        try:
            db.execute('DROP TRIGGER immutable_scope')
            db.execute('DROP TRIGGER one_reservation')
            db.execute("UPDATE intents SET sha256='corrupt'")
            db.commit()
        finally:
            db.close()
        with patch('orch.github_reconcile.reconcile_pull_request', side_effect=AssertionError('No classification')):
            with self.assertRaises(Rejected):
                self.assess(seen(self.source, []))

    def cli(self, extra=()):
        from orch.__main__ import main
        output = io.StringIO()
        argv = ['orch', 'github-reconcile-journal', '--journal', str(self.path), '--operation-id', self.source['operation_id'], '--expected-sha256', self.saved['sha256'], '--observations', str(self.path.parent / 'observations.json'), *extra]
        with patch.object(sys, 'argv', argv), redirect_stdout(output), redirect_stderr(io.StringIO()), patch('socket.socket', side_effect=AssertionError('No network')), patch('subprocess.Popen', side_effect=AssertionError('No process')), patch('orch.__main__.Engine', side_effect=AssertionError('No workflow')):
            with self.assertRaises(SystemExit) as result:
                main()
        return result.exception.code, output.getvalue()

    def test_cli_read_only_success_unresolved_and_override_rejection(self):
        self.reserve()
        before = self.path.read_bytes()
        for rows, code in (([], 2), ([candidate(self.source)], 0)):
            (self.path.parent / 'observations.json').write_text(json.dumps(seen(self.source, rows)))
            result, output = self.cli()
            self.assertEqual(result, code)
            self.assertFalse(json.loads(output)['retry_allowed'])
        for extra in (['--intent', 'other.json'], ['--allow-repository', 'other/repo'], ['--repository-id', '999']):
            self.assertEqual(self.cli(extra), (2, ''))
        self.assertEqual(self.path.read_bytes(), before)

    def test_cli_invalid_input_and_missing_journal_have_no_effects(self):
        self.reserve()
        before = self.path.read_bytes()
        observation_path = self.path.parent / 'observations.json'
        for raw in ('{}', '{"pages":[],"pages":[]}', 'x' * 65537):
            observation_path.write_text(raw)
            self.assertEqual(self.cli(), (2, ''))
            self.assertEqual(self.path.read_bytes(), before)
        observation_path.write_text(json.dumps(seen(self.source, [])))
        self.path = self.path.parent / 'missing' / 'journal.sqlite'
        self.assertEqual(self.cli(), (2, ''))
        self.assertFalse(self.path.parent.exists())
