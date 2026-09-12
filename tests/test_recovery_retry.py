import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import test_runtime as runtime
from orch.contracts import Rejected, uid
from orch.maintenance import backup, restore
from orch.engine import Engine
from orch.execution import Executor


class RecoveryRetryTests(unittest.TestCase):
    setUp = runtime.RuntimeTests.setUp
    tearDown = runtime.RuntimeTests.tearDown
    create = runtime.RuntimeTests.create
    approve = runtime.RuntimeTests.approve
    interrupted_broker = runtime.RuntimeTests.interrupted_broker

    def test_cleanup_failure_reports_committed_recovery_and_preserves_files(self):
        task, state = self.interrupted_broker()
        workspace = self.engine.worker.workspace(state["active_operation_id"])
        unexpected = workspace / "operator-notes.txt"
        unexpected.write_text("retain")
        result = self.engine.recover(task, state["revision"])
        self.assertEqual(result["state"]["state"], "CHANGES_REQUESTED")
        self.assertEqual(result["context"]["operator_recovery"]["cleanup"], "deferred")
        self.assertEqual(unexpected.read_text(), "retain")
        events = self.engine.events(task)
        with patch.object(self.engine.worker, "cleanup", side_effect=AssertionError("No repeated cleanup")), patch.object(self.engine.executor.broker, "result", side_effect=AssertionError("No repeated reconciliation")):
            self.assertEqual(self.engine.recover(task, state["revision"]), result)
        self.assertEqual(self.engine.events(task), events)

    def test_interruption_after_commit_retries_cleanup_only(self):
        task, state = self.interrupted_broker()
        with patch.object(self.engine.worker, "cleanup", side_effect=RuntimeError("crash after commit")), self.assertRaises(RuntimeError):
            self.engine.recover(task, state["revision"])
        marker = self.engine.task(task)["context"]["operator_recovery"]
        self.assertEqual(marker["cleanup"], "pending")
        operation = dict(self.engine.store.db.execute("SELECT * FROM operations WHERE id=?", (state["active_operation_id"],)).fetchone())
        with patch.object(self.engine.executor.broker, "result", side_effect=AssertionError("No runtime replay")):
            result = self.engine.recover(task, state["revision"])
        self.assertEqual(result["context"]["operator_recovery"]["cleanup"], "completed")
        self.assertEqual(dict(self.engine.store.db.execute("SELECT * FROM operations WHERE id=?", (state["active_operation_id"],)).fetchone()), operation)
        with self.assertRaises(Rejected):
            self.engine.recover(task, state["revision"], principal="different-operator")
        self.engine.run(task)
        with self.assertRaises(Rejected):
            self.engine.recover(task, state["revision"])

    def test_completed_recovery_retry_survives_restore(self):
        task, state = self.interrupted_broker()
        expected = self.engine.recover(task, state["revision"])
        with tempfile.TemporaryDirectory() as base:
            source, target = Path(base) / "backup", Path(base) / "restored"
            backup(self.engine.store, source)
            restore(source, target)
            other = Engine(target, Executor("trusted-fixture"))
            try:
                with patch.object(other.executor.broker, "result", side_effect=AssertionError("No restored process ownership")):
                    self.assertEqual(other.recover(task, state["revision"]), expected)
            finally:
                other.close()

    def test_extra_unresolved_operations_block_before_runtime_probe(self):
        task, state = self.interrupted_broker()
        self.engine.store.db.execute("INSERT INTO operations(id,task_id,stage,slot,status) VALUES(?,?,?,'extra','started')", (uid(), task, "tests"))
        with patch.object(self.engine.executor.broker, "result", side_effect=AssertionError("No probe")):
            with self.assertRaises(Rejected):
                self.engine.recover(task, state["revision"])
            with self.assertRaises(Rejected):
                self.engine.recover(task, True)
        self.assertEqual(self.engine.task(task)["state"]["state"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
