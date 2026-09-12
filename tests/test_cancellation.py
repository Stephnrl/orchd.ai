import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_workflow as helpers
from orch.api import Application
from orch.contracts import Rejected, uid
from orch.engine import Engine
from orch.execution import Executor
from orch.maintenance import backup, restore


class CancellationTests(unittest.TestCase):
    setUp = helpers.WorkflowTests.setUp
    tearDown = helpers.WorkflowTests.tearDown
    create = helpers.WorkflowTests.create
    approve = helpers.WorkflowTests.approve
    to_action = helpers.WorkflowTests.to_action
    count = helpers.WorkflowTests.count

    def test_plan_pause_cancel_idempotency_and_no_further_execution(self):
        task = self.create()
        self.engine.run(task)
        before = self.engine.task(task)["state"]
        result = self.engine.cancel(task, before["revision"], "No longer needed")
        self.assertEqual(result["state"]["state"], "CANCELLED")
        self.assertIsNone(result["state"]["pending_approval"])
        self.assertEqual(result["context"]["cancellation"]["pending_approval"], before["pending_approval"])
        events = self.engine.events(task)
        self.assertEqual(result, self.engine.cancel(task, before["revision"], "No longer needed"))
        self.assertEqual(events, self.engine.events(task))
        with self.assertRaises(Rejected):
            self.engine.cancel(task, before["revision"], "Different reason")
        with self.assertRaises(Rejected):
            self.engine.approve(task, before["pending_approval"]["id"], "approve", before["revision"])
        with patch.object(self.engine, "_invoke", side_effect=AssertionError("No invocation")), patch.object(self.engine.worker, "implement", side_effect=AssertionError("No execution")):
            self.assertEqual(self.engine.run(task), result)
            self.assertEqual(self.engine.advance(task), result)
        event = next(e for e in events if e["event_type"] == "operator_cancellation")
        self.assertEqual(event["actor"]["role"], "human")
        payload = json.loads(self.engine.store.read_artifact(event["payload"], task))
        self.assertEqual(payload["detail"]["principal"], "local-operator")

    def test_cancel_action_pause_preserves_evidence_without_external_effect(self):
        task = self.create()
        self.to_action(task)
        state = self.engine.task(task)["state"]
        records = self.engine.store.db.execute("SELECT count(*) FROM records").fetchone()[0]
        self.engine.cancel(task, state["revision"], "Do not create a simulated PR")
        self.assertEqual(self.count(task, "ExternalActionReceipt"), 0)
        self.assertEqual(self.engine.store.db.execute("SELECT count(*) FROM records").fetchone()[0], records)
        self.assertEqual(self.engine.store.get(state["patch"], task)["contract_type"], "PatchReceipt")

    def test_unresolved_operation_or_busy_dispatcher_cannot_be_cancelled(self):
        task = self.create()
        state = self.engine.task(task)["state"]
        with self.engine.store.exclusive(), self.assertRaises(Rejected):
            self.engine.cancel(task, state["revision"], "Busy")
        operation = uid()
        self.engine.store.db.execute("INSERT INTO operations(id,task_id,stage,slot,status) VALUES(?,?,?,?,?)", (operation, task, "implementation", "test-slot", "started"))
        with self.assertRaises(Rejected):
            self.engine.cancel(task, state["revision"], "Uncertain")
        self.assertEqual(self.engine.task(task)["state"], state)

    def test_cancel_rejects_successful_action_before_final_transition(self):
        task = self.create()
        self.to_action(task)
        self.approve(task)
        def crash(stage):
            if stage == "external_action":
                raise RuntimeError("receipt committed")
        self.engine.after_operation = crash
        with self.assertRaises(RuntimeError):
            self.engine.run(task)
        state = self.engine.task(task)["state"]
        with self.assertRaises(Rejected):
            self.engine.cancel(task, state["revision"], "Too late")
        self.engine.after_operation = None
        self.engine.run(task)
        self.assertEqual(self.engine.task(task)["state"]["state"], "COMPLETED")

    def test_api_auth_closed_payload_stale_revision_and_reason_validation(self):
        task = self.create()
        app = Application(self.engine)
        state = self.engine.task(task)["state"]
        payload = {"expected_revision": state["revision"], "reason": "Cancel fixture"}
        path = f"/tasks/{task}/cancel"
        self.assertEqual(app.dispatch("POST", path, payload, "wrong")[0], 401)
        for changes in ({"actor": "admin"}, {"expected_revision": True}, {"expected_revision": 0}, {"reason": ""}, {"reason": "x" * 501}, {"reason": "token=canary"}, {"reason": "a\nb"}):
            self.assertEqual(app.dispatch("POST", path, {**payload, **changes}, app.session)[0], 409)
        self.assertEqual(self.engine.task(task)["state"], state)
        self.assertEqual(app.dispatch("POST", path, payload, app.session)[0], 200)

    def test_cancellation_replays_after_backup_restore(self):
        task = self.create()
        state = self.engine.task(task)["state"]
        expected = self.engine.cancel(task, state["revision"], "Stop this fixture")
        with tempfile.TemporaryDirectory() as outer:
            backup_path, restored_path = Path(outer) / "backup", Path(outer) / "restore"
            backup(self.engine.store, backup_path)
            restore(backup_path, restored_path)
            restored = Engine(restored_path, Executor("trusted-fixture"))
            try:
                self.assertEqual(restored.task(task), expected)
                self.assertEqual(restored.run(task), expected)
                self.assertEqual(restored.store.reconstruct(task)["state"], expected["state"])
            finally:
                restored.close()

    def test_cli_cancellation_needs_no_docker_configuration(self):
        task = self.create()
        state = self.engine.task(task)["state"]
        result = subprocess.run([sys.executable, "-m", "orch", "cancel", "--data", self.temp.name,
                                 "--task", task, "--expected-revision", str(state["revision"]), "--reason", "CLI cancellation"],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["state"]["state"], "CANCELLED")
        self.assertEqual(self.engine.task(task)["state"]["state"], "CANCELLED")
