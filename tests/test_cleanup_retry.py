import json
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_runtime as runtime
from orch.api import Application
from orch.contracts import Rejected, uid
from orch.maintenance import audit


class CleanupRetryTests(unittest.TestCase):
    setUp = runtime.RuntimeTests.setUp
    tearDown = runtime.RuntimeTests.tearDown
    create = runtime.RuntimeTests.create
    approve = runtime.RuntimeTests.approve
    interrupted_broker = runtime.RuntimeTests.interrupted_broker

    def deferred(self):
        task, state = self.interrupted_broker()
        operation = state["active_operation_id"]
        workspace = self.engine.worker.workspace(operation)
        note = workspace / "notes.txt"
        note.write_text("retain for inspection")
        result = self.engine.recover(task, state["revision"])
        self.assertEqual(result["context"]["operator_recovery"]["cleanup"], "deferred")
        return task, operation, result["state"]["revision"], note

    def test_explicit_retry_preserves_unexpected_files_then_cleans_without_runtime(self):
        task, operation, revision, note = self.deferred()
        before = dict(self.engine.store.db.execute("SELECT * FROM operations WHERE id=?", (operation,)).fetchone())
        with patch.object(self.engine.executor, "reconcile", side_effect=AssertionError("No runtime")), patch.object(self.engine.executor.broker, "result", side_effect=AssertionError("No journal")):
            result = self.engine.retry_cleanup(task, operation, revision)
            self.assertEqual(result["context"]["operator_recovery"]["cleanup"], "deferred")
            self.assertEqual(note.read_text(), "retain for inspection")
            note.unlink()  # Represents operator inspection/removal of the test fixture.
            result = self.engine.retry_cleanup(task, operation, revision)
            self.assertEqual(result["context"]["operator_recovery"]["cleanup"], "completed")
            self.assertFalse(note.parent.exists())
            events = self.engine.events(task)
            self.assertEqual(self.engine.retry_cleanup(task, operation, revision), result)
            self.assertEqual(self.engine.events(task), events)
        self.assertEqual(result["state"]["state"], "CHANGES_REQUESTED")
        self.assertEqual(dict(self.engine.store.db.execute("SELECT * FROM operations WHERE id=?", (operation,)).fetchone()), before)
        self.assertTrue(any(e["event_type"] == "operator_cleanup_retry" and e["actor"]["role"] == "human" for e in events))
        audit(self.engine.store)

    def test_stale_cross_operation_identity_busy_and_unresolved_reject(self):
        task, operation, revision, note = self.deferred()
        with patch.object(self.engine.worker, "cleanup", side_effect=AssertionError("No deletion")):
            for op, rev, principal in ((uid(), revision, "local-operator"), (operation, revision - 1, "local-operator"),
                                       (operation, True, "local-operator"), (operation, revision, "other")):
                with self.assertRaises(Rejected):
                    self.engine.retry_cleanup(task, op, rev, principal)
            with self.engine.store.exclusive(), self.assertRaises(Rejected):
                self.engine.retry_cleanup(task, operation, revision)
            self.engine.store.db.execute("INSERT INTO operations(id,task_id,stage,slot,status) VALUES(?,?,?,'extra','started')", (uid(), task, "tests"))
            with self.assertRaises(Rejected):
                self.engine.retry_cleanup(task, operation, revision)
        self.assertTrue(note.exists())

    def test_pending_retry_after_crash_and_workflow_advancement(self):
        task, operation, revision, note = self.deferred()
        note.unlink()
        with patch.object(self.engine.worker, "cleanup", side_effect=RuntimeError("crash")), self.assertRaises(RuntimeError):
            self.engine.retry_cleanup(task, operation, revision)
        self.assertEqual(self.engine.task(task)["context"]["operator_recovery"]["cleanup"], "pending")
        self.engine.retry_cleanup(task, operation, revision)
        self.engine.run(task)
        with self.assertRaises(Rejected):
            self.engine.retry_cleanup(task, operation, revision)

    def test_api_auth_closed_fields_and_cli_without_docker(self):
        task, operation, revision, note = self.deferred()
        app = Application(self.engine)
        route = f"/tasks/{task}/retry-cleanup"
        payload = {"operation_id": operation, "expected_revision": revision}
        self.assertEqual(app.dispatch("POST", route, payload, "wrong")[0], 401)
        for body in ({}, {**payload, "principal": "other"}, {**payload, "expected_revision": True}, {**payload, "operation_id": []}):
            self.assertEqual(app.dispatch("POST", route, body, app.session)[0], 409)
        self.assertEqual(app.dispatch("POST", route, payload, app.session)[1]["context"]["operator_recovery"]["cleanup"], "deferred")
        note.unlink()
        command = [sys.executable, "-m", "orch", "retry-cleanup", "--task", task, "--operation-id", operation,
                   "--expected-revision", str(revision), "--data"]
        result = subprocess.run(command + [str(self.engine.store.root)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["context"]["operator_recovery"]["cleanup"], "completed")
        missing = self.engine.store.root / "missing"
        result = subprocess.run(command + [str(missing)], capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
