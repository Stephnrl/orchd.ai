from datetime import datetime, timedelta, timezone
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
from orch.maintenance import backup, restore, audit
from orch.storage import Store


class ApprovalRenewalTests(unittest.TestCase):
    setUp = helpers.WorkflowTests.setUp
    tearDown = helpers.WorkflowTests.tearDown
    create = helpers.WorkflowTests.create
    approve = helpers.WorkflowTests.approve
    to_action = helpers.WorkflowTests.to_action
    count = helpers.WorkflowTests.count

    def pending(self, task):
        state = self.engine.task(task)["state"]
        request = self.engine.store.get(state["pending_approval"], task)
        return state, request

    def test_renewal_preserves_scope_requires_separate_decision_and_replays(self):
        for action in (False, True):
            with self.subTest(action=action):
                task = self.create()
                self.to_action(task) if action else self.engine.run(task)
                state, old = self.pending(task)
                with patch("orch.engine.now", return_value=old["expires_at"]):
                    with self.assertRaises(Rejected):
                        self.approve(task)
                    result = self.engine.renew_approval(task, old["id"], state["revision"])
                    self.assertEqual(result["state"]["state"], state["state"])
                    self.assertEqual(result["state"]["revision"], state["revision"] + 1)
                    _, fresh = self.pending(task)
                    self.assertNotEqual(fresh["id"], old["id"])
                    self.assertNotEqual(fresh["subject_sha256"], old["subject_sha256"])
                    self.assertEqual(fresh["subject"], old["subject"])
                    self.assertEqual(fresh["evidence"], old["evidence"])
                    self.assertGreater(fresh["expires_at"], old["expires_at"])
                    self.assertEqual(self.engine.run(task), result)
                    self.assertEqual(self.count(task, "ExternalActionReceipt"), 0)
                    with self.assertRaises(Rejected):
                        self.engine.approve(task, old["id"], "approve", state["revision"])
                    events = self.engine.events(task)
                    self.assertEqual(self.engine.renew_approval(task, old["id"], state["revision"]), result)
                    self.assertEqual(self.engine.events(task), events)
                    with tempfile.TemporaryDirectory() as base:
                        source, target = Path(base) / "backup", Path(base) / "restored"
                        backup(self.engine.store, source)
                        restore(source, target)
                        other = Store(target)
                        try:
                            audit(other)
                            self.assertEqual(other.task(task), self.engine.store.task(task))
                        finally:
                            other.close()
                    self.approve(task)
                    self.assertEqual(self.engine.task(task)["state"]["state"], "READY_FOR_PR" if action else "READY_FOR_IMPLEMENTATION")
                    if action:
                        self.engine.run(task)
                        self.assertEqual(self.engine.task(task)["state"]["state"], "COMPLETED")

    def test_current_request_stale_revision_and_cross_task_are_rejected(self):
        task = self.create()
        self.engine.run(task)
        state, old = self.pending(task)
        before = self.engine.task(task)
        with self.assertRaises(Rejected):
            self.engine.renew_approval(task, old["id"], state["revision"])
        with patch("orch.engine.now", return_value=old["expires_at"]):
            for request, revision in ((old["id"], True), (old["id"], state["revision"] - 1), (uid(), state["revision"])):
                with self.assertRaises(Rejected):
                    self.engine.renew_approval(task, request, revision)
            other = self.create()
            with self.assertRaises(Rejected):
                self.engine.renew_approval(other, old["id"], state["revision"])
        self.assertEqual(self.engine.task(task), before)

    def test_busy_and_unresolved_execution_reject_renewal(self):
        task = self.create()
        self.engine.run(task)
        state, old = self.pending(task)
        with patch("orch.engine.now", return_value=old["expires_at"]):
            with self.engine.store.exclusive(), self.assertRaises(Rejected):
                self.engine.renew_approval(task, old["id"], state["revision"])
            self.engine.store.db.execute("INSERT INTO operations(id,task_id,stage,slot,status) VALUES(?,?,?,'injected','started')", (uid(), task, "planning"))
            with self.assertRaises(Rejected):
                self.engine.renew_approval(task, old["id"], state["revision"])

    def test_changed_scope_is_not_silently_renewed(self):
        task = self.create()
        self.engine.run(task)
        state, old = self.pending(task)
        current, context = self.engine.store.task(task)
        current["spec"] = current["plan"]
        self.engine._event(current, context, "injected_scope_change")
        with patch("orch.engine.now", return_value=old["expires_at"]), self.assertRaises(Rejected):
            self.engine.renew_approval(task, old["id"], state["revision"])

    def test_cancelled_and_decided_requests_cannot_be_renewed(self):
        for cancel in (False, True):
            task = self.create()
            self.engine.run(task)
            state, old = self.pending(task)
            if cancel:
                self.engine.cancel(task, state["revision"], "Stop")
            else:
                self.approve(task)
            with patch("orch.engine.now", return_value=old["expires_at"]), self.assertRaises(Rejected):
                self.engine.renew_approval(task, old["id"], self.engine.task(task)["state"]["revision"])

    def test_api_auth_closed_fields_and_trusted_identity(self):
        task = self.create()
        self.engine.run(task)
        state, old = self.pending(task)
        app = Application(self.engine)
        payload = {"request_id": old["id"], "expected_revision": state["revision"]}
        route = f"/tasks/{task}/renew-approval"
        with patch("orch.engine.now", return_value=old["expires_at"]):
            self.assertEqual(app.dispatch("POST", route, payload, "wrong")[0], 401)
            for extra in ({"principal": "agent"}, {"expires_at": "2099-01-01T00:00:00Z"}, {"expected_revision": True}):
                self.assertEqual(app.dispatch("POST", route, {**payload, **extra}, app.session)[0], 409)
            code, result = app.dispatch("POST", route, payload, app.session)
            self.assertEqual(code, 200)
            self.assertEqual(result["context"]["approval_renewal"]["principal"], "local-operator")
            event = self.engine.events(task)[-1]
            self.assertEqual(event["event_type"], "approval_renewed")
            self.assertEqual(event["actor"]["role"], "human")

    def test_cli_renews_expired_request_without_docker(self):
        task = self.create()
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        with patch("orch.engine.now", return_value=past):
            self.engine.run(task)
        state, old = self.pending(task)
        result = subprocess.run([sys.executable, "-m", "orch", "renew-approval", "--data", str(self.engine.store.root),
                                 "--task", task, "--request-id", old["id"], "--expected-revision", str(state["revision"])],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["state"]["state"], "AWAITING_PLAN_APPROVAL")
        self.assertNotEqual(self.pending(task)[1]["id"], old["id"])


if __name__ == "__main__":
    unittest.main()
