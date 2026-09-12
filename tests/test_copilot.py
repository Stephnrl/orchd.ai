import asyncio
from contextlib import redirect_stdout
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator
from contracts.interfaces import ValidatedContract
from orch.contracts import Rejected
from orch.copilot import CopilotProvider, configuration, provider_check, result_schema
from orch.engine import Engine
from orch.execution import Executor


class CopilotProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = Engine(self.temp.name, Executor("trusted-fixture"))
        self.task = self.engine.create_task('Fixture "quotes" and $(not-a-command)')
        self.engine.run(self.task)
        state = self.engine.task(self.task)["state"]
        self.engine.approve(self.task, state["pending_approval"]["id"], "approve", state["revision"])
        self.engine.run(self.task)
        self.provider = CopilotProvider(self.engine.store, configuration(sys.executable, "0" * 64, "test-model"))
        self.requests, self.results = {}, {}
        for row in self.engine.store.db.execute("SELECT payload FROM records WHERE kind='AgentInvocation'"):
            invocation = json.loads(row[0])
            receipt = json.loads(self.engine.store.db.execute("SELECT payload FROM records WHERE kind='AgentInvocationReceipt' AND json_extract(payload,'$.invocation_id')=?", (invocation["id"],)).fetchone()[0])
            self.results[invocation["role"]] = self.engine.store.read_artifact(receipt["structured_result"], self.task)
            invocation.update(provider="github_copilot_cli", model="test-model", adapter_version="copilot-text-v1", prompt_version="copilot-role-v1")
            self.requests[invocation["role"]] = ValidatedContract("AgentInvocation", invocation)

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def test_three_role_native_prompts_and_output_conformance(self):
        for role, request in self.requests.items():
            with self.subTest(role=role), patch("subprocess.Popen", side_effect=AssertionError("No execution")):
                prepared = self.provider.prepare(request, "windows")
                self.assertFalse(prepared["provider_authorized"])
                self.assertEqual(prepared["argv"][-2], "-p")
                prompt = json.loads(prepared["argv"][-1])
                self.assertEqual(prompt["prompt_version"], "copilot-role-v1")
                self.assertTrue(prompt["proposal_policy"]["fixture_only"])
                self.assertNotIn("fixture_scenario", prompt)
                self.assertEqual(prompt["request_text"]["trust"], "untrusted_data")
                self.assertIn('$(not-a-command)', prepared["argv"][-1])
                result = self.provider.decode(self.results[role], request)
                Draft202012Validator.check_schema(prompt["response_schema"])
                Draft202012Validator(prompt["response_schema"]).validate(result)
                self.assertEqual(result, json.loads(self.results[role]))

    def test_rejects_protocol_noise_tools_and_binding_mismatches(self):
        request = self.requests["lead_planner"]
        result = json.loads(self.results["lead_planner"])
        invalid = ["```json\n" + self.results["lead_planner"] + "\n```", "Summary: " + self.results["lead_planner"],
                   '{"tool":"shell","command":"anything"}', '{"a":1,"a":2}', '{"a":NaN}', "[1]", '{"type":"assistant.message","data":{}}']
        for changes in ({"task_id": "other-task"}, {"planner_invocation_id": "other"}, {"allowed_paths": ["other.txt"]}, {"max_changed_bytes": 101}, {"permitted_tests": []}):
            invalid.append(json.dumps({**result, **changes}))
        invalid.append(json.dumps({**result, "risks": ["token=fixture-canary"]}).replace("token", "\\u0074oken"))
        for raw in invalid:
            with self.subTest(raw=raw[:40]), self.assertRaises(Rejected):
                self.provider.decode(raw, request)
        review = json.loads(self.results["reviewer"])
        review["request"]["sha256"] = "0" * 64
        with self.assertRaises(Rejected):
            self.provider.decode(json.dumps(review), self.requests["reviewer"])

    def test_fixed_config_and_no_live_admission(self):
        for model in (None, "--allow-all", "name with spaces", "a\nmodel"):
            with self.assertRaises(Rejected):
                configuration(sys.executable, "0" * 64, model)
        with self.assertRaises(Rejected):
            CopilotProvider(self.engine.store, replace(self.provider.config, arguments=("--allow-all",)))
        request = self.requests["lead_planner"]
        with patch("subprocess.Popen", side_effect=AssertionError("No execution")):
            health = asyncio.run(self.provider.health_check())
            receipt = asyncio.run(self.provider.invoke(request)).payload
        self.assertEqual(health.status, "unsupported")
        self.assertFalse(self.provider.capabilities().proposal_only_verified)
        self.assertIsNotNone(receipt["error"])
        self.assertIsNone(receipt["structured_result"])
        self.assertIsNone(receipt["exit_code"])
        with self.assertRaises(Rejected):
            Engine(Path(self.temp.name) / "must-not-exist", provider_factory=CopilotProvider)
        self.assertFalse((Path(self.temp.name) / "must-not-exist").exists())

    def test_prompt_limits_and_context_fail_closed(self):
        request = self.requests["lead_planner"]
        for changes in ({"model": "wrong"}, {"workspace_id": "forbidden"}, {"supplied_artifacts": []}):
            with self.assertRaises(Rejected):
                self.provider.prepare(ValidatedContract("AgentInvocation", {**request.payload, **changes}), "linux")
        with self.assertRaises(Rejected):
            self.provider.prepare(request, "other")
        original = self.provider.prompt
        with patch.object(self.provider, "prompt", side_effect=lambda inv: {**original(inv), "test_padding": "x" * 40000}):
            with self.assertRaises(Rejected):
                self.provider.prepare(request, "windows")
            self.assertFalse(self.provider.prepare(request, "linux")["provider_authorized"])
        with patch.object(self.provider, "prompt", return_value={"padding": "x" * 65537}):
            with self.assertRaises(Rejected):
                self.provider.prepare(request, "linux")
        with self.assertRaises(Rejected):
            self.provider.decode("x" * 65537, request)
        # Closed schema copies cannot mutate the central Phase 1 schema.
        schema = result_schema("ImplementationPlan")
        schema["$defs"].clear()
        self.assertTrue(result_schema("ImplementationPlan")["$defs"])


class ProviderInventoryTests(unittest.TestCase):
    def test_cli_reports_unavailable_without_opening_workflow_storage(self):
        from orch.__main__ import main
        for provider in ("github_copilot_cli", "abc_binary_ai_placeholder"):
            output = io.StringIO()
            with patch.object(sys, "argv", ["orch", "provider-check", "--provider", provider]), patch("orch.__main__.Engine", side_effect=AssertionError("No storage")), redirect_stdout(output):
                with self.assertRaises(SystemExit) as result:
                    main()
            self.assertEqual(result.exception.code, 2)
            self.assertFalse(json.loads(output.getvalue())["provider_authorized"])

    def test_read_only_inventory_never_authorizes_a_matching_file(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "candidate.exe"
            path.write_bytes(b"not executable; inventory must only read it")
            expected = hashlib.sha256(path.read_bytes()).hexdigest()
            with patch("subprocess.Popen", side_effect=AssertionError("No execution")):
                report = provider_check("github_copilot_cli", str(path), expected)
            self.assertEqual(report["executable_status"], "digest_match")
            self.assertFalse(report["provider_authorized"])
            self.assertEqual(report["version"], "unverified")
            self.assertEqual(report["authentication"], "unverified")
            self.assertEqual(provider_check("github_copilot_cli", str(path), "0" * 64)["executable_status"], "digest_mismatch")
            self.assertEqual(provider_check("github_copilot_cli", str(path))["executable_status"], "digest_unapproved")
            self.assertEqual(provider_check("github_copilot_cli", "relative.exe")["executable_status"], "invalid_path")
            self.assertEqual(provider_check("github_copilot_cli", root)["executable_status"], "missing_or_link")
            corporate = provider_check("abc_binary_ai_placeholder")
            self.assertIsNone(corporate["protocol"])
            self.assertIn("~/.abcplaceholder", corporate["location_hint"])
