import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from orch.contracts import Rejected
from orch.pilot import Pilot
from orch import pilot_retention
from orch.pilot_retention import usage, reclaim, eligibility, reclamation


REASON = 'Reviewed after retained evidence was archived'


class PilotRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init')
        (self.repo / 'config.json').write_bytes(b'{"enabled":false}\n')
        self.git('add', 'config.json')
        self.git('-c', 'user.name=Pilot', '-c', 'user.email=pilot@example.invalid',
                 '-c', 'commit.gpgsign=false', 'commit', '-m', 'Disposable baseline')
        self.commit = self.git('rev-parse', 'HEAD').strip()
        self.pilot = Pilot(self.root / 'journal')
        self.intent = {'repository': str(self.repo), 'base_commit': self.commit,
                       'replacements': {'config.json': '{"enabled":true}\n'},
                       'checks': [{'path': 'config.json', 'keys': ['enabled'], 'equals': True}]}

    def git(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0')
        return subprocess.check_output(['git', '-c', 'core.autocrlf=false', '-c', 'core.hooksPath='+os.devnull,
                                        '-C', str(self.repo), *args], env=env, stderr=subprocess.DEVNULL).decode()

    def tearDown(self):
        self.pilot.close()
        self.tmp.cleanup()

    def passed(self):
        prepared = self.pilot.prepare(self.intent)
        return self.pilot.run(prepared['id'], prepared['scope_sha256'], 0)

    def test_usage_is_read_only_and_reports_eligibility(self):
        prepared = self.pilot.prepare(self.intent)
        done = self.passed()
        self.pilot.close()
        self.pilot = Pilot(self.root / 'journal', read_only=True)
        report = usage(self.pilot)
        self.assertEqual((report['journal_rows'], report['live_pilots'], report['reclaimable']), (2, 2, 1))
        self.assertEqual(report['live_headroom'], pilot_retention.LIVE_CAP - 2)
        self.assertEqual(report['status_counts'], {'passed': 1, 'prepared': 1})
        by_id = {p['id']: p for p in report['pilots']}
        self.assertFalse(by_id[prepared['id']]['eligible'])
        self.assertIn('abandon it first', by_id[prepared['id']]['reasons'][0])
        self.assertTrue(by_id[done['id']]['eligible'])
        self.assertEqual(by_id[done['id']]['workspace_bytes'], len('{"enabled":true}\n'))
        self.assertEqual(report['workspace_bytes'], len('{"enabled":true}\n'))
        with self.assertRaises(Exception): reclaim(self.pilot, done['id'], done['scope_sha256'], 2, reason=REASON)

    def test_dry_run_writes_nothing_and_reclaim_retains_evidence(self):
        done = self.passed()
        workspace = self.pilot.root / done['id']
        plan = reclaim(self.pilot, done['id'], done['scope_sha256'], 2, dry_run=True)
        self.assertEqual((plan['dry_run'], plan['recorded'], plan['deleted'], plan['files']), (True, False, False, ['config.json']))
        self.assertTrue(workspace.is_dir())
        self.assertIsNone(self.pilot.inspect(done['id'])['reclamation'])
        result = reclaim(self.pilot, done['id'], done['scope_sha256'], 2, reason=REASON)
        self.assertEqual((result['recorded'], result['deleted'], result['reclamation']), (True, True, 'reclaimed'))
        self.assertFalse(workspace.exists())
        after = self.pilot.inspect(done['id'])
        self.assertEqual((after['status'], after['revision'], after['reclamation']), ('passed', 2, 'reclaimed'))
        self.assertEqual(after['receipt'], done['receipt'])
        self.assertFalse(after['matches_receipt'])
        self.assertEqual(after['observed_files'], {})
        self.assertEqual((self.repo / 'config.json').read_bytes(), b'{"enabled":false}\n')
        with self.assertRaisesRegex(Rejected, 'already reclaimed'): reclaim(self.pilot, done['id'], done['scope_sha256'], 2, reason=REASON)
        self.pilot.close()
        self.pilot = Pilot(self.root / 'journal')
        self.assertEqual(self.pilot.inspect(done['id']), after)

    def test_stale_review_and_non_terminal_pilots_are_refused(self):
        prepared = self.pilot.prepare(self.intent)
        with self.assertRaisesRegex(Rejected, 'abandon it first'): reclaim(self.pilot, prepared['id'], prepared['scope_sha256'], 0, reason=REASON)
        self.assertEqual(self.pilot.inspect(prepared['id'])['status'], 'prepared')
        done = self.passed()
        with self.assertRaisesRegex(Rejected, 'inapplicable'): reclaim(self.pilot, done['id'], done['scope_sha256'], 1, reason=REASON)
        with self.assertRaisesRegex(Rejected, 'inapplicable'): reclaim(self.pilot, done['id'], 'a' * 64, 2, reason=REASON)
        with self.assertRaises(Rejected): reclaim(self.pilot, done['id'], done['scope_sha256'], '2', reason=REASON)
        with patch('orch.pilot.evaluate', side_effect=RuntimeError('Injected process interruption')):
            interrupted = self.pilot.prepare(self.intent)
            with self.assertRaises(RuntimeError): self.pilot.run(interrupted['id'], interrupted['scope_sha256'], 0)
        self.assertEqual(self.pilot.inspect(interrupted['id'])['status'], 'reserved')
        reasons, _ = eligibility(self.pilot, interrupted['id'])
        self.assertIn('reconciliation first', reasons[0])
        with self.assertRaisesRegex(Rejected, 'inapplicable'): reclaim(self.pilot, interrupted['id'], interrupted['scope_sha256'], 1, reason=REASON)
        reconciled = self.pilot.reconcile(interrupted['id'], interrupted['scope_sha256'], 1)
        self.assertEqual(reconciled['status'], 'interrupted')
        result = reclaim(self.pilot, interrupted['id'], reconciled['scope_sha256'], reconciled['revision'], reason=REASON)
        self.assertTrue(result['deleted'])
        self.assertFalse((self.pilot.root / interrupted['id']).exists())

    def test_abandoned_pilot_without_workspace_is_reclaimable(self):
        prepared = self.pilot.prepare(self.intent)
        abandoned = self.pilot.abandon(prepared['id'], prepared['scope_sha256'], 0)
        result = reclaim(self.pilot, prepared['id'], abandoned['scope_sha256'], 1, reason=REASON)
        self.assertEqual((result['files'], result['bytes'], result['deleted']), ([], 0, True))
        self.assertEqual(self.pilot.inspect(prepared['id'])['reclamation'], 'reclaimed')

    def test_tampered_workspace_is_retained_for_investigation(self):
        done = self.passed()
        workspace = self.pilot.root / done['id']
        (workspace / 'extra.txt').write_bytes(b'evidence')
        reasons, _ = eligibility(self.pilot, done['id'])
        self.assertIn('unexpected entries', reasons[0])
        with self.assertRaisesRegex(Rejected, 'not reclaimable'): reclaim(self.pilot, done['id'], done['scope_sha256'], 2, reason=REASON)
        self.assertTrue((workspace / 'extra.txt').exists())
        (workspace / 'extra.txt').unlink()
        try: (workspace / 'link.json').symlink_to(self.repo / 'config.json')
        except OSError: pass  # Symlink creation needs a privilege some Windows accounts lack; the extra-file case above still ran.
        else:
            with self.assertRaises(Rejected): reclaim(self.pilot, done['id'], done['scope_sha256'], 2, reason=REASON)
            self.assertTrue((self.repo / 'config.json').exists())
            (workspace / 'link.json').unlink()
        self.assertTrue(reclaim(self.pilot, done['id'], done['scope_sha256'], 2, reason=REASON)['deleted'])

    def test_read_only_workspace_files_are_reclaimed(self):
        # Docker workers leave frozen 0444 files; reclamation must still delete them on every host.
        done = self.passed()
        workspace = self.pilot.root / done['id']
        (workspace / 'config.json').chmod(0o444)
        self.assertTrue(reclaim(self.pilot, done['id'], done['scope_sha256'], 2, reason=REASON)['deleted'])
        self.assertFalse(workspace.exists())
        self.assertEqual(self.pilot.inspect(done['id'])['reclamation'], 'reclaimed')

    def test_crash_after_intent_is_visible_and_resumable(self):
        done = self.passed()
        workspace = self.pilot.root / done['id']
        with patch('pathlib.Path.unlink', side_effect=OSError('Injected crash before deletion')):
            with self.assertRaises(OSError): reclaim(self.pilot, done['id'], done['scope_sha256'], 2, reason=REASON)
        self.assertTrue((workspace / 'config.json').exists())
        self.assertEqual(self.pilot.inspect(done['id'])['reclamation'], 'reclaiming')
        report = usage(self.pilot)
        entry = report['pilots'][0]
        self.assertEqual((entry['reclamation'], entry['eligible'], report['live_pilots']), ('reclaiming', True, 1))
        (workspace / 'config.json').write_bytes(b'changed after intent')
        with self.assertRaisesRegex(Rejected, 'changed after reclamation started'): reclaim(self.pilot, done['id'], done['scope_sha256'], 2, reason=REASON)
        (workspace / 'config.json').write_bytes(b'{"enabled":true}\n')
        resumed = reclaim(self.pilot, done['id'], done['scope_sha256'], 2, reason=REASON)
        self.assertEqual((resumed['deleted'], resumed['reclamation']), (True, 'reclaimed'))
        self.assertFalse(workspace.exists())
        self.assertEqual(usage(self.pilot)['live_pilots'], 0)

    def test_reclamation_releases_live_capacity_but_not_journal_capacity(self):
        with patch.object(pilot_retention, 'LIVE_CAP', 1):
            done = self.passed()
            with self.assertRaisesRegex(Rejected, 'retention cap'): self.pilot.prepare(self.intent)
            reclaim(self.pilot, done['id'], done['scope_sha256'], 2, reason=REASON)
            prepared = self.pilot.prepare(self.intent)
            self.assertEqual(usage(self.pilot)['live_pilots'], 1)
        with patch.object(pilot_retention, 'JOURNAL_CAP', 2), patch.object(pilot_retention, 'LIVE_CAP', 1):
            abandoned = self.pilot.abandon(prepared['id'], prepared['scope_sha256'], 0)
            reclaim(self.pilot, prepared['id'], abandoned['scope_sha256'], 1, reason=REASON)
            with self.assertRaisesRegex(Rejected, 'journal cap'): self.pilot.prepare(self.intent)

    def test_reclamation_record_tampering_is_rejected(self):
        done = self.passed()
        reclaim(self.pilot, done['id'], done['scope_sha256'], 2, reason=REASON)
        self.pilot.db.execute("UPDATE pilot_reclamations SET status='reclaiming' WHERE id=?", (done['id'],))
        with self.assertRaisesRegex(Rejected, 'Reclamation record integrity'): self.pilot.inspect(done['id'])
        self.assertFalse(usage(self.pilot)['pilots'][0]['eligible'])

    def test_legacy_journal_without_reclamation_table(self):
        done = self.passed()
        self.pilot.db.execute('DROP TABLE pilot_reclamations')
        self.pilot.close()
        self.pilot = Pilot(self.root / 'journal', read_only=True)
        self.assertIsNone(self.pilot.inspect(done['id'])['reclamation'])
        self.assertEqual(usage(self.pilot)['live_pilots'], 1)
        self.pilot.close()
        self.pilot = Pilot(self.root / 'journal')
        self.assertTrue(reclaim(self.pilot, done['id'], done['scope_sha256'], 2, reason=REASON)['deleted'])

    def test_cli_usage_and_reclaim(self, reason=REASON):
        done = self.passed()
        env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1])}
        def cli(*args):
            return json.loads(subprocess.check_output([os.sys.executable, '-m', 'orch', *args, '--data', str(self.pilot.root)], env=env))
        report = cli('pilot-usage')
        self.assertEqual(report['reclaimable'], 1)
        plan = cli('pilot-reclaim', '--pilot-id', done['id'], '--expected-sha256', done['scope_sha256'], '--expected-revision', '2', '--dry-run')
        self.assertFalse(plan['recorded'])
        self.assertTrue((self.pilot.root / done['id']).exists())
        with self.assertRaises(subprocess.CalledProcessError):  # A live reclamation needs its audited reason.
            subprocess.check_output([os.sys.executable, '-m', 'orch', 'pilot-reclaim', '--pilot-id', done['id'], '--expected-sha256', done['scope_sha256'],
                                     '--expected-revision', '2', '--data', str(self.pilot.root)], env=env, stderr=subprocess.DEVNULL)
        result = cli('pilot-reclaim', '--pilot-id', done['id'], '--expected-sha256', done['scope_sha256'], '--expected-revision', '2', '--reason', REASON)
        self.assertTrue(result['deleted'])
        self.assertEqual(result['reason'], REASON)
        with self.assertRaises(subprocess.CalledProcessError):
            subprocess.check_output([os.sys.executable, '-m', 'orch', 'pilot-reclaim', '--pilot-id', done['id'], '--expected-sha256', done['scope_sha256'],
                                     '--data', str(self.pilot.root)], env=env, stderr=subprocess.DEVNULL)


class ReclaimReasonTests(unittest.TestCase):
    """A live reclamation carries one audited reason, recorded with its durable intent."""
    setUp, tearDown, git, passed = PilotRetentionTests.setUp, PilotRetentionTests.tearDown, PilotRetentionTests.git, PilotRetentionTests.passed

    def test_reason_is_required_validated_and_retained_across_resume(self):
        done = self.passed()
        for bad in (None, '', '   ', 'x' * 501, 'line\nbreak', 'token=secret-value', 7):
            with self.subTest(bad=bad), self.assertRaises(Rejected):
                reclaim(self.pilot, done['id'], done['scope_sha256'], 2, reason=bad)
        self.assertIsNone(reclamation(self.pilot, done['id']))
        self.assertTrue((self.pilot.root / done['id'] / 'config.json').exists())
        preview = reclaim(self.pilot, done['id'], done['scope_sha256'], 2, dry_run=True)
        self.assertIsNone(preview['reason'])
        with self.assertRaises(Rejected): reclaim(self.pilot, done['id'], done['scope_sha256'], 2, dry_run=True, reason='')
        with patch('pathlib.Path.unlink', side_effect=OSError('Injected crash before deletion')):
            with self.assertRaises(OSError): reclaim(self.pilot, done['id'], done['scope_sha256'], 2, reason='  First review  ')
        record = reclamation(self.pilot, done['id'])
        self.assertEqual((record['status'], record['reason'], record['principal']), ('reclaiming', 'First review', 'local-operator'))
        resumed = reclaim(self.pilot, done['id'], done['scope_sha256'], 2, reason='Second review')
        self.assertTrue(resumed['deleted'])
        self.assertEqual(resumed['reason'], 'First review')
        self.assertEqual(reclamation(self.pilot, done['id'])['reason'], 'First review')


class KernelBoundRetentionTests(unittest.TestCase):
    """Reclamation of task-bound pilots waits for the kernel task, never the reverse."""
    git = PilotRetentionTests.git

    def setUp(self):
        PilotRetentionTests.setUp(self)
        self.pilot.close()
        from orch.engine import Engine
        from orch.execution import Executor
        self.engine = Engine(self.root / 'tasks', Executor('trusted-fixture'))

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def approve(self, task):
        state = self.engine.task(task)['state']
        return self.engine.approve(task, state['pending_approval']['id'], 'approve', state['revision'])

    def journal(self, read_only=False):
        return Pilot(self.engine.store.root / 'repository-pilots', read_only=read_only)

    def bound(self, task):
        state, _ = self.engine.store.task(task)
        plan = self.engine.store.get(state['plan'], task, 'RepositoryTaskPlan')
        return plan['pilot_id'], plan['scope_sha256']

    def test_bound_pilot_waits_for_terminal_task_and_retains_kernel_snapshots(self):
        task = self.engine.create_repository_task(self.intent)
        self.engine.run(task)
        self.approve(task)
        self.engine.run(task)
        pilot_id, scope_hash = self.bound(task)
        pilot = self.journal()
        try:
            self.assertEqual(pilot.inspect(pilot_id)['status'], 'passed')
            reasons, bound = eligibility(pilot, pilot_id)
            self.assertEqual(reasons, ['Bound kernel task is not terminal'])
            self.assertEqual(bound['state'], 'AWAITING_ACTION_APPROVAL')
            with self.assertRaisesRegex(Rejected, 'not terminal'): reclaim(pilot, pilot_id, scope_hash, 2, reason=REASON)
            self.approve(task)
            self.assertEqual(self.engine.task(task)['state']['state'], 'COMPLETED')
            result = reclaim(pilot, pilot_id, scope_hash, 2, reason=REASON)
            self.assertTrue(result['deleted'])
            record = pilot_retention.reclamation(pilot, pilot_id)
            self.assertEqual(record['task'], {'task_id': task, 'state': 'COMPLETED', 'revision': self.engine.task(task)['state']['revision']})
        finally: pilot.close()
        current = self.engine.task(task)
        result = self.engine.store.get(current['context']['repository_result'], task, 'RepositoryTaskResult')
        for item in result['snapshots']:
            self.assertEqual(self.engine.store.read_artifact(item['content'], task), self.intent['replacements'][item['path']])
        self.assertEqual(self.engine.store.reconstruct(task)['state'], current['state'])
        self.assertEqual(self.engine.run(task), current)

    def test_cancelled_task_pilot_is_reclaimable_and_missing_store_blocks(self):
        task = self.engine.create_repository_task(self.intent)
        self.engine.run(task)
        state = self.engine.task(task)['state']
        self.engine.approve(task, state['pending_approval']['id'], 'reject', state['revision'])
        self.assertEqual(self.engine.task(task)['state']['state'], 'CANCELLED')
        pilot_id, scope_hash = self.bound(task)
        pilot = self.journal()
        try:
            report = pilot.inspect(pilot_id)
            self.assertEqual(report['status'], 'prepared')  # The kernel never told the journal.
            plan = reclaim(pilot, pilot_id, scope_hash, 0, dry_run=True)
            self.assertTrue(plan['abandons'])
            self.assertEqual(pilot.inspect(pilot_id)['status'], 'prepared')
            self.assertTrue(reclaim(pilot, pilot_id, scope_hash, 0, reason=REASON)['deleted'])
            after = pilot.inspect(pilot_id)
            self.assertEqual((after['status'], after['revision'], after['reclamation']), ('abandoned', 1, 'reclaimed'))
            with self.assertRaises(Rejected): pilot.run(pilot_id, scope_hash, 0)
        finally: pilot.close()
        other = self.engine.create_repository_task(self.intent)
        other_id, other_hash = self.bound(other)
        self.engine.cancel(other, self.engine.task(other)['state']['revision'], 'operator stop')
        self.engine.close()
        (self.root / 'tasks' / 'orch.sqlite').rename(self.root / 'tasks' / 'orch.sqlite.moved')
        pilot = self.journal()
        try:
            reasons, bound = eligibility(pilot, other_id)
            self.assertIsNone(bound)
            self.assertTrue(reasons and reasons[0].startswith('Bound kernel task state unavailable'))
            with self.assertRaises(Rejected): reclaim(pilot, other_id, other_hash, pilot.inspect(other_id)['revision'])
        finally: pilot.close()
        (self.root / 'tasks' / 'orch.sqlite.moved').rename(self.root / 'tasks' / 'orch.sqlite')
        from orch.engine import Engine
        from orch.execution import Executor
        self.engine = Engine(self.root / 'tasks', Executor('trusted-fixture'))


if __name__ == '__main__':
    unittest.main()
