from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orch.contracts import Rejected
from orch.engine import Engine
from orch.readiness import EXPECTED, assess_checks, code_digest, doctor, require_current_report, verify_runtime

IMAGE = "python@sha256:" + "a" * 64
INFO = {"id": "daemon-fixture", "os_type": "linux", "server_version": "fixture", "architecture": "amd64"}
DETAILS = {"id": "sha256:" + "b" * 64, "digests": [IMAGE]}


def success():
    return {"schema_version": "1.0.0", "expected": EXPECTED, "passed": EXPECTED, "failed": [], "errors": [], "skipped": [], "tests_run": len(EXPECTED)}


class ReadinessTests(unittest.TestCase):
    def test_missing_docker_is_unavailable_not_passed(self):
        with patch("orch.readiness.shutil.which", return_value=None):
            report = doctor(IMAGE)
            self.assertEqual(report["status"], "unavailable")
            self.assertEqual(report["reason"], "docker_cli_missing")

    def test_no_image_pull_or_docker_install(self):
        with patch("orch.readiness.shutil.which", return_value="docker"), patch("orch.readiness.docker_json", side_effect=[INFO, DETAILS]) as call:
            report = doctor(IMAGE)
            self.assertEqual(report["status"], "ready")
            self.assertEqual(report["image_id"], DETAILS["id"])
            self.assertEqual([c.args[0][1:3] for c in call.call_args_list], [["info", "--format"], ["image", "inspect"]])

    def test_daemon_failure_windows_mode_and_image_mismatch(self):
        cases = ([{**INFO, "os_type": "windows"}], [INFO, {**DETAILS, "digests": []}])
        with patch("orch.readiness.shutil.which", return_value="docker"):
            for responses in cases:
                with patch("orch.readiness.docker_json", side_effect=responses):
                    self.assertEqual(doctor(IMAGE)["status"], "unavailable")
            with patch("orch.readiness.docker_json", side_effect=Rejected("daemon unavailable")):
                self.assertEqual(doctor(IMAGE)["status"], "unavailable")

    def test_skipped_partial_or_malformed_checks_never_pass(self):
        process = {"code": 0, "failure": None}
        self.assertTrue(assess_checks(success(), process))
        for update in ({"skipped": EXPECTED}, {"passed": EXPECTED[:1]}, {"errors": ["test"]}, {"tests_run": 0}, {"extra": True}):
            self.assertFalse(assess_checks({**success(), **update}, process))
        self.assertFalse(assess_checks(success(), {"code": 2, "failure": None}))
        self.assertFalse(assess_checks(success(), {"code": 0, "failure": "timeout"}))
        old = sorted(["test_docker_integration.DockerIsolationTests.test_full_docker_workflow",
                      "test_docker_integration.DockerIsolationTests.test_linux_isolation_and_cleanup"])
        self.assertFalse(assess_checks({**success(), "expected": old, "passed": old, "tests_run": 2}, process))

    def preflight(self):
        with patch("orch.readiness.shutil.which", return_value="docker"), patch("orch.readiness.docker_json", side_effect=[INFO, DETAILS]):
            return doctor(IMAGE)

    def test_unavailable_report_persisted_without_launch(self):
        with tempfile.TemporaryDirectory() as root, patch("orch.readiness.shutil.which", return_value=None), patch("orch.readiness.capture", side_effect=AssertionError("no runtime")):
            report = verify_runtime(IMAGE, Path(root) / "report.json")
            self.assertEqual(report["status"], "unavailable")
            self.assertFalse(report["provider_authorized"])
            self.assertEqual(json.loads((Path(root) / "report.json").read_text()), report)

    def test_report_expiry_and_environment_binding(self):
        ready = self.preflight()
        process = {"code": 0, "failure": None, "stdout": json.dumps(success())}
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "report.json"
            with patch("orch.readiness.doctor", return_value=ready), patch("orch.readiness.capture", return_value=process):
                report = verify_runtime(IMAGE, path)
                self.assertEqual(report["status"], "passed")
                self.assertEqual(require_current_report(path, IMAGE), report)
                with self.assertRaises(Rejected):
                    verify_runtime(IMAGE, path)
            with patch("orch.readiness.doctor", return_value={**ready, "image_id": "different"}), self.assertRaises(Rejected):
                require_current_report(path, IMAGE)
            report["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            path.write_text(json.dumps(report))
            with self.assertRaises(Rejected):
                require_current_report(path, IMAGE)

    def test_environment_drift_or_skips_fail_verification(self):
        ready = self.preflight()
        with tempfile.TemporaryDirectory() as root:
            with patch("orch.readiness.doctor", side_effect=[ready, {**ready, "image_id": "different"}]), patch("orch.readiness.capture", return_value={"code": 0, "failure": None, "stdout": json.dumps(success())}):
                self.assertEqual(verify_runtime(IMAGE, Path(root) / "drift.json")["status"], "failed")
            with patch("orch.readiness.doctor", return_value=ready), patch("orch.readiness.capture", return_value={"code": 0, "failure": None, "stdout": json.dumps({**success(), "skipped": EXPECTED})}):
                self.assertEqual(verify_runtime(IMAGE, Path(root) / "skips.json")["status"], "failed")

    def test_nonmock_provider_blocked_before_storage_or_invocation(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "not-created"
            with self.assertRaises(Rejected):
                Engine(target, provider_factory=lambda *args: None)
            self.assertFalse(target.exists())
