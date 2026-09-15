import hashlib
import io
import json
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from orch.contracts import Rejected
from orch.deployment import deployment_check


class DeploymentTests(unittest.TestCase):
    def test_missing_prerequisites_do_not_probe_or_create_storage(self):
        from orch.__main__ import main
        for provider in ("github_copilot_cli", "abc_binary_ai_placeholder"):
            output = io.StringIO()
            with patch.object(sys, "argv", ["orch", "deployment-check", "--provider", provider]), patch("orch.__main__.Engine", side_effect=AssertionError("No store")), patch("subprocess.Popen", side_effect=AssertionError("No process")), redirect_stdout(output):
                with self.assertRaises(SystemExit) as result:
                    main()
            self.assertEqual(result.exception.code, 2)
            report = json.loads(output.getvalue())
            self.assertEqual(report["status"], "blocked")
            self.assertFalse(report["provider_authorized"])
            self.assertEqual(len(report["blocking_gates"]), 7)
            self.assertIn("broker_identity", report["blocking_gates"])
            self.assertEqual(report["broker_identity"]["status"], "not_supplied")
            self.assertEqual(report["worker_runtime"]["status"], "not_supplied")

    def test_matching_binary_and_current_worker_evidence_never_authorize(self):
        with tempfile.TemporaryDirectory() as root:
            executable = Path(root) / "candidate.exe"
            executable.write_bytes(b"inventory only")
            expected = hashlib.sha256(executable.read_bytes()).hexdigest()
            with patch("orch.deployment.require_current_report", return_value={}) as verify, patch("subprocess.Popen", side_effect=AssertionError("No provider execution")):
                report = deployment_check("github_copilot_cli", str(executable), expected, "pinned-image", "report.json")
            verify.assert_called_once_with("report.json", "pinned-image")
            self.assertEqual(report["worker_runtime"]["status"], "current")
            self.assertNotIn("worker_runtime", report["blocking_gates"])
            self.assertNotIn("executable_digest", report["blocking_gates"])
            self.assertEqual(len(report["blocking_gates"]), 5)
            self.assertIn("broker_identity", report["blocking_gates"])
            self.assertFalse(report["provider_authorized"])
            self.assertFalse(report["provider"]["provider_authorized"])
            self.assertEqual(report["status"], "blocked")

    def test_rejected_runtime_evidence_is_not_promoted(self):
        for error in (Rejected("expired"), OSError("unreadable")):
            with patch("orch.deployment.require_current_report", side_effect=error):
                report = deployment_check("github_copilot_cli", image="image", runtime_report="report")
            self.assertEqual(report["worker_runtime"]["status"], "rejected")
            self.assertIn("worker_runtime", report["blocking_gates"])
            self.assertFalse(report["provider_authorized"])
        with tempfile.TemporaryDirectory() as root:
            forged = Path(root) / "forged.json"
            forged.write_text('{"status":"passed","provider_authorized":true}')
            with patch("subprocess.Popen", side_effect=AssertionError("Malformed report must fail before Docker probe")):
                report = deployment_check("github_copilot_cli", image="image", runtime_report=str(forged))
            self.assertEqual(report["worker_runtime"]["status"], "rejected")

    def test_protocol_guidance_and_required_arguments(self):
        from orch.__main__ import main
        corporate = deployment_check("abc_binary_ai_placeholder")
        native = next(gate for gate in corporate["gates"] if gate["id"] == "native_protocol")
        self.assertIn("corporate CLI", native["next_step"])
        with self.assertRaises(Rejected):
            deployment_check("github_copilot_cli", runtime_report="report")
        for argv in (["orch", "deployment-check"], ["orch", "deployment-check", "--provider", "github_copilot_cli", "--runtime-report", "report"]):
            with patch.object(sys, "argv", argv), redirect_stderr(io.StringIO()), patch("orch.__main__.Engine", side_effect=AssertionError("No store")):
                with self.assertRaises(SystemExit) as result:
                    main()
            self.assertEqual(result.exception.code, 2)
