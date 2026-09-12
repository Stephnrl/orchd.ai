from unittest.mock import patch
import unittest

import test_runtime as runtime
from orch.api import Application
from orch.contracts import Rejected, uid


class RecoveryDiagnosticsTests(unittest.TestCase):
    setUp = runtime.RuntimeTests.setUp
    tearDown = runtime.RuntimeTests.tearDown
    create = runtime.RuntimeTests.create
    approve = runtime.RuntimeTests.approve
    interrupted_broker = runtime.RuntimeTests.interrupted_broker

    def test_supported_recovery_is_advisory_and_does_not_probe_or_mutate(self):
        task, state = self.interrupted_broker()
        before = self.engine.task(task)
        events = self.engine.events(task)
        operations = [tuple(row) for row in self.engine.store.db.execute("SELECT * FROM operations")]
        with patch.object(self.engine.executor.broker, "result", side_effect=AssertionError("No broker probe")), patch.object(self.engine.executor, "reconcile", side_effect=AssertionError("No Docker probe")):
            report = self.engine.recovery_diagnostics(task)
        self.assertEqual(report["recovery_status"], "preconditions_met")
        self.assertFalse(report["runtime_checked"])
        self.assertEqual(report["revision"], state["revision"])
        self.assertEqual(report["unresolved_operations"][0]["id"], state["active_operation_id"])
        self.assertEqual(self.engine.task(task), before)
        self.assertEqual(self.engine.events(task), events)
        self.assertEqual([tuple(row) for row in self.engine.store.db.execute("SELECT * FROM operations")], operations)
        self.engine.recover(task, state["revision"])
        self.assertEqual(self.engine.recovery_diagnostics(task)["recovery_status"], "not_applicable")

    def test_expired_approval_explains_recovery_limit(self):
        task, _ = self.interrupted_broker()
        context = self.engine.task(task)["context"]
        request = self.engine.store.get(context["plan_approval_request"], task)
        with patch("orch.engine.now", return_value=request["expires_at"]):
            report = self.engine.recovery_diagnostics(task)
        self.assertEqual(report["recovery_status"], "unavailable")
        self.assertIn("expired", " ".join(report["reasons"]))
        self.assertIn("cannot extend", " ".join(report["reasons"]))

    def test_unsupported_stage_and_mismatched_operation_are_unavailable(self):
        task, _ = self.interrupted_broker()
        state, context = self.engine.store.task(task)
        state["resume_state"] = "PLANNING"
        state["active_operation_id"] = uid()
        self.engine._event(state, context, "injected_inconsistent_state")
        report = self.engine.recovery_diagnostics(task)
        self.assertEqual(report["recovery_status"], "unavailable")
        self.assertEqual(len(report["reasons"]), 2)

    def test_api_auth_query_rejection_unknown_task_and_busy_dispatcher(self):
        task = self.create()
        app = Application(self.engine)
        route = f"/tasks/{task}/recovery-diagnostics"
        self.assertEqual(app.dispatch("GET", route, {}, "wrong")[0], 401)
        self.assertEqual(app.dispatch("GET", route + "?probe=true", {}, app.session)[0], 409)
        self.assertEqual(app.dispatch("GET", "/tasks/unknown/recovery-diagnostics", {}, app.session)[0], 409)
        self.assertEqual(app.dispatch("POST", route, {}, app.session)[0], 404)
        self.assertEqual(app.dispatch("GET", route, {}, app.session)[1]["recovery_status"], "not_applicable")
        with self.engine.store.exclusive():
            self.assertEqual(app.dispatch("GET", route, {}, app.session)[0], 409)

    def test_operation_details_are_bounded_and_do_not_expose_results(self):
        task, _ = self.interrupted_broker()
        for index in range(25):
            self.engine.store.db.execute("INSERT INTO operations(id,task_id,stage,slot,status,result) VALUES(?,?,?,?,'started',?)", (uid(), task, "tests", str(index + 100), "private result"))
        report = self.engine.recovery_diagnostics(task)
        self.assertTrue(report["operations_truncated"])
        self.assertEqual(len(report["unresolved_operations"]), 20)
        self.assertEqual(report["recovery_status"], "unavailable")
        self.assertNotIn("private result", str(report))


if __name__ == "__main__":
    unittest.main()
