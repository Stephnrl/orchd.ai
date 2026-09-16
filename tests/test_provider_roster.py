"""Which provider answers for which role, and what a roster may not quietly do.

The engine took one provider factory and used it for every invocation, so every record named
the same provider and model whatever role produced it. That was true while there was one
provider and stops being true the moment a junior runs on one model and a lead on another.

Two properties matter more than the mapping itself: a roster cannot introduce a provider the
engine would otherwise refuse, and it cannot claim a model that did not answer.
"""
import json
import unittest

import test_workflow as helpers
from orch.cli_provider import FixtureCliProvider
from orch.contracts import Rejected
from orch.engine import Engine
from orch.execution import Executor
from orch.provider import MockProvider
from orch.provider_roster import ADMITTED, Roster, load, name, offered


class RosterTests(unittest.TestCase):
    def test_a_role_nobody_named_gets_the_default(self):
        roster = Roster("in-process", {"junior": "cli"})
        self.assertIs(roster.for_role("junior"), FixtureCliProvider)
        self.assertIs(roster.for_role("lead_planner"), MockProvider)
        self.assertIs(roster.for_role("a_role_that_does_not_exist"), MockProvider)

    def test_a_roster_reports_what_each_provider_actually_runs(self):
        report = Roster("in-process", {"junior": "cli"}).report()
        self.assertEqual(report["default"], {"provider": "in-process", "model": "deterministic-v1"})
        self.assertEqual(report["roles"], {"junior": {"provider": "cli", "model": "fixture-v1"}})
        self.assertIs(report["live_authorized"], False)

    # What it may not do

    def test_a_roster_cannot_introduce_a_provider_the_engine_would_refuse(self):
        # Configuration must not become a way past admission.
        for entry in ("github_copilot_cli", "abc_binary_ai_placeholder", "anything_else"):
            with self.subTest(provider=entry):
                with self.assertRaisesRegex(Rejected, "Unknown provider"):
                    Roster(entry)
                with self.assertRaisesRegex(Rejected, "Unknown provider"):
                    Roster("in-process", {"junior": entry})

    def test_a_roster_cannot_claim_a_model_that_did_not_answer(self):
        # An invocation naming a model that never ran is false evidence, and the evidence is
        # the product. The refusal is also the useful part: it says what is missing.
        with self.assertRaisesRegex(Rejected, "has not been admitted"):
            Roster("in-process", {"junior": {"provider": "in-process", "model": "claude-sonnet-5"}})
        with self.assertRaisesRegex(Rejected, "runs deterministic-v1"):
            Roster("in-process", model="claude-opus-5")

    def test_naming_the_model_a_provider_does_run_is_accepted(self):
        roster = Roster("in-process", {"junior": {"provider": "cli", "model": "fixture-v1"}})
        self.assertIs(roster.for_role("junior"), FixtureCliProvider)

    def test_a_roster_is_bounded_and_strictly_shaped(self):
        with self.assertRaisesRegex(Rejected, "at most"):
            Roster("in-process", {("role_%d" % n): "cli" for n in range(20)})
        for broken in ({"junior": {"model": "fixture-v1"}}, {"junior": {"provider": "cli", "extra": 1}},
                       {"junior": 7}):
            with self.assertRaisesRegex(Rejected, "roster entry"):
                Roster("in-process", broken)

    def test_loading_refuses_anything_it_was_not_given(self):
        self.assertIs(load(None).for_role("junior"), MockProvider)
        with self.assertRaisesRegex(Rejected, "object of default, roles"):
            load({"default": "in-process", "unexpected": True})
        with self.assertRaisesRegex(Rejected, "object of default, roles"):
            load(["in-process"])

    def test_every_admitted_provider_declares_the_model_it_runs(self):
        for label, factory in ADMITTED.items():
            with self.subTest(provider=label):
                self.assertEqual(name(factory), label)
                self.assertTrue(offered(factory))


class EngineRosterTests(unittest.TestCase):
    """What the record says about which provider produced it."""

    def setUp(self):
        helpers.WorkflowTests.setUp(self)

    def tearDown(self):
        helpers.WorkflowTests.tearDown(self)

    def invocations(self, task):
        return [json.loads(row[0]) for row in self.engine.store.db.execute(
            "SELECT payload FROM records WHERE task_id=? AND kind='AgentInvocation'", (task,))]

    def test_an_engine_with_no_roster_behaves_as_it_always_did(self):
        task = self.engine.create_task()
        self.engine.run(task)
        providers = {record["provider"] for record in self.invocations(task)}
        self.assertEqual(providers, {"mock"})

    def test_each_role_records_the_provider_that_answered_for_it(self):
        engine = Engine(self.temp.name + "-roster", Executor("trusted-fixture"),
                        providers=Roster("in-process", {"lead_planner": "cli"}))
        self.addCleanup(engine.close)
        task = engine.create_task()
        engine.run(task)
        by_role = {record["role"]: record["provider"] for record in
                   [json.loads(row[0]) for row in engine.store.db.execute(
                       "SELECT payload FROM records WHERE task_id=? AND kind='AgentInvocation'", (task,))]}
        self.assertEqual(by_role.get("lead_planner"), "fixture_cli")
        self.assertNotIn("mock", [by_role.get("lead_planner")])

    def test_the_model_recorded_is_the_one_that_provider_runs(self):
        engine = Engine(self.temp.name + "-models", Executor("trusted-fixture"),
                        providers=Roster("in-process", {"lead_planner": "cli"}))
        self.addCleanup(engine.close)
        task = engine.create_task()
        engine.run(task)
        records = [json.loads(row[0]) for row in engine.store.db.execute(
            "SELECT payload FROM records WHERE task_id=? AND kind='AgentInvocation'", (task,))]
        for record in records:
            with self.subTest(role=record["role"]):
                expected = "fixture-v1" if record["role"] == "lead_planner" else "deterministic-v1"
                self.assertEqual(record["model"], expected)

    def test_a_roster_cannot_smuggle_a_provider_past_the_engine(self):
        with self.assertRaises(Rejected):
            Engine(self.temp.name + "-nope", Executor("trusted-fixture"),
                   providers=Roster("github_copilot_cli"))


if __name__ == "__main__":
    unittest.main()
