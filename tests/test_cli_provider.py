import asyncio
from dataclasses import asdict, replace
import json
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from contracts.interfaces import ValidatedContract
from orch.cli_provider import FixtureCliProvider, JsonCliProvider, ProviderConfig
from orch.contracts import Rejected, validate
from orch.engine import Engine
from orch.execution import Executor
from orch.process import capture
from orch.provider import Scenario


class CliProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = Engine(self.temp.name, Executor("trusted-fixture"), FixtureCliProvider)
        self.task = self.engine.create_task()
        self.engine.run(self.task)
        row = self.engine.store.db.execute("SELECT payload FROM records WHERE kind='AgentInvocation'").fetchone()
        self.invocation = json.loads(row[0])

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def invoke(self, provider, invocation=None):
        result = asyncio.run(provider.invoke(ValidatedContract("AgentInvocation", invocation or self.invocation))).payload
        validate(dict(result), "AgentInvocationReceipt")
        return result

    def test_both_prompt_deliveries(self):
        for delivery in ("stdin", "prompt_argument"):
            result = self.invoke(FixtureCliProvider(self.engine.store, delivery=delivery))
            self.assertIsNone(result["error"])
            plan = json.loads(self.engine.store.read_artifact(result["structured_result"], self.task))
            self.assertEqual(plan["planner_invocation_id"], self.invocation["id"])

    def test_failures_have_receipts_and_no_result(self):
        for behavior in ("invalid-json", "nonzero", "flood", "wait", "secret", "wrong-task", "wrong-shape"):
            with self.subTest(behavior=behavior):
                provider = FixtureCliProvider(self.engine.store, behavior=behavior)
                provider.config = replace(provider.config, timeout_seconds=1)
                result = self.invoke(provider)
                self.assertIsNotNone(result["error"])
                self.assertIsNone(result["structured_result"])
                if behavior == "nonzero":
                    self.assertEqual(result["exit_code"], 7)
                if behavior == "flood":
                    self.assertTrue(result["output_truncated"])
                if behavior == "secret":
                    self.assertNotIn("fixture-canary", self.engine.store.read_artifact(result["stdout"], self.task))

    def test_cancellation_reaps_fixture(self):
        provider = FixtureCliProvider(self.engine.store, behavior="wait")
        async def run():
            pending = asyncio.create_task(provider.invoke(ValidatedContract("AgentInvocation", self.invocation)))
            await asyncio.sleep(.1)
            await provider.cancel(self.invocation["id"])
            return (await pending).payload
        started = time.monotonic()
        result = asyncio.run(run())
        self.assertIsNotNone(result["error"])
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(provider.active, {})

    def test_live_providers_never_launch(self):
        fixture = FixtureCliProvider(self.engine.store)
        for name in ("github_copilot_cli", "abc_binary_ai_placeholder"):
            provider = JsonCliProvider(self.engine.store, replace(fixture.config, provider=name), fixture.transport)
            self.assertFalse(provider.capabilities().proposal_only_verified)
            with patch("orch.cli_provider.capture", side_effect=AssertionError("must not launch")):
                result = self.invoke(provider, {**self.invocation, "provider": name})
            self.assertIsNotNone(result["error"])
            self.assertIsNone(result["exit_code"])

    def test_invalid_configuration(self):
        config = asdict(FixtureCliProvider(self.engine.store).config)
        config["arguments"] = list(config["arguments"])
        for changes in ({"unknown": True}, {"executable": "relative.exe"}, {"timeout_seconds": 0},
                        {"arguments": ["prefix{prompt}"], "delivery": "prompt_argument"},
                        {"arguments": ["{prompt}", "{prompt}"], "delivery": "prompt_argument"}):
            with self.assertRaises(Rejected):
                ProviderConfig.parse({**config, **changes})

    def test_admission_budgets_and_context(self):
        for changes in ({"executable_sha256": "0" * 64}, {"max_prompt_bytes": 1}, {"arguments": ("-c", "print('bad')")}):
            provider = FixtureCliProvider(self.engine.store)
            provider.config = replace(provider.config, **changes)
            self.assertIsNotNone(self.invoke(provider)["error"])
        provider = FixtureCliProvider(self.engine.store)
        for changes in ({"workspace_id": "forbidden"}, {"supplied_artifacts": []}, {"model": "wrong"}):
            self.assertIsNotNone(self.invoke(provider, {**self.invocation, **changes})["error"])
        for raw in ('{"a":1,"a":2}', '{"a":NaN}'):
            with self.assertRaises(Rejected):
                provider.parse_result(raw, self.invocation, provider.prompt(self.invocation))

    def test_full_revision_workflow(self):
        task = self.engine.create_task(scenario=Scenario(reviews=("NEEDS_CHANGES", "ACCEPT")))
        self.engine.run(task)
        for expected in ("AWAITING_PLAN_APPROVAL", "AWAITING_ACTION_APPROVAL"):
            state = self.engine.task(task)["state"]
            self.assertEqual(state["state"], expected)
            self.engine.approve(task, state["pending_approval"]["id"], "approve", state["revision"])
            self.engine.run(task)
        self.assertEqual(self.engine.task(task)["state"]["state"], "COMPLETED")
        self.assertTrue(all(e["provider"] == "fixture_cli" for e in self.engine.events(task) if e["invocation_id"]))
        self.assertEqual(self.engine.store.db.execute("SELECT count(*) FROM records WHERE task_id=? AND kind='ReviewDecision'", (task,)).fetchone()[0], 2)

    def test_nonreading_stdin_cannot_block_deadline(self):
        started = time.monotonic()
        result = capture([sys.executable, "-I", "-c", "import time; time.sleep(30)"], input_bytes=b"x" * 1000000, timeout=.1)
        self.assertEqual(result["failure"], "timeout")
        self.assertLess(time.monotonic() - started, 5)
