"""Portable, fail-closed runtime evidence. Never installs Docker or pulls images."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sys

from .broker import atomic_json, source_digest
from .contracts import Rejected, digest, now, uid
from .execution import Executor
from .process import capture

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = sorted(["test_docker_integration.DockerIsolationTests.test_full_docker_workflow",
                   "test_docker_integration.DockerIsolationTests.test_linux_isolation_and_cleanup"])


def code_digest():
    files = list((ROOT / "orch").glob("*.py")) + list((ROOT / "contracts").rglob("*.json"))
    files += [ROOT / "contracts/interfaces.py", ROOT / "tests/test_docker_integration.py"]
    return digest({str(path.relative_to(ROOT).as_posix()): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(files)})


def host():
    return {"system": platform.system(), "release": platform.release(), "machine": platform.machine(),
            "hostname_sha256": hashlib.sha256(platform.node().encode()).hexdigest(), "python": platform.python_version()}


def docker_json(argv):
    # Match the executor's host environment; ambient DOCKER_HOST overrides must
    # not qualify a different daemon from the one the broker actually uses.
    env = {k: os.environ[k] for k in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP") if k in os.environ}
    result = capture(argv, timeout=15, limit=65536, env=env)
    if result["code"] != 0 or result["failure"]:
        raise Rejected("Docker command unavailable or failed")
    try:
        return json.loads(result["stdout"])
    except (ValueError, TypeError) as exc:
        raise Rejected("Malformed Docker response") from exc


def doctor(image=None):
    report = {"schema_version": "1.0.0", "kind": "RuntimePreflight", "id": uid(), "created_at": now(),
              "host": host(), "status": "unavailable", "reason": None, "docker": None, "image": image, "image_id": None}
    executable = shutil.which("docker")
    if not executable:
        report["reason"] = "docker_cli_missing"
        return report
    try:
        info = docker_json([executable, "info", "--format", '{"id":{{json .ID}},"os_type":{{json .OSType}},"server_version":{{json .ServerVersion}},"architecture":{{json .Architecture}}}'])
        if set(info) != {"id", "os_type", "server_version", "architecture"} or any(not isinstance(v, str) or not v for v in info.values()):
            raise Rejected("Incomplete daemon identity")
        report["docker"] = info
        if info["os_type"] != "linux":
            report["reason"] = "linux_containers_required"
            return report
        if not image:
            report["reason"] = "digest_pinned_image_required"
            return report
        Executor("docker", image)  # The same image grammar enforced by execution.
        details = docker_json([executable, "image", "inspect", image, "--format", '{"id":{{json .Id}},"digests":{{json .RepoDigests}}}'])
        if not isinstance(details, dict) or not re.fullmatch(r"sha256:[a-f0-9]{64}", str(details.get("id", ""))):
            raise Rejected("Invalid local image identity")
        digests = details.get("digests")
        if not isinstance(digests, list) or not any(isinstance(d, str) and d.endswith("@" + image.split("@", 1)[1]) for d in digests):
            raise Rejected("Requested image digest is not locally verified")
        report.update(status="ready", reason=None, image_id=details["id"])
    except (Rejected, OSError, TypeError, ValueError):
        report["reason"] = "daemon_or_image_unavailable"
    return report


def assess_checks(result, process):
    if process["code"] != 0 or process["failure"]:
        return False
    if not isinstance(result, dict) or set(result) != {"schema_version", "expected", "passed", "failed", "errors", "skipped", "tests_run"}:
        return False
    return (result["schema_version"] == "1.0.0" and result["expected"] == EXPECTED
            and result["passed"] == EXPECTED and result["tests_run"] == len(EXPECTED)
            and result["failed"] == [] and result["errors"] == [] and result["skipped"] == [])


def verify_runtime(image, destination):
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise Rejected("Use a new report path; existing evidence is never overwritten")
    preflight = doctor(image)
    report = {"schema_version": "1.0.0", "kind": "RuntimeVerification", "id": uid(), "started_at": now(),
              "ended_at": None, "expires_at": None, "source_digest": code_digest(), "status": "unavailable",
              "preflight": preflight, "checks": None, "reason": preflight["reason"], "provider_authorized": False}
    if preflight["status"] == "ready":
        env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP") if key in os.environ}
        env["PYTHONPATH"] = os.pathsep.join(sys.path)
        env["ORCH_DOCKER_TEST_IMAGE"] = image
        try:
            process = capture([sys.executable, "-m", "orch.runtime_checks"], cwd=ROOT, env=env, timeout=180, limit=65536)
            checks = json.loads(process["stdout"])
            report["checks"] = checks
            passed = assess_checks(checks, process)
            # Recheck daemon/image/code after the test process to detect drift.
            latest = doctor(image)
            unchanged = all(latest[key] == preflight[key] for key in ("host", "docker", "image", "image_id", "status")) and report["source_digest"] == code_digest()
            report["status"] = "passed" if passed and unchanged else "failed"
            report["reason"] = None if report["status"] == "passed" else "required_checks_failed_skipped_or_environment_changed"
        except (OSError, ValueError, Rejected):
            report.update(status="failed", reason="verification_process_failed")
    report["ended_at"] = now()
    if report["status"] == "passed":
        report["expires_at"] = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat().replace("+00:00", "Z")
    destination.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(destination, report)
    return report


def require_current_report(path, image):
    """Trusted local evidence check; never sufficient to authorize a real provider."""
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise Rejected("Invalid readiness report")
    try:
        report = json.loads(path.read_text())
        if report["schema_version"] != "1.0.0" or report["kind"] != "RuntimeVerification" or report["status"] != "passed":
            raise Rejected("Runtime verification has not passed")
        expiry = datetime.fromisoformat(report["expires_at"])
        if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc) or report["source_digest"] != code_digest():
            raise Rejected("Runtime evidence expired or code changed")
        if not assess_checks(report["checks"], {"code": 0, "failure": None}):
            raise Rejected("Missing required isolation evidence")
        current = doctor(image)
        if current["status"] != "ready" or any(current[k] != report["preflight"][k] for k in ("host", "docker", "image", "image_id")):
            raise Rejected("Runtime environment no longer matches evidence")
        return report
    except (ValueError, KeyError, TypeError) as exc:
        raise Rejected("Invalid readiness evidence") from exc
