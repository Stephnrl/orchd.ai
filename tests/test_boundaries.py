import copy
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import test_workflow as helpers
from contracts.interfaces import ValidatedContract
from orch.contracts import Rejected, uid
from orch.execution import Executor
from orch.fixtures import EDIT_CODE, TEST_CODE


import unittest
class BoundaryTests(unittest.TestCase):
    setUp = helpers.WorkflowTests.setUp
    tearDown = helpers.WorkflowTests.tearDown
    create = helpers.WorkflowTests.create
    approve = helpers.WorkflowTests.approve
    to_action = helpers.WorkflowTests.to_action
    count = helpers.WorkflowTests.count

    def test_expired_and_stale_approval(self):
        task = self.create()
        self.engine.run(task)
        state = self.engine.task(task)["state"]
        with self.assertRaises(Rejected):
            self.engine.approve(task, state["pending_approval"]["id"], "approve", state["revision"] - 1)
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat().replace("+00:00", "Z")
        with patch("orch.engine.now", return_value=future), self.assertRaises(Rejected):
            self.approve(task)
        self.assertEqual(self.count(task, "PatchReceipt"), 0)

    def test_uncertain_worker_blocks_without_repeating(self):
        task = self.create()
        self.engine.run(task)
        self.approve(task)
        self.engine.advance(task)
        with patch.object(self.engine.worker, "implement", side_effect=RuntimeError("process stopped")), self.assertRaises(RuntimeError):
            self.engine.advance(task)
        with patch.object(self.engine.worker, "implement", side_effect=AssertionError("must not repeat")):
            self.engine.advance(task)
        self.assertEqual(self.engine.task(task)["state"]["state"], "BLOCKED")
        self.assertIn("agent_invocation_started", [e["event_type"] for e in self.engine.events(task)])

    def test_new_id_cannot_duplicate_worker_operation(self):
        task = self.create()
        self.to_action(task)
        receipt = self.engine.store.get(self.engine.task(task)["state"]["patch"], task)
        receipt["id"] = uid()
        with self.assertRaises(Rejected):
            self.engine.store.put(receipt)

    def test_guardian_denies_arbitrary_commands_and_secrets(self):
        task = self.create()
        self.to_action(task)
        row = self.engine.store.db.execute("SELECT payload FROM records WHERE kind='ToolRequest' ORDER BY rowid LIMIT 1").fetchone()
        request = json.loads(row[0])
        request["tool"] = "network"
        self.assertEqual(self.engine.guardian.evaluate(ValidatedContract("ToolRequest", request)).payload["decision"], "deny")
        request["tool"] = "test"
        from orch.fixtures import recipe
        request["command"] = recipe()
        request["command"]["argv"] = ["powershell", "-Command", "anything"]
        self.assertEqual(self.engine.guardian.evaluate(ValidatedContract("ToolRequest", request)).payload["decision"], "deny")

    def test_process_timeout_receipt(self):
        task = self.create()
        self.engine.run(task)
        self.approve(task)
        self.engine.advance(task)
        self.engine.advance(task)
        with patch.object(self.engine.executor.broker, "run", return_value={"command": ["fixed-test"], "started": "2026-09-12T14:00:00Z", "ended": "2026-09-12T14:00:10Z", "code": None, "stdout": "", "stderr": "deadline", "truncated": False, "failure": "timeout"}):
            self.engine.advance(task)
        receipt = self.engine.store.get(self.engine.task(task)["context"]["test"], task)
        self.assertEqual(receipt["outcome"], "timeout")
        self.assertIsNone(receipt["exit_code"])

    def test_tampered_artifact_fails_closed(self):
        task = self.create()
        self.engine.run(task)
        self.approve(task)
        self.engine.advance(task)
        self.engine.advance(task)
        artifact = self.engine.task(task)["context"]["snapshot"]
        (self.engine.store.root / "artifacts" / artifact["sha256"]).write_text("tampered")
        self.engine.advance(task)
        self.assertEqual(self.engine.task(task)["state"]["state"], "CHANGES_REQUESTED")
        self.assertEqual(self.count(task, "TestReceipt"), 0)

    def test_secrets_redacted_and_title_rejected(self):
        task = self.create()
        item = self.engine.store.artifact(task, "token=canary-secret", "text/plain")
        self.assertEqual(self.engine.store.read_artifact(item, task), "[REDACTED]")
        with self.assertRaises(Rejected):
            self.engine.create_task("token=canary-secret")

    def test_docker_command_restrictions_and_untrusted_script(self):
        executor = Executor("docker", "python@sha256:" + "a" * 64)
        with patch("orch.execution.capture", return_value={"code": 0, "stdout": "", "stderr": "", "truncated": False, "failure": None}) as run:
            executor.run(self.temp.name, TEST_CODE, [], uid())
            command = run.call_args.args[0]
            for flag in ("--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user=65534:65534", "--pull=never", "--memory-swap=256m", "--log-driver=none"):
                self.assertIn(flag, command)
            self.assertNotIn("/var/run/docker.sock", str(command))
            self.assertIsInstance(command, list)
        with self.assertRaises(Rejected):
            executor.run(self.temp.name, "untrusted code", [], uid())

    def test_no_external_effect_before_action_approval(self):
        task = self.create()
        self.to_action(task)
        with patch.object(self.engine.external_action, "execute", side_effect=AssertionError("not approved")):
            self.engine.run(task)
        self.assertEqual(self.engine.store.db.execute("SELECT count(*) FROM effects").fetchone()[0], 0)

    def test_exclusive_dispatch(self):
        with self.engine.store.exclusive(), self.assertRaises(Rejected):
            with self.engine.store.exclusive():
                pass

    def test_reconcile_effect_before_dispatch_acknowledgement(self):
        task = self.create()
        self.to_action(task)
        self.approve(task)
        def crash(stage):
            if stage == "external_action":
                raise RuntimeError("crash")
        self.engine.after_operation = crash
        with self.assertRaises(RuntimeError):
            self.engine.advance(task)
        # Inject crash boundary: effect/receipt committed, dispatch acknowledgement absent.
        self.engine.store.db.execute("UPDATE operations SET status='started',result=NULL WHERE stage='external_action'")
        self.engine.after_operation = None
        with patch.object(self.engine.external_action, "execute", side_effect=AssertionError("duplicate effect")):
            self.engine.run(task)
        self.assertEqual(self.engine.task(task)["state"]["state"], "COMPLETED")
        self.assertEqual(self.engine.store.db.execute("SELECT count(*) FROM effects").fetchone()[0], 1)
