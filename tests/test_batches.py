import copy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from orch.batches import Batches, snapshot
from orch.broker import file_lock
from orch.contracts import Rejected, canonical, digest
from orch.engine import Engine
from orch.execution import Executor


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = Engine(self.tmp.name, Executor('trusted-fixture'))
        self.batches = Batches(self.engine)
        self.tasks = [self.engine.create_task('Batch fixture '+str(i)) for i in range(2)]

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def create(self, tasks=None):
        return self.batches.create({'tasks':[{'task_id': t, 'expected_revision':self.engine.task(t)['state']['revision']} for t in (tasks or self.tasks)]})

    def run_batch(self, batch):
        return self.batches.run(batch['id'], batch['scope_sha256'], batch['revision'])

    def approve(self, task):
        state = self.engine.task(task)['state']
        self.engine.approve(task, state['pending_approval']['id'], 'approve', state['revision'])

    def test_creation_inspection_and_restart_do_not_execute(self):
        before = [self.engine.task(t) for t in self.tasks]
        batch = self.create()
        raw = self.batches._path(batch['id']).read_bytes()
        self.engine.close()
        self.engine = Engine(self.tmp.name, Executor('trusted-fixture'))
        self.batches = Batches(self.engine)
        report = self.batches.inspect(batch['id'])
        self.assertEqual([self.engine.task(t) for t in self.tasks], before)
        self.assertTrue(all(o['matches_checkpoint'] for o in report['observations']))
        self.assertFalse(report['execution_authorized'])
        self.assertEqual(raw, self.batches._path(batch['id']).read_bytes())

    def test_serial_end_to_end_requires_each_individual_approval(self):
        order = []
        advance = self.engine.advance
        def observed(task, **kwargs):
            order.append(task)
            return advance(task, **kwargs)
        for expected in ('AWAITING_PLAN_APPROVAL', 'AWAITING_ACTION_APPROVAL', 'COMPLETED'):
            batch = self.create()
            with patch.object(self.engine, 'advance', side_effect=observed):
                result = self.run_batch(batch)
            self.assertEqual([e['result'] for e in result['entries']], [expected]*2)
            self.assertEqual([e['status'] for e in result['entries']], ['stopped']*2)
            unchanged = [self.engine.task(t) for t in self.tasks]
            self.run_batch(result)
            self.assertEqual(unchanged, [self.engine.task(t) for t in self.tasks])
            self.assertEqual(order, sorted(order, key=self.tasks.index))
            order.clear()
            if expected != 'COMPLETED':
                with self.assertRaises(Rejected): self.create()
                for task in self.tasks: self.approve(task)

    def test_input_rejection_is_atomic_and_bounded(self):
        valid = {'task_id':self.tasks[0], 'expected_revision':1}
        bad = [{}, {'tasks':[]}, {'tasks':[valid]*17}, {'tasks':[valid,valid]},
               {'tasks':[{**valid, 'expected_revision':True}]}, {'tasks':[{**valid, 'expected_revision':0}]},
               {'tasks':[{**valid, 'extra':'instruction'}]}, {'tasks':[{**valid, 'task_id':'../escape'}]}]
        for requests in bad:
            with self.subTest(requests=requests), self.assertRaises(Rejected): self.batches.create(requests)
        self.assertEqual(list(self.batches.root.glob('*.json')), [])
        with patch('orch.batches.MAX_BATCHES', 0), self.assertRaises(Rejected): self.create()

    def test_stale_revision_and_same_revision_context_change_cannot_dispatch(self):
        batch = self.create()
        # Context-only changes must be fenced even without a revision increase.
        state, context = self.engine.store.task(self.tasks[0])
        context['batch_test'] = True
        self.engine.store.db.execute('UPDATE tasks SET context=? WHERE id=?', (canonical(context).decode(), self.tasks[0]))
        result = self.run_batch(batch)
        self.assertEqual([e['status'] for e in result['entries']], ['running','pending'])
        self.assertEqual(self.engine.task(self.tasks[0])['state'], state)
        with self.assertRaises(Rejected): self.run_batch(result)

    def test_compare_and_advance_is_inside_dispatcher_lock(self):
        task = self.tasks[0]
        _, sha = snapshot(self.engine, task)
        self.engine.advance(task)
        before = self.engine.task(task)
        with self.assertRaises(Rejected): self.engine.advance(task, expected_snapshot=sha)
        self.assertEqual(self.engine.task(task), before)

    def test_crash_after_effect_never_repeats_and_remaining_work_needs_review(self):
        batch = self.create()
        advance = self.engine.advance
        def crash(task, **kwargs):
            advance(task, **kwargs)
            raise RuntimeError('simulated process loss')
        with patch.object(self.engine, 'advance', side_effect=crash), self.assertRaises(RuntimeError):
            self.run_batch(batch)
        retained = self.batches.inspect(batch['id'])
        self.assertFalse(retained['observations'][0]['matches_checkpoint'])
        with self.assertRaises(Rejected): self.run_batch(retained['batch'])
        current = retained['observations'][0]['snapshot_sha256']
        resolved = self.batches.abandon(batch['id'], batch['scope_sha256'], retained['batch']['revision'], self.tasks[0], current)
        first = self.engine.task(self.tasks[0])
        completed = self.run_batch(resolved)
        self.assertEqual(completed['entries'][0]['status'], 'abandoned')
        self.assertEqual(completed['entries'][1]['result'], 'AWAITING_PLAN_APPROVAL')
        self.assertEqual(self.engine.task(self.tasks[0]), first)

    def test_scope_revision_source_runtime_and_expiry_are_checked(self):
        batch = self.create()
        for sha, revision in [('a'*64, 0), (batch['scope_sha256'], True), (batch['scope_sha256'], 9)]:
            with self.assertRaises(Rejected): self.batches.run(batch['id'], sha, revision)
        with patch('orch.batches.code_digest', return_value='c'*64), self.assertRaises(Rejected): self.run_batch(batch)
        with patch.object(self.engine.executor, 'mode', 'docker'), self.assertRaises(Rejected): self.run_batch(batch)
        batch['scope']['created_at'] = (datetime.now(timezone.utc)-timedelta(hours=1)).isoformat().replace('+00:00','Z')
        batch['scope']['expires_at'] = (datetime.fromisoformat(batch['scope']['created_at'])+timedelta(minutes=30)).isoformat().replace('+00:00','Z')
        batch['scope_sha256'] = digest(batch['scope'])
        self.batches._path(batch['id']).write_bytes(canonical(batch))
        with self.assertRaises(Rejected): self.run_batch(batch)
        self.assertEqual(self.engine.task(self.tasks[0])['state']['state'], 'SPEC_READY')

    def test_failed_checkpoint_write_reports_retained_reservation(self):
        batch = self.create()
        save = self.batches._save
        count = 0
        def failing(value):
            nonlocal count
            count += 1
            if count == 2: raise OSError('checkpoint unavailable')
            save(value)
        with patch.object(self.batches, '_save', side_effect=failing):
            result = self.run_batch(batch)
        self.assertEqual(result['entries'][0]['status'], 'running')
        self.assertEqual(result['entries'][0]['steps'], 0)
        self.assertEqual(result, self.batches.inspect(batch['id'])['batch'])
        self.assertEqual(self.engine.task(self.tasks[0])['state']['state'], 'PLANNING')
        with self.assertRaises(Rejected): self.run_batch(result)

    def test_scope_expiry_or_source_change_is_rechecked_between_steps(self):
        batch = self.create()
        check = self.batches._current
        count = 0
        def expired(scope):
            nonlocal count
            count += 1
            if count == 3: raise Rejected('scope expired')
            check(scope)
        with patch.object(self.batches, '_current', side_effect=expired): result = self.run_batch(batch)
        self.assertEqual(result['entries'][0]['status'], 'running')
        self.assertEqual(result['entries'][0]['steps'], 1)
        self.assertEqual(self.engine.task(self.tasks[1])['state']['state'], 'SPEC_READY')

    def test_abandon_is_revision_and_snapshot_bound_and_does_not_cancel_task(self):
        batch = self.create()
        before, sha = snapshot(self.engine, self.tasks[0])
        with self.assertRaises(Rejected): self.batches.abandon(batch['id'],batch['scope_sha256'],0,self.tasks[0],'a'*64)
        after = self.batches.abandon(batch['id'],batch['scope_sha256'],0,self.tasks[0],sha)
        self.assertEqual(self.engine.task(self.tasks[0]), before)
        self.assertEqual(after['entries'][0]['status'],'abandoned')
        with self.assertRaises(Rejected): self.run_batch(batch)
        result = self.run_batch(after)
        self.assertEqual(result['entries'][1]['result'], 'AWAITING_PLAN_APPROVAL')

    def test_abandon_refuses_active_work(self):
        batch = self.create()
        state, context = self.engine.store.task(self.tasks[0])
        state['active_operation_id'] = 'a'*32
        self.engine.store.db.execute('UPDATE tasks SET state=? WHERE id=?',(canonical(state).decode(),self.tasks[0]))
        _, sha = snapshot(self.engine,self.tasks[0])
        with self.assertRaises(Rejected): self.batches.abandon(batch['id'],batch['scope_sha256'],0,self.tasks[0],sha)

    def test_stopped_success_cannot_be_rewritten_and_step_budget_is_bounded(self):
        batch = self.create()
        current = self.engine.task(self.tasks[0])
        with patch.object(self.engine,'advance',return_value=current) as advance:
            result = self.run_batch(batch)
        self.assertEqual(advance.call_count,40)
        self.assertEqual(result['entries'][0]['steps'],40)
        self.assertEqual(result['entries'][1]['status'],'pending')
        with self.assertRaises(Rejected): self.run_batch(result)
        batch = self.create([self.tasks[1]])
        result = self.run_batch(batch)
        _, sha = snapshot(self.engine,self.tasks[1])
        with self.assertRaises(Rejected): self.batches.abandon(batch['id'],batch['scope_sha256'],result['revision'],self.tasks[1],sha)

    def test_journal_corruption_and_unknown_fields_are_rejected(self):
        batch = self.create()
        cases = [{**batch,'extra':True}, {**batch,'entries':[]}, {**batch,'scope_sha256':'a'*64}]
        for updates in ({'status':'approved'},{'steps':True},{'last_snapshot_sha256':'bad'}, {'result':'COMPLETED'}):
            item = copy.deepcopy(batch)
            item['entries'][0].update(updates)
            cases.append(item)
        for item in cases:
            self.batches._path(batch['id']).write_bytes(canonical(item))
            with self.assertRaises(Rejected): self.batches.inspect(batch['id'])

    def test_cross_process_dispatch_lock_blocks_all_batch_mutation(self):
        batch = self.create()
        with file_lock(self.batches.root/'dispatch.lock'):
            code = ('from orch.engine import Engine; from orch.execution import Executor; from orch.batches import Batches; '
                    'from orch.contracts import Rejected\n'
                    'e=Engine(__import__("sys").argv[1],Executor("trusted-fixture"))\n'
                    'try:\n Batches(e).run(*__import__("json").loads(__import__("sys").argv[2]))\n'
                    'except Rejected:\n print("locked")\n'
                    'finally:\n e.close()')
            result = subprocess.run([sys.executable,'-c',code,self.tmp.name,json.dumps([batch['id'],batch['scope_sha256'],0])],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0)
        self.assertEqual(result.stdout.strip(),'locked')
        self.assertEqual(self.batches.inspect(batch['id'])['batch']['revision'],0)

    def test_cli_inspection_and_required_scope(self):
        from orch.__main__ import main
        batch = self.create()
        output = io.StringIO()
        with patch('sys.argv',['orch','batch-inspect','--data',self.tmp.name,'--batch-id',batch['id']]), patch('sys.stdout',output): main()
        self.assertEqual(json.loads(output.getvalue())['batch'],batch)
        with patch('sys.argv',['orch','batch-run','--data',self.tmp.name,'--batch-id',batch['id']]), patch('sys.stderr',io.StringIO()), self.assertRaises(SystemExit) as error: main()
        self.assertEqual(error.exception.code,2)

    def test_cli_create_run_list_roundtrip(self):
        from orch.__main__ import main
        intent = Path(self.tmp.name)/'requests.json'
        intent.write_bytes(canonical({'tasks':[{'task_id':self.tasks[0],'expected_revision':1}]}))
        def command(*args):
            output = io.StringIO()
            with patch('sys.argv',['orch',*args,'--data',self.tmp.name]), patch('sys.stdout',output): main()
            return json.loads(output.getvalue())
        batch = command('batch-create','--trusted-fixture','--intent',str(intent))
        result = command('batch-run','--trusted-fixture','--batch-id',batch['id'],'--expected-sha256',batch['scope_sha256'],'--expected-revision','0')
        self.assertEqual(result['entries'][0]['result'],'AWAITING_PLAN_APPROVAL')
        self.assertEqual(command('batch-list')['items'][0]['id'],batch['id'])

    def test_list_is_bounded_and_does_not_create_metadata(self):
        self.assertEqual(self.batches.list()['items'],[])
        self.assertFalse(self.batches.root.exists())
        original = self.create()
        for i in range(51):
            value = copy.deepcopy(original)
            value['id'] = f'{i:032x}'
            self.batches._path(value['id']).write_bytes(canonical(value))
        first = self.batches.list()
        second = self.batches.list(first['next'])
        self.assertEqual(len(first['items']),50)
        self.assertEqual(len(second['items']),2)
        self.assertIsNone(second['next'])
        self.assertTrue(all(item['counts']['pending']==2 for item in first['items']))


if __name__ == '__main__': unittest.main()
