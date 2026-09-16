"""A project manager container doing the work, not just holding the task.

Every capability built before this was reachable only from the command line, so an agent
could authenticate, claim a task, renew its lease, release it — and do nothing with it. These
tests fix the three routes that close that, and the condition that makes them safe: an agent
works on a task it is *holding*, and a lease is only ever granted for the role that task's
next step needs.

They also fix what stays out of reach. Nothing an agent can call answers a clarification or
confirms a specification, because both are a person's act and are recorded as one.
"""
from datetime import datetime, timedelta
import os
from pathlib import Path
import secrets as randomness
import tempfile
import threading
import unittest
from unittest.mock import patch

from orch import agent_sessions, task_drafts
from orch.agent_client import AgentClient
from orch.agent_service import ROUTES, WORK, AgentService
from orch.contracts import Rejected, now
from orch.engine import Engine
from orch.execution import Executor

SPEC = {"title": "Add a rate limit to the public API",
        "request": "Requests from one client should be limited.",
        "acceptance_criteria": ["A client over the limit receives 429"],
        "constraints": ["No new runtime dependency"],
        "allowed_paths": ["api/limits.py"]}
QUESTIONS = [{"question_id": "window", "question": "Over what window should the limit apply?"}]


class AgentWorkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store, self.agents = self.root / "store", self.root / "agents"
        engine = Engine(self.store, Executor("trusted-fixture"))
        try:
            # Two tasks waiting to be specified, which is the project manager's step.
            self.task = engine.create_task("Rate limiting", draft=True)
            self.other = engine.create_task("Something else", draft=True)
        finally:
            engine.close()
        self.enrol("project-manager", "project_manager")
        self.enrol("second-manager", "project_manager")
        self.enrol("junior-devops", "junior")
        self.service = AgentService(self.store, self.agents)
        self.thread = threading.Thread(target=self.service.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(lambda: (self.service.server.shutdown(), self.thread.join(30)))
        self.endpoint = "127.0.0.1:" + str(self.service.port)

    def enrol(self, name, *roles):
        folder = self.agents / name
        folder.mkdir(parents=True)
        (folder / "role").write_text("\n".join(roles) + "\n", encoding="ascii")
        secret = folder / "secret"
        secret.write_text(randomness.token_hex(32) + "\n", encoding="ascii")
        os.chmod(secret, 0o600)
        return secret

    def client(self, name):
        return AgentClient(self.endpoint, str(self.agents / name / "secret"))

    def holding(self, name="project-manager", task=None):
        client = self.client(name)
        client.claim(task or self.task, "project_manager", 900)
        return client

    # The work itself

    def test_a_holding_agent_can_read_write_and_revise_a_specification(self):
        client = self.holding()
        empty = client.specification(self.task)["draft"]
        self.assertIsNone(empty["spec"])
        written = client.draft(self.task, SPEC)["draft"]
        self.assertEqual(written["title"], SPEC["title"])
        self.assertEqual(written["spec_revision"], 0)
        revised = client.draft(self.task, {**SPEC, "title": "Rate limit the public API"})["draft"]
        self.assertEqual(revised["spec_revision"], 1)
        self.assertEqual(client.specification(self.task)["draft"]["spec_sha256"],
                         revised["spec_sha256"])

    def test_an_agent_can_stop_on_a_question_only_a_person_can_answer(self):
        client = self.holding()
        client.draft(self.task, SPEC)
        asked = client.ask(self.task, QUESTIONS)["draft"]
        self.assertEqual(asked["state"], "AWAITING_CLARIFICATION")
        self.assertEqual([q["question_id"] for q in asked["pending_questions"]], ["window"])
        # And having asked, it cannot carry on as though it had not.
        with self.assertRaises(Rejected):
            client.draft(self.task, SPEC)

    def test_a_draft_through_the_api_is_the_same_draft_the_kernel_would_have_written(self):
        client = self.holding()
        client.draft(self.task, SPEC)
        engine = Engine(self.store, Executor("trusted-fixture"))
        try:
            direct = task_drafts.show(engine, self.task)
            self.assertEqual(direct["spec_sha256"], client.specification(self.task)["draft"]["spec_sha256"])
            self.assertEqual(direct["specification"]["acceptance_criteria"], SPEC["acceptance_criteria"])
        finally:
            engine.close()

    def test_a_stale_revision_is_refused_through_the_api_too(self):
        client = self.holding()
        first = client.draft(self.task, SPEC)["draft"]
        client.draft(self.task, {**SPEC, "title": "Moved on"})
        with self.assertRaises(Rejected):
            client.draft(self.task, SPEC, expected_revision=first["revision"])

    # Holding it is the condition

    def test_an_agent_that_has_not_claimed_the_task_cannot_write_to_it(self):
        client = self.client("project-manager")
        for call in (lambda: client.specification(self.task),
                     lambda: client.draft(self.task, SPEC),
                     lambda: client.ask(self.task, QUESTIONS)):
            with self.assertRaises(Rejected):
                call()
        engine = Engine(self.store, Executor("trusted-fixture"))
        try:
            self.assertIsNone(engine.store.task(self.task)[0]["spec"], "nothing was written")
        finally:
            engine.close()

    def test_one_agent_cannot_write_to_a_task_another_is_holding(self):
        self.holding("project-manager")
        intruder = self.client("second-manager")
        with self.assertRaises(Rejected):
            intruder.draft(self.task, SPEC)

    def test_holding_one_task_is_no_licence_to_write_to_another(self):
        client = self.holding("project-manager", self.task)
        with self.assertRaises(Rejected):
            client.draft(self.other, SPEC)

    def test_an_expired_lease_stops_the_work_it_was_holding(self):
        client = self.holding()
        client.draft(self.task, SPEC)
        later = (datetime.fromisoformat(now()) + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        with patch("orch.storage.now", return_value=later):
            with self.assertRaises(Rejected):
                client.draft(self.task, {**SPEC, "title": "After the lease ran out"})

    def test_a_junior_cannot_hold_a_task_waiting_on_the_project_manager(self):
        # The role gate is at the claim, so a junior never reaches the work routes at all.
        junior = self.client("junior-devops")
        with self.assertRaises(Rejected):
            junior.claim(self.task, "junior")
        with self.assertRaises(Rejected):
            junior.draft(self.task, SPEC)

    # What stays out of reach

    def test_no_route_an_agent_can_call_confirms_or_answers(self):
        # An agent drafts; a person decides. There is deliberately no route for either, and
        # this asserts the route table rather than trusting the absence.
        self.assertEqual(sorted(ROUTES), ["agent-ask", "agent-claim", "agent-draft",
                                          "agent-identity", "agent-release", "agent-renew",
                                          "agent-sessions", "agent-spec"])
        for forbidden in ("agent-confirm", "agent-answer", "agent-approve"):
            self.assertNotIn(forbidden, ROUTES)
            with self.assertRaisesRegex(Rejected, "Unknown agent route"):
                self.client("project-manager").call(forbidden, {"task_id": self.task})

    def test_a_confirmed_specification_is_beyond_the_agent_that_wrote_it(self):
        client = self.holding()
        client.draft(self.task, SPEC)
        sha = client.specification(self.task)["draft"]["spec_sha256"]
        engine = Engine(self.store, Executor("trusted-fixture"))
        try:
            task_drafts.confirm(engine, self.task, sha, "steph")   # the human boundary
        finally:
            engine.close()
        with self.assertRaises(Rejected):
            client.draft(self.task, {**SPEC, "title": "After a person signed it"})

    def test_every_work_route_requires_the_task_and_carries_no_agent_name(self):
        for route in WORK:
            request = ROUTES[route][0]
            from orch.agent_service import WIRE_SCHEMA
            properties = WIRE_SCHEMA["$defs"][request]["properties"]
            self.assertIn("task_id", properties)
            self.assertNotIn("agent", properties, route + " must not name an agent")

    def test_a_released_task_is_no_longer_writable_by_the_agent_that_held_it(self):
        client = self.holding()
        client.draft(self.task, SPEC)
        client.release(self.task)
        with self.assertRaises(Rejected):
            client.draft(self.task, {**SPEC, "title": "After letting go"})


if __name__ == "__main__":
    unittest.main()
