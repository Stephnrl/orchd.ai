"""Finding the work that is yours, and being told about no other.

A container could claim, hold, work and release — provided somebody told it which task. This
is how it finds one. The listing is as interesting for what it withholds as for what it
returns: only roles this agent is enrolled for, only identifiers and state, never anything
waiting on a person, and never the contents of a task nobody has claimed.
"""
from datetime import datetime, timedelta
import os
from pathlib import Path
import secrets as randomness
import tempfile
import threading
import unittest
from unittest.mock import patch

from orch import agent_queue, task_drafts
from orch.agent_client import AgentClient
from orch.agent_service import AgentService
from orch.contracts import Rejected, now
from orch.engine import Engine
from orch.execution import Executor

SPEC = {"title": "Add a rate limit", "request": "Limit requests from one client.",
        "acceptance_criteria": ["A client over the limit receives 429"],
        "allowed_paths": ["api/limits.py"]}


class AgentQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store, self.agents = self.root / "store", self.root / "agents"
        self.engine = Engine(self.store, Executor("trusted-fixture"))
        self.addCleanup(self.engine.close)
        # Two waiting on the project manager, one already past that step.
        self.drafting = self.engine.create_task("Rate limiting", draft=True)
        self.also_drafting = self.engine.create_task("Something else", draft=True)
        self.planning = self.engine.create_task("Greeting fixture")     # SPEC_READY
        self.enrol("project-manager", "project_manager")
        self.enrol("lead-devops", "lead_planner")
        self.enrol("both", "project_manager", "lead_planner")

    def enrol(self, name, *roles):
        folder = self.agents / name
        folder.mkdir(parents=True)
        (folder / "role").write_text("\n".join(roles) + "\n", encoding="ascii")
        secret = folder / "secret"
        secret.write_text(randomness.token_hex(32) + "\n", encoding="ascii")
        os.chmod(secret, 0o600)

    def serve(self):
        service = AgentService(self.store, self.agents)
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (service.server.shutdown(), thread.join(30)))
        return "127.0.0.1:" + str(service.port)

    def client(self, name):
        return AgentClient(self.serve(), str(self.agents / name / "secret"))

    def listed(self, *roles):
        return agent_queue.available(self.engine, roles)

    # What is offered

    def test_an_agent_is_offered_the_work_its_role_is_next_for(self):
        report = self.listed("project_manager")
        self.assertEqual({row["task_id"] for row in report["available"]},
                         {self.drafting, self.also_drafting})
        self.assertEqual({row["state"] for row in report["available"]}, {"DRAFT_SPEC"})
        self.assertEqual({row["role"] for row in report["available"]}, {"project_manager"})

    def test_an_agent_is_not_told_about_work_belonging_to_another_role(self):
        planner = self.listed("lead_planner")
        self.assertEqual([row["task_id"] for row in planner["available"]], [self.planning])
        self.assertNotIn(self.drafting, [row["task_id"] for row in planner["available"]])

    def test_an_agent_enrolled_for_two_roles_sees_both(self):
        report = self.listed("project_manager", "lead_planner")
        self.assertEqual({row["task_id"] for row in report["available"]},
                         {self.drafting, self.also_drafting, self.planning})

    def test_a_role_the_agent_is_not_enrolled_for_offers_nothing(self):
        self.assertEqual(self.listed("junior")["available"], [])
        self.assertEqual(self.listed()["available"], [])

    # What is withheld

    def test_the_listing_carries_no_task_content(self):
        # Otherwise this becomes a way to read every task in the store without ever holding
        # one, which is the hole that holding exists to close.
        task_drafts.draft(self.engine, self.drafting, SPEC)
        [row] = [row for row in self.listed("project_manager")["available"]
                 if row["task_id"] == self.drafting]
        self.assertEqual(sorted(row), ["revision", "role", "state", "task_id"])
        self.assertNotIn(SPEC["title"], str(row))

    def test_work_waiting_on_a_person_is_offered_to_nobody(self):
        task_drafts.draft(self.engine, self.drafting, SPEC)
        task_drafts.ask(self.engine, self.drafting, [{"question_id": "q", "question": "Which?"}])
        for role in ("project_manager", "lead_planner", "human"):
            self.assertNotIn(self.drafting, [row["task_id"] for row in self.listed(role)["available"]])

    def test_a_held_task_is_not_offered_to_anyone(self):
        from orch import agent_sessions
        agent_sessions.claim(self.engine, self.drafting, "project-manager", "project_manager", 900)
        self.assertEqual([row["task_id"] for row in self.listed("project_manager")["available"]],
                         [self.also_drafting])

    def test_an_expired_lease_puts_the_work_back_on_the_list(self):
        from orch import agent_sessions
        agent_sessions.claim(self.engine, self.drafting, "project-manager", "project_manager", 900)
        later = (datetime.fromisoformat(now()) + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        with patch("orch.storage.now", return_value=later):
            offered = [row["task_id"] for row in self.listed("project_manager")["available"]]
        self.assertIn(self.drafting, offered)

    # The bound

    def test_the_bound_is_reported_so_an_agent_can_wait_instead_of_spinning(self):
        report = self.listed("project_manager")
        self.assertEqual(report["admitted"], 0)
        self.assertFalse(report["at_bound"])
        self.assertEqual(report["admission_bound"], self.engine.admission_bound())

    def test_at_the_bound_the_listing_says_so(self):
        from orch import agent_sessions
        agent_sessions.claim(self.engine, self.drafting, "project-manager", "project_manager", 900)
        with patch.object(self.engine, "admission_bound", return_value=1):
            report = agent_queue.available(self.engine, ["project_manager"])
        self.assertTrue(report["at_bound"])
        self.assertEqual(report["admitted"], 1)

    # Over the wire

    def test_an_agent_finds_its_own_work_and_claims_it(self):
        client = self.client("project-manager")
        report = client.available()
        self.assertEqual(report["count"], 2)
        self.assertFalse(report["at_bound"])
        task = report["available"][0]["task_id"]
        self.assertEqual(client.claim(task, "project_manager", 900)["status"], "claimed")
        # And having claimed it, it is no longer offered.
        self.assertNotIn(task, [row["task_id"] for row in client.available()["available"]])

    def test_the_wire_listing_is_scoped_to_the_secret_that_asked(self):
        planner = self.client("lead-devops").available()
        self.assertEqual([row["role"] for row in planner["available"]], ["lead_planner"])
        self.assertNotIn(self.drafting, [row["task_id"] for row in planner["available"]])

    def test_an_agent_enrolled_for_nothing_claimable_is_told_nothing(self):
        self.enrol("guardian-agent", "guardian")
        self.assertEqual(self.client("guardian-agent").available()["available"], [])

    def test_the_reply_authorizes_nothing(self):
        self.assertIs(self.client("project-manager").available()["live_authorized"], False)


if __name__ == "__main__":
    unittest.main()
