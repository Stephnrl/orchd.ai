import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_pilot as fixtures
from orch.api import Application
from orch.batches import Batches
from orch.contracts import Rejected, digest
from orch.engine import Engine
from orch.execution import Executor
from orch.maintenance import audit, backup, restore
from orch.pilot import Pilot
from orch.repository_tasks import RepositoryTasks


class RepositoryTaskTests(unittest.TestCase):
    git = fixtures.PilotTests.git

    def setUp(self):
        fixtures.PilotTests.setUp(self)
        self.pilot.close()
        self.engine = Engine(self.root / 'tasks', Executor('trusted-fixture'))

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def create(self):
        return self.engine.create_repository_task(self.intent)

    def approve(self, task, decision='approve'):
        state = self.engine.task(task)['state']
        return self.engine.approve(task, state['pending_approval']['id'], decision, state['revision'])

    def test_two_approval_lifecycle_retained_snapshot_and_replay(self):
        task = self.create()
        self.assertEqual(self.engine.run(task)['state']['state'], 'AWAITING_PLAN_APPROVAL')
        self.approve(task)
        self.assertEqual(self.engine.run(task)['state']['state'], 'AWAITING_ACTION_APPROVAL')
        self.approve(task)
        current = self.engine.task(task)
        self.assertEqual(current['state']['state'], 'COMPLETED')
        self.assertEqual(current['context']['completion_semantics'], 'human_accepted_local_snapshot')
        self.assertIsNone(current['state']['patch'])
        self.assertNotIn('external', current['context'])
        self.assertEqual(self.engine.store.db.execute("SELECT count(*) FROM records WHERE kind IN ('ExternalActionReceipt','AgentInvocation')").fetchone()[0], 0)
        replay = self.engine.store.reconstruct(task)
        self.assertEqual(replay['state'], current['state'])
        audit(self.engine.store)
        self.engine.close()
        self.engine = Engine(self.root / 'tasks', Executor('trusted-fixture'))
        self.assertEqual(self.engine.run(task), current)
        self.assertEqual((self.repo / 'config.json').read_bytes(), b'{"enabled":false}\n')

    def test_standalone_pilot_run_cannot_bypass_kernel_approval(self):
        task = self.create()
        state, context = self.engine.store.task(task)
        plan, scope = RepositoryTasks(self.engine)._scope(state, context)
        pilot = Pilot(self.engine.store.root / 'repository-pilots')
        try:
            with self.assertRaises(Rejected): pilot.run(scope['id'], plan['scope_sha256'], 0)
            self.approve(task)
            with self.assertRaises(Rejected): pilot.run(scope['id'], plan['scope_sha256'], 0)
            self.assertEqual(pilot.inspect(scope['id'])['status'], 'prepared')
        finally: pilot.close()

    def test_stale_and_cross_task_approval_and_batch_rejection(self):
        task, other = self.create(), self.create()
        state = self.engine.task(task)['state']
        for target, revision in [(other, state['revision']), (task, state['revision']+1)]:
            with self.assertRaises(Rejected): self.engine.approve(target, state['pending_approval']['id'], 'approve', revision)
        self.approve(task)
        state = self.engine.task(task)['state']
        with self.assertRaises(Rejected): Batches(self.engine).create({'tasks': [{'task_id': task, 'expected_revision': state['revision']}]})

    def test_reject_plan_or_result_never_creates_remote_effect(self):
        task = self.create()
        self.approve(task, 'reject')
        self.assertEqual(self.engine.run(task)['state']['state'], 'CANCELLED')
        task = self.create()
        self.approve(task)
        self.engine.run(task)
        self.approve(task, 'reject')
        self.assertEqual(self.engine.run(task)['state']['state'], 'CANCELLED')

    def test_failed_assertion_cannot_reach_result_acceptance(self):
        self.intent['checks'][0]['equals'] = False
        task = self.create()
        self.approve(task)
        self.assertEqual(self.engine.run(task)['state']['state'], 'FAILED')
        self.assertIsNone(self.engine.task(task)['state']['pending_approval'])

    def test_crash_before_pilot_dispatch_blocks_then_abandons(self):
        task = self.create()
        self.approve(task)
        with patch('orch.repository_tasks.Pilot.run', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError): self.engine.run(task)
        self.assertEqual(self.engine.task(task)['state']['state'], 'IMPLEMENTING')
        self.assertEqual(self.engine.run(task)['state']['state'], 'BLOCKED')
        state = self.engine.task(task)['state']
        self.assertEqual(self.engine.recover(task, state['revision'])['state']['state'], 'FAILED')
        self.assertEqual(self.engine.run(task)['state']['state'], 'FAILED')
        audit(self.engine.store)

    def test_partial_local_data_run_reconciles_only_after_writer_lock_released(self):
        task = self.create()
        self.approve(task)
        with patch('orch.pilot.evaluate', side_effect=RuntimeError('Interrupted local check')):
            with self.assertRaises(RuntimeError): self.engine.run(task)
        self.engine.run(task)
        state = self.engine.task(task)['state']
        result = self.engine.recover(task, state['revision'])
        self.assertEqual(result['state']['state'], 'FAILED')
        report = self.engine.store.get(result['context']['repository_result'], task)
        self.assertEqual(json.loads(self.engine.store.read_artifact(report['report'], task))['status'], 'interrupted')

    def test_crash_after_pilot_effect_never_adopts_result_automatically(self):
        task = self.create()
        self.approve(task)
        run = Pilot.run
        def crash(pilot, *args):
            run(pilot, *args)
            raise RuntimeError('crash after pilot committed')
        with patch('orch.repository_tasks.Pilot.run', new=crash):
            with self.assertRaises(RuntimeError): self.engine.run(task)
        self.engine.run(task)
        state = self.engine.task(task)['state']
        result = self.engine.recover(task, state['revision'])
        self.assertEqual(result['state']['state'], 'FAILED')
        self.assertEqual(self.engine.store.get(result['context']['repository_result'], task)['outcome'], 'interrupted')

    def test_completed_checkpoint_survives_crash_without_rerun(self):
        task = self.create()
        self.approve(task)
        self.engine.after_operation = lambda _: (_ for _ in ()).throw(RuntimeError('crash after kernel commit'))
        with self.assertRaises(RuntimeError): self.engine.run(task)
        self.engine.after_operation = None
        with patch('orch.repository_tasks.Pilot.run') as run:
            self.assertEqual(self.engine.run(task)['state']['state'], 'AWAITING_ACTION_APPROVAL')
            run.assert_not_called()

    def test_missing_pilot_journal_blocks_without_creating_a_replacement(self):
        task = self.create()
        self.approve(task)
        journal = self.engine.store.root / 'repository-pilots/pilot.sqlite'
        journal.rename(journal.with_suffix('.retained'))
        with self.assertRaises(Rejected): self.engine.run(task)
        self.assertFalse(journal.exists())

    def test_cancel_and_direct_fixture_transition_are_fenced(self):
        task = self.create()
        state, context = self.engine.store.task(task)
        with self.assertRaises(Rejected): self.engine._transition(state, context, 'COMPLETED')
        result = self.engine.cancel(task, state['revision'], 'Cancel unused task')
        self.assertEqual(result['state']['state'], 'CANCELLED')
        with self.assertRaises(Rejected): self.engine.retry_cleanup(task, 'a'*32, result['state']['revision'])

    def test_immutable_result_artifact_is_required_for_acceptance(self):
        task = self.create()
        self.approve(task)
        self.engine.run(task)
        context = self.engine.task(task)['context']
        result = self.engine.store.get(context['repository_result'], task)
        blob = self.engine.store.root / 'artifacts' / result['snapshots'][0]['content']['sha256']
        blob.write_bytes(b'{}')
        with self.assertRaises(Rejected): self.approve(task)

    def test_common_backup_retains_result_without_runtime_journal(self):
        task = self.create()
        self.approve(task)
        self.engine.run(task)
        self.approve(task)
        destination = self.root / 'backup'
        backup(self.engine.store, destination)
        self.assertTrue((destination / 'orch.sqlite').is_file())
        self.assertFalse((destination / 'repository-pilots').exists())

    def test_authenticated_api_creation_and_existing_task_controls(self):
        app = Application(self.engine)
        # The same in-process credential used by existing API tests.
        token = app.session
        status, _ = app.dispatch('POST', '/repository-tasks', {'title': 'Repository task', 'intent': self.intent}, 'bad-token')
        self.assertEqual(status, 401)
        status, created = app.dispatch('POST', '/repository-tasks', {'title': 'Repository task', 'intent': self.intent}, token)
        self.assertEqual(status, 201)
        task, state = created['state']['task_id'], created['state']
        status, _ = app.dispatch('POST', f'/tasks/{task}/approvals', {'request_id': state['pending_approval']['id'], 'decision': 'approve', 'expected_revision': state['revision']}, token)
        self.assertEqual(status, 200)
        status, result = app.dispatch('POST', f'/tasks/{task}/run', {}, token)
        self.assertEqual(status, 200)
        self.assertEqual(result['state']['state'], 'AWAITING_ACTION_APPROVAL')

    def test_expired_execution_request_requires_new_scope(self):
        from datetime import datetime, timezone
        with patch('orch.pilot.datetime') as clock:
            clock.now.return_value = datetime(2000, 1, 1, tzinfo=timezone.utc)
            task = self.create()
        state = self.engine.task(task)['state']
        with self.assertRaises(Rejected): self.approve(task)
        with self.assertRaises(Rejected): self.engine.renew_approval(task, state['pending_approval']['id'], state['revision'])

    def test_expired_result_can_be_renewed_without_rerunning(self):
        from datetime import datetime, timezone
        task = self.create()
        self.approve(task)
        # Only the result-approval clock is moved; worker scope remains current.
        with patch('orch.repository_tasks.datetime') as clock:
            clock.now.return_value = datetime(2000, 1, 1, tzinfo=timezone.utc)
            clock.fromisoformat = datetime.fromisoformat
            self.engine.run(task)
        state = self.engine.task(task)['state']
        with self.assertRaises(Rejected): self.approve(task)
        with patch('orch.repository_tasks.Pilot.run') as run:
            renewed = self.engine.renew_approval(task, state['pending_approval']['id'], state['revision'])
            run.assert_not_called()
        self.assertNotEqual(renewed['state']['pending_approval'], state['pending_approval'])
        self.approve(task)
        self.assertEqual(self.engine.task(task)['state']['state'], 'COMPLETED')

    def test_restore_preserves_inspection_but_cannot_execute_original_scope(self):
        task = self.create()
        self.approve(task)
        backup(self.engine.store, self.root / 'backup')
        restore(self.root / 'backup', self.root / 'restored')
        restored = Engine(self.root / 'restored', Executor('trusted-fixture'))
        try:
            self.assertEqual(restored.task(task), self.engine.task(task))
            with self.assertRaises(Rejected): restored.run(task)
            self.assertFalse((self.root / 'restored/repository-pilots').exists())
        finally: restored.close()

    def test_source_change_blocks_admission_and_allows_safe_recovery(self):
        task = self.create()
        self.approve(task)
        with patch('orch.pilot.source_hash', return_value='f'*64):
            self.assertEqual(self.engine.run(task)['state']['state'], 'BLOCKED')
        state = self.engine.task(task)['state']
        self.assertEqual(self.engine.recover(task, state['revision'])['state']['state'], 'FAILED')
