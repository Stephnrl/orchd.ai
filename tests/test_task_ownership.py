"""One owner per task: per-task advancement locks and the claim that outlives a crash.

The store-wide dispatcher lock used to serialise every task in the store. These tests fix
what replaced it: two workers may advance two different tasks at once, never the same one,
whole-store work still excludes both, and a worker that dies leaves evidence rather than a
silently reusable task.
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
from orch.contracts import Rejected, canonical, uid
from orch.engine import Engine
from orch.execution import Executor
from orch.maintenance import backup, integrity_report
from orch.storage import Store

ROOT = Path(__file__).resolve().parents[1]
FOREIGN = canonical({"principal": "another-account", "pid": 4242, "session": "b" * 32}).decode()


def hold(path, task, ready, release):
    """A child process that holds one task's advancement lock until told to stop."""
    return subprocess.Popen(
        [sys.executable, '-c',
         "import sys, time;"
         "from orch.storage import Store;"
         "store = Store(sys.argv[1]);"
         "lock = store.exclusive(sys.argv[2]) if sys.argv[2] != '-' else store.exclusive();"
         "lock.__enter__();"
         "open(sys.argv[3], 'w').close();"
         "[time.sleep(0.05) for _ in range(600) if not __import__('os').path.exists(sys.argv[4])];"
         "lock.__exit__(None, None, None)",
         str(path), task, str(ready), str(release)],
        cwd=ROOT, env={**os.environ, 'PYTHONPATH': str(ROOT)},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)


class TaskOwnershipTests(unittest.TestCase):
    tearDown = helpers.WorkflowTests.tearDown
    create = helpers.WorkflowTests.create
    approve = helpers.WorkflowTests.approve

    def setUp(self):
        helpers.WorkflowTests.setUp(self)
        self.backups = tempfile.TemporaryDirectory()   # Backups refuse a destination in live data.
        self.addCleanup(self.backups.cleanup)

    def held_elsewhere(self, task):
        """Run a real second process holding that task's lock, and return a stopper."""
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
            self.fail('the second worker never took the lock: ' + str(child.communicate(timeout=10)))
        return stop

    # What two workers may and may not do -------------------------------------------------

    def test_two_workers_advance_different_tasks_at_once(self):
        first, second = self.create(), self.create()
        stop = self.held_elsewhere(first)
        try:
            # Another process is advancing `first`; this one is free to advance `second`.
            self.assertEqual(self.engine.run(second)["state"]["state"], "AWAITING_PLAN_APPROVAL")
            with self.assertRaisesRegex(Rejected, 'advanced elsewhere'):
                self.engine.advance(first)
        finally:
            stop()
        self.assertEqual(self.engine.run(first)["state"]["state"], "AWAITING_PLAN_APPROVAL")

    def test_operator_decisions_wait_for_the_worker_on_that_task_only(self):
        first, second = self.create(), self.create()
        self.engine.run(first)
        self.engine.run(second)
        state = self.engine.task(first)["state"]
        stop = self.held_elsewhere(first)
        try:
            for action in (lambda: self.engine.approve(first, state["pending_approval"]["id"], "approve", state["revision"]),
                           lambda: self.engine.cancel(first, state["revision"], "Busy"),
                           lambda: self.engine.recovery_diagnostics(first)):
                with self.assertRaisesRegex(Rejected, 'advanced elsewhere'):
                    action()
            # The same decisions on another task are unaffected.
            other = self.engine.task(second)["state"]
            self.engine.approve(second, other["pending_approval"]["id"], "approve", other["revision"])
            self.assertEqual(self.engine.task(second)["state"]["state"], "READY_FOR_IMPLEMENTATION")
        finally:
            stop()

    def test_a_claim_is_recorded_while_advancing_and_released_afterwards(self):
        task = self.create()
        seen = []
        self.engine.after_operation = lambda *_: seen.append(self.engine.store.owner(task))
        self.engine.run(task)
        self.engine.after_operation = None
        self.assertTrue(seen, 'the fixture must reach an operation')
        self.assertEqual(json.loads(seen[0]["owner"]), json.loads(self.engine.owner))
        self.assertEqual(seen[0]["generation"], 1)
        self.assertIsNone(self.engine.store.owner(task))   # Released when the step returns.
        self.assertEqual(self.engine.store.owners(), [])

    # What a crashed worker leaves behind ---------------------------------------------------

    def test_an_interrupted_claim_is_taken_over_and_recorded(self):
        task = self.create()
        self.engine.run(task)
        with self.engine.store.transaction():
            self.engine.store.claim(task, FOREIGN, 0, 1)    # A worker that died mid-advance.
        self.assertIsNotNone(self.engine.store.owner(task))
        self.approve(task)
        self.engine.run(task)
        taken = [event for event in self.engine.events(task) if event["event_type"] == "task_ownership_taken"]
        self.assertEqual(len(taken), 1)
        detail = json.loads(self.engine.store.read_artifact(taken[0]["payload"], task))["detail"]
        self.assertEqual(detail["previous_owner"]["principal"], "another-account")
        self.assertEqual(detail["generation"], 2)
        self.assertIsNone(self.engine.store.owner(task))

    def test_takeover_never_repeats_an_interrupted_operation(self):
        task = self.create()
        self.engine.run(task)
        self.approve(task)
        operation, attempt = uid(), self.engine.task(task)["context"]["attempt"] + 1
        with self.engine.store.transaction():
            self.engine.store.db.execute(
                "INSERT INTO operations(id,task_id,stage,slot,status,result) VALUES(?,?,?,?,?,NULL)",
                (operation, task, "implementation", str(attempt), "started"))
            self.engine.store.claim(task, FOREIGN, 0, 1)
        with patch.object(self.engine.worker, "implement", side_effect=AssertionError("No execution")):
            result = self.engine.run(task)
        self.assertEqual(result["state"]["state"], "BLOCKED")
        self.assertEqual(self.engine.store.db.execute(
            "SELECT status FROM operations WHERE id=?", (operation,)).fetchone()[0], "started")

    def test_diagnostics_reports_an_interrupted_claim(self):
        task = self.create()
        self.engine.run(task)
        self.assertIsNone(self.engine.recovery_diagnostics(task)["interrupted_claim"])
        with self.engine.store.transaction():
            self.engine.store.claim(task, FOREIGN, 3, 2)
        report = self.engine.recovery_diagnostics(task)
        self.assertEqual((report["interrupted_claim"]["revision"], report["interrupted_claim"]["generation"]), (3, 2))

    def test_a_claim_outlives_the_process_that_wrote_it(self):
        task = self.create()
        with self.engine.store.transaction():
            self.engine.store.claim(task, self.engine.owner, 1, 1)
        code = ("import sys, json; from orch.storage import Store;"
                "store = Store(sys.argv[1]);"
                "print(json.dumps([store.owner(sys.argv[2]) is not None, store.active_owners()]))")
        result = subprocess.run([sys.executable, '-c', code, self.temp.name, task], cwd=ROOT,
                                env={**os.environ, 'PYTHONPATH': str(ROOT)},
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-400:])
        recorded, active = json.loads(result.stdout)
        self.assertTrue(recorded)          # The row survives in another process,
        self.assertEqual(active, [])       # but nothing is actually advancing it.

    # Whole-store work ------------------------------------------------------------------------

    def test_maintenance_refuses_while_a_task_is_really_being_advanced(self):
        task = self.create()
        with self.engine.store.transaction():
            self.engine.store.claim(task, FOREIGN, 0, 1)
        # A recorded claim whose worker is gone does not stop whole-store work.
        self.assertEqual(integrity_report(self.engine.store)["status"], "passed")
        stop = self.held_elsewhere(task)
        try:
            self.assertEqual(self.engine.store.active_owners()[0]["task_id"], task)
            with self.assertRaisesRegex(Rejected, 'being advanced'):
                integrity_report(self.engine.store)
            with self.assertRaisesRegex(Rejected, 'being advanced'):
                backup(self.engine.store, Path(self.backups.name) / 'backup')
        finally:
            stop()
        self.assertEqual(integrity_report(self.engine.store)["status"], "passed")

    def test_maintenance_stops_new_claims_while_it_holds_the_store(self):
        task = self.create()
        ready = Path(self.temp.name) / 'store.ready'
        release = Path(self.temp.name) / 'store.release'
        child = hold(self.temp.name, '-', ready, release)
        try:
            for _ in range(600):
                if ready.exists() or child.poll() is not None:
                    break
                time.sleep(0.05)
            self.assertTrue(ready.exists())
            with self.assertRaisesRegex(Rejected, 'Dispatcher busy'):
                self.engine.advance(task)       # The claim cannot be registered.
            self.assertIsNone(self.engine.store.owner(task))
        finally:
            release.write_bytes(b'')
            child.wait(timeout=30)
        self.assertEqual(self.engine.run(task)["state"]["state"], "AWAITING_PLAN_APPROVAL")

    def test_unsafe_task_identifiers_never_become_lock_paths(self):
        for identifier in ('../escape', 'a/b', 'a' * 65, '', 'a b'):
            with self.subTest(identifier=identifier):
                with self.assertRaises(Rejected):
                    self.engine.store.lock_path(identifier)
        self.assertEqual(self.engine.store.lock_path('abc').name, 'abc.lock')


if __name__ == '__main__':
    unittest.main()
