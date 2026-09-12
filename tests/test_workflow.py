import copy
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from orch.api import Application
from orch.contracts import Rejected, validate
from orch.engine import EDGES, TERMINAL, Engine, allowed
from orch.execution import Executor
from orch.provider import Scenario


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = Engine(self.temp.name, Executor("trusted-fixture"))

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def create(self, **scenario):
        return self.engine.create_task(scenario=Scenario(**scenario))

    def approve(self, task, decision="approve"):
        state = self.engine.task(task)["state"]
        return self.engine.approve(task, state["pending_approval"]["id"], decision, state["revision"])

    def to_action(self, task):
        self.engine.run(task)
        self.assertEqual(self.engine.task(task)["state"]["state"], "AWAITING_PLAN_APPROVAL")
        self.approve(task)
        self.engine.run(task)

    def count(self, task, kind):
        return self.engine.store.db.execute("SELECT count(*) FROM records WHERE task_id=? AND kind=?", (task, kind)).fetchone()[0]

    def test_straight_through_and_reconstruction(self):
        task = self.create()
        self.to_action(task)
        self.assertEqual(self.engine.task(task)["state"]["state"], "AWAITING_ACTION_APPROVAL")
        self.assertEqual(self.count(task, "ExternalActionReceipt"), 0)
        self.approve(task)
        self.engine.run(task)
        self.assertEqual(self.engine.task(task)["state"]["state"], "COMPLETED")
        replay = self.engine.store.reconstruct(task)
        self.assertEqual(replay["state"], self.engine.task(task)["state"])
        self.assertEqual(replay["context"], self.engine.task(task)["context"])
        events = self.engine.events(task)
        self.assertEqual([e["sequence"] for e in events], list(range(1, len(events) + 1)))
        for row in self.engine.store.db.execute("SELECT payload FROM records"):
            validate(json.loads(row[0]))
        self.assertEqual(self.count(task, "PatchReceipt"), 1)
        self.assertEqual(self.count(task, "TestReceipt"), 1)

    def test_needs_changes_full_loop(self):
        task = self.create(reviews=("NEEDS_CHANGES", "ACCEPT"))
        self.to_action(task)
        self.assertEqual(self.count(task, "PatchReceipt"), 2)
        self.assertEqual(self.count(task, "TestReceipt"), 2)
        self.assertEqual(self.count(task, "ReviewDecision"), 2)
        self.approve(task)
        self.engine.run(task)
        self.assertEqual(self.engine.task(task)["state"]["state"], "COMPLETED")
        reviews = [json.loads(r[0])["decision"] for r in self.engine.store.db.execute("SELECT payload FROM records WHERE task_id=? AND kind='ReviewDecision' ORDER BY rowid", (task,))]
        self.assertEqual(reviews, ["NEEDS_CHANGES", "ACCEPT"])

    def test_state_matrix(self):
        for source in EDGES:
            for target in EDGES:
                expected = source not in TERMINAL and ((target in {"BLOCKED", "FAILED", "CANCELLED"} and source != target) or target in EDGES[source])
                self.assertEqual(allowed(source, target), expected, (source, target))

    def test_important_guards(self):
        task = self.create()
        state, ctx = self.engine.store.task(task)
        for target in ("IMPLEMENTING", "TESTING", "READY_FOR_PR", "PR_CREATED", "COMPLETED"):
            with self.assertRaises(Rejected), self.engine.store.transaction():
                self.engine._transition(state, ctx, target)
        self.engine.run(task)
        for _ in range(3):
            self.engine.run(task)
        self.assertEqual(self.count(task, "PatchReceipt"), 0)
        state, ctx = self.engine.store.task(task)
        with self.assertRaises(Rejected):
            self.engine._transition(state, ctx, "READY_FOR_IMPLEMENTATION")

    def test_implementation_failure_then_retry(self):
        task = self.create(implementation_failures=1)
        self.to_action(task)
        self.assertEqual(self.engine.task(task)["context"]["attempt"], 2)
        self.assertIn("implementation_failed", [e["event_type"] for e in self.engine.events(task)])

    def test_actual_test_failure_then_retry(self):
        task = self.create(test_failures=1)
        self.to_action(task)
        receipts = [json.loads(r[0]) for r in self.engine.store.db.execute("SELECT payload FROM records WHERE kind='TestReceipt' ORDER BY rowid")]
        self.assertEqual([r["exit_code"] for r in receipts], [1, 0])
        self.assertEqual([r["outcome"] for r in receipts], ["failed", "passed"])
        self.assertIn("greeting:", self.engine.store.read_artifact(receipts[0]["stdout"], task))

    def test_review_failure_and_reject_exhaust_budget(self):
        for scenario in ({"review_failure": True}, {"reviews": ("REJECT",)}):
            task = self.create(**scenario)
            self.to_action(task)
            self.assertEqual(self.engine.task(task)["state"]["state"], "FAILED")
            self.assertEqual(self.count(task, "PatchReceipt"), 3)

    def test_failed_plan(self):
        task = self.create(plan_failure=True)
        self.engine.run(task)
        self.assertEqual(self.engine.task(task)["state"]["state"], "FAILED")
        self.assertEqual(self.count(task, "ApprovalRequest"), 0)

    def test_approval_rejection_and_duplicate(self):
        task = self.create()
        self.engine.run(task)
        state = self.engine.task(task)["state"]
        first = self.approve(task, "reject")
        self.assertEqual(self.engine.approve(task, state["pending_approval"]["id"], "reject", state["revision"]), first)
        with self.assertRaises(Rejected):
            self.engine.approve(task, state["pending_approval"]["id"], "approve", state["revision"])
        self.assertEqual(self.engine.task(task)["state"]["state"], "PLANNING")
        self.engine.run(task)
        self.approve(task)
        self.engine.run(task)
        self.approve(task, "reject")
        self.assertEqual(self.count(task, "ExternalActionReceipt"), 0)
        self.engine.run(task)
        self.assertEqual(self.engine.task(task)["state"]["state"], "AWAITING_ACTION_APPROVAL")

    def test_action_failure(self):
        task = self.create(external_failure=True)
        self.to_action(task)
        self.approve(task)
        self.engine.run(task)
        self.assertEqual(self.engine.task(task)["state"]["state"], "FAILED")
        self.assertEqual(self.count(task, "ExternalActionReceipt"), 1)

    def test_crash_after_receipt_resumes_without_execution(self):
        task = self.create()
        self.engine.run(task)
        self.approve(task)
        def crash(stage):
            if stage == "tests":
                raise RuntimeError("simulated crash after commit")
        self.engine.after_operation = crash
        with self.assertRaises(RuntimeError):
            self.engine.run(task)
        self.engine.close()
        self.engine = Engine(self.temp.name, Executor("trusted-fixture"))
        with patch.object(self.engine.worker, "test", side_effect=AssertionError("test repeated")), patch.object(self.engine.worker, "implement", side_effect=AssertionError("implementation repeated")):
            self.engine.run(task)
        self.assertEqual(self.engine.task(task)["state"]["state"], "AWAITING_ACTION_APPROVAL")

    def test_external_duplicate_and_crash_recovery(self):
        task = self.create()
        self.to_action(task)
        state = self.engine.task(task)["state"]
        first = self.approve(task)
        self.assertEqual(first, self.engine.approve(task, state["pending_approval"]["id"], "approve", state["revision"]))
        def crash(stage):
            if stage == "external_action":
                raise RuntimeError("after external receipt")
        self.engine.after_operation = crash
        with self.assertRaises(RuntimeError):
            self.engine.run(task)
        self.engine.close()
        self.engine = Engine(self.temp.name, Executor("trusted-fixture"))
        self.engine.run(task)
        self.engine.run(task)
        self.assertEqual(self.count(task, "ExternalActionReceipt"), 1)
        self.assertEqual(self.engine.store.db.execute("SELECT count(*) FROM effects").fetchone()[0], 1)

    def test_duplicate_worker_receipt(self):
        task = self.create()
        self.to_action(task)
        receipt = self.engine.store.get(self.engine.task(task)["state"]["patch"], task)
        self.engine.store.put(receipt)
        self.assertEqual(self.count(task, "PatchReceipt"), 1)
        invalid = copy.deepcopy(receipt)
        invalid["changed_bytes"] += 1
        with self.assertRaises(Rejected):
            self.engine.store.put(invalid)

    def test_append_only_records(self):
        task = self.create()
        with self.assertRaises(sqlite3.IntegrityError):
            self.engine.store.db.execute("DELETE FROM events WHERE task_id=?", (task,))

    def test_api_auth_and_closed_approval_boundary(self):
        app = Application(self.engine)
        self.assertEqual(app.dispatch("POST", "/tasks", {}, "wrong")[0], 401)
        status, result = app.dispatch("POST", "/tasks", {}, app.session)
        self.assertEqual(status, 201)
        task = result["task_id"]
        app.dispatch("POST", f"/tasks/{task}/run", {}, app.session)
        state = self.engine.task(task)["state"]
        payload = {"request_id": state["pending_approval"]["id"], "decision": "approve", "expected_revision": state["revision"], "actor": "human"}
        self.assertEqual(app.dispatch("POST", f"/tasks/{task}/approvals", payload, app.session)[0], 409)
        self.assertEqual(app.dispatch("POST", f"/tasks/{task}/advance", {"approved": True}, app.session)[0], 409)
        self.assertEqual(self.count(task, "PatchReceipt"), 0)

    def test_contract_and_cross_task_rejection(self):
        task, other = self.create(), self.create()
        reference = self.engine.task(task)["state"]["spec"]
        with self.assertRaises(Rejected):
            self.engine.store.get(reference, other)
        invalid = self.engine.store.get(reference, task)
        invalid["schema_version"] = "99"
        with self.assertRaises(Rejected):
            self.engine.store.put(invalid)

    def test_reviewer_context(self):
        task = self.create()
        self.to_action(task)
        invocations = [json.loads(r[0]) for r in self.engine.store.db.execute("SELECT payload FROM records WHERE kind='AgentInvocation'")]
        review = next(i for i in invocations if i["role"] == "reviewer")
        inputs = [json.loads(self.engine.store.read_artifact(a, task)) for a in review["supplied_artifacts"]]
        self.assertEqual([i.get("contract_type", "diff") for i in inputs], ["TaskSpec", "ImplementationPlan", "PatchReceipt", "diff", "TestReceipt", "ReviewRequest"])
        self.assertNotEqual(review["id"], next(i for i in invocations if i["role"] == "junior")["id"])


if __name__ == "__main__":
    unittest.main()
