"""How many workers may advance tasks at once, and what happens at the bound.

Ownership made two workers possible; this fixes how many. The bound is a host-resource
rule, so work is refused before it starts rather than failing halfway and leaving an
operation to reconcile.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import test_workflow as helpers
from test_task_ownership import FOREIGN, hold
from orch.contracts import Rejected, canonical
from orch.engine import Engine
from orch.execution import Executor
from orch.storage import MAX_ACTIVE_TASKS

ROOT = Path(__file__).resolve().parents[1]


class WorkerAdmissionTests(unittest.TestCase):
    create = helpers.WorkflowTests.create
    approve = helpers.WorkflowTests.approve

    def setUp(self):
        helpers.WorkflowTests.setUp(self)
        self.stoppers = []

    def tearDown(self):
        for stop in self.stoppers:      # Before the temp directory, which the child holds open.
            stop()
        helpers.WorkflowTests.tearDown(self)

    def occupied(self, task):
        """A live worker elsewhere: a recorded claim whose task lock is genuinely held."""
        ready = Path(self.temp.name) / (task + '.ready')
        release = Path(self.temp.name) / (task + '.release')
        child = hold(self.temp.name, task, ready, release)

        def stop():
            release.write_bytes(b'')
            child.wait(timeout=30)

        for _ in range(600):
            if ready.exists() or child.poll() is not None:
                break
            time.sleep(0.05)
        if not ready.exists():
            stop()
            self.fail('the occupying worker never started: ' + str(child.communicate(timeout=10)))
        with self.engine.store.transaction():
            self.engine.store.claim(task, FOREIGN, 0, 1)
        self.stoppers.append(stop)
        return stop

    def test_the_bound_refuses_new_work_rather_than_failing_halfway(self):
        busy, mine = self.create(), self.create()
        untouched = self.engine.task(mine)
        self.occupied(busy)
        with patch('orch.engine.MAX_ACTIVE_TASKS', 1):
            with self.assertRaisesRegex(Rejected, 'Too many tasks'):
                self.engine.advance(mine)
            # Nothing was claimed or changed by the refusal.
            self.assertIsNone(self.engine.store.owner(mine))
            self.assertEqual(self.engine.task(mine), untouched)
        # Above the bound, the same work proceeds.
        with patch('orch.engine.MAX_ACTIVE_TASKS', 2):
            self.assertEqual(self.engine.run(mine)["state"]["state"], "AWAITING_PLAN_APPROVAL")

    def test_a_worker_keeps_its_own_slot_across_steps(self):
        task = self.create()
        with patch('orch.engine.MAX_ACTIVE_TASKS', 1):
            # The bound counts other tasks, so one worker can take as many steps as it needs.
            self.assertEqual(self.engine.run(task)["state"]["state"], "AWAITING_PLAN_APPROVAL")
            self.approve(task)
            self.assertEqual(self.engine.run(task)["state"]["state"], "AWAITING_ACTION_APPROVAL")

    def test_a_claim_left_by_a_crash_does_not_hold_a_slot(self):
        abandoned, mine = self.create(), self.create()
        with self.engine.store.transaction():
            self.engine.store.claim(abandoned, FOREIGN, 0, 1)   # A worker that died.
        self.assertEqual(len(self.engine.store.owners()), 1)
        self.assertEqual(self.engine.store.admitted(mine), [])  # Nothing is actually running.
        with patch('orch.engine.MAX_ACTIVE_TASKS', 1):
            self.assertEqual(self.engine.run(mine)["state"]["state"], "AWAITING_PLAN_APPROVAL")

    def test_a_slot_is_free_again_once_the_work_returns(self):
        first, second = self.create(), self.create()
        with patch('orch.engine.MAX_ACTIVE_TASKS', 1):
            self.engine.run(first)
            self.assertEqual(self.engine.store.owners(), [])
            self.assertEqual(self.engine.run(second)["state"]["state"], "AWAITING_PLAN_APPROVAL")

    def test_one_process_refuses_to_advance_a_task_it_is_already_advancing(self):
        task = self.create()
        seen = []

        def reenter(*_):
            try:
                self.engine.advance(task)
            except Rejected as exc:
                seen.append(str(exc))

        self.engine.after_operation = reenter
        self.engine.run(task)
        self.engine.after_operation = None
        self.assertTrue(seen)
        self.assertIn('already advancing', seen[0])

    def test_two_workers_finish_two_tasks_without_touching_each_other(self):
        first, second = self.create(), self.create()
        code = ("import sys\n"
                "from orch.engine import Engine\n"
                "from orch.execution import Executor\n"
                "engine = Engine(sys.argv[1], Executor('trusted-fixture'))\n"
                "print(engine.run(sys.argv[2])['state']['state'])\n"
                "engine.close()\n")
        workers = [subprocess.Popen([sys.executable, '-c', code, self.temp.name, task], cwd=ROOT,
                                    env={**os.environ, 'PYTHONPATH': str(ROOT)},
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                   for task in (first, second)]
        results = [worker.communicate(timeout=300) for worker in workers]
        for (out, err), worker in zip(results, workers):
            self.assertEqual(worker.returncode, 0, err[-400:])
            self.assertEqual(out.strip(), "AWAITING_PLAN_APPROVAL")
        # Separate evidence, separate operations, and no claim left behind by either.
        self.assertEqual(self.engine.store.owners(), [])
        for task in (first, second):
            operations = self.engine.store.db.execute(
                "SELECT id FROM operations WHERE task_id=?", (task,)).fetchall()
            self.assertTrue(operations)
            self.assertEqual(self.engine.task(task)["state"]["state"], "AWAITING_PLAN_APPROVAL")
        overlap = {row["id"] for row in self.engine.store.db.execute(
            "SELECT id FROM operations WHERE task_id=?", (first,))} & {
            row["id"] for row in self.engine.store.db.execute(
                "SELECT id FROM operations WHERE task_id=?", (second,))}
        self.assertEqual(overlap, set())

    def test_an_approval_on_one_task_leaves_the_other_untouched(self):
        first, second = self.create(), self.create()
        self.engine.run(first)
        self.engine.run(second)
        before = self.engine.task(second)
        self.occupied(second)          # A worker is advancing the second task.
        self.approve(first)            # The first task's approval is unaffected by it.
        self.assertEqual(self.engine.task(first)["state"]["state"], "READY_FOR_IMPLEMENTATION")
        after = self.engine.task(second)
        self.assertEqual((before["state"]["revision"], before["state"]["state"]),
                         (after["state"]["revision"], after["state"]["state"]))

    def test_the_documented_bound_is_the_one_enforced(self):
        self.assertEqual(MAX_ACTIVE_TASKS, 4)
        text = (ROOT / 'docs/worker-admission.md').read_text(encoding='utf-8')
        self.assertIn(str(MAX_ACTIVE_TASKS), text)


if __name__ == '__main__':
    unittest.main()
