"""Loopback broker service: authenticated fixed routes, a broker-owned journal and recovery without relaunch."""
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import test_workflow as helpers
from orch import broker as brokermod
from orch.broker import ServiceClient, VERSION, assess_identity, endpoint, identity, load_secret, separate, sign
from orch.broker_service import BrokerService
from orch.contracts import Rejected, canonical, digest, now, uid
from orch.deployment import deployment_check
from orch.engine import Engine
from orch.execution import Executor
from orch.fixtures import EDIT_CODE
from orch.maintenance import audit, integrity_report
from orch.storage import Store

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "python@sha256:" + "a" * 64


def write_secret(path):
    path.write_text(secrets.token_hex(32), encoding="ascii")
    os.chmod(path, 0o600)
    return path


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Harness:
    """A broker service thread with its own journal directory and secret, apart from the store."""

    def __init__(self, workspaces, mode="trusted-fixture", image=None):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.secret = write_secret(root / "secret")
        self.journal = root / "journal"
        self.service = BrokerService(self.journal, self.secret, workspaces, mode, image, 0)
        self.thread = threading.Thread(target=self.service.serve_forever, daemon=True)
        self.thread.start()
        self.config = {"endpoint": "127.0.0.1:" + str(self.service.port), "secret": str(self.secret)}
        self.key = self.service.secret.current

    def close(self):
        self.service.shutdown()
        self.thread.join(5)
        self.temp.cleanup()

    def raw(self, route, body, tag=None, headers=None, method="POST"):
        """The status and body, or `(None, b"")` when the service dropped the connection.

        Refusing a request before reading its body is correct, and a host may then reset
        the connection rather than deliver the response. Callers assert what the refusal
        must be, and only the oversized case accepts either outcome.
        """
        connection = http.client.HTTPConnection("127.0.0.1", self.service.port, timeout=10)
        sent = {"Content-Type": "application/json", "X-Orch-Broker-Auth": tag if tag is not None else sign(self.key, "request", route, body)}
        sent.update(headers or {})
        try:
            connection.request(method, "/" + route, body, sent)
            response = connection.getresponse()
            return response.status, response.read()
        except (OSError, http.client.HTTPException):
            return None, b""
        finally:
            connection.close()


class BrokerServiceTests(unittest.TestCase):
    create = helpers.WorkflowTests.create
    approve = helpers.WorkflowTests.approve
    to_action = helpers.WorkflowTests.to_action

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store_root = Path(self.temp.name) / "store"
        self.harness = self.engine = None

    def tearDown(self):
        if self.engine:
            self.engine.close()
        if self.harness:
            self.harness.close()

    def start(self, mode="trusted-fixture", image=None):
        self.harness = Harness(self.store_root.resolve() / "workspaces", mode, image)
        self.engine = Engine(self.store_root, Executor(mode, image), broker_service=self.harness.config)
        return self.engine

    def request(self, operation=None, **overrides):
        operation = operation or uid()
        request = {"schema_version": VERSION, "operation_id": operation, "task_id": uid(), "generation": 1, "nonce": uid(),
                   "workspace": str(self.engine.store.root / "workspaces" / operation), "recipe": "test", "args": [],
                   "mode": "trusted-fixture", "image": None, "source_digest": brokermod.source_digest()}
        request.update(overrides)
        return request

    def message(self, kind, request):
        return {"schema_version": VERSION, "kind": kind, "request": request}

    def to_implementation(self, task):
        self.engine.run(task)
        self.approve(task)
        self.engine.advance(task)

    def test_workflow_completes_through_service_and_records_identity(self):
        engine = self.start()
        service = self.harness.service
        with patch.object(service.executor, "run_direct", wraps=service.executor.run_direct) as executed, \
                patch.object(engine.executor, "run_direct", side_effect=AssertionError("Orchestrator must not execute recipes")):
            task = self.create()
            self.to_action(task)
            self.approve(task)
            engine.run(task)
        self.assertEqual(engine.task(task)["state"]["state"], "COMPLETED")
        self.assertEqual(executed.call_count, 2)
        rows = engine.store.db.execute("SELECT * FROM broker_identities ORDER BY rowid").fetchall()
        self.assertEqual(len(rows), 2)
        for row in rows:
            record = json.loads(row["record"])
            self.assertEqual(record["transport"], "service")
            self.assertFalse(record["separate_identity"])
            self.assertEqual(record["broker"], service.identity)
            self.assertEqual(record["orchestrator"], identity())
            self.assertEqual(digest(record), row["sha256"])
            self.assertTrue(engine.store.db.execute("SELECT 1 FROM execution_provenance WHERE operation_id=? AND generation=?",
                                                    (row["operation_id"], row["generation"])).fetchone())
            self.assertTrue((self.harness.journal / (row["operation_id"] + ".json")).is_file())
        self.assertIsNone(engine.executor.broker.root)
        self.assertFalse((engine.store.root / "broker").exists())
        audit(engine.store)

    def test_unauthenticated_malformed_and_foreign_requests_never_execute(self):
        self.start()
        h = self.harness
        request = self.request()
        body = canonical(self.message("execute", request))
        port = str(h.service.port)
        with patch.object(h.service.executor, "run_direct", side_effect=AssertionError("No execution")):
            self.assertEqual(h.raw("execute", body, tag="")[0], 401)
            self.assertEqual(h.raw("execute", body, tag="f" * 64)[0], 401)
            self.assertEqual(h.raw("execute", body + b" ", tag=sign(h.key, "request", "execute", body))[0], 401)
            self.assertEqual(h.raw("execute", body, tag=sign(h.key, "request", "result", body))[0], 401)
            self.assertEqual(h.raw("execute", body, tag=sign(h.key, "reply", "execute", body))[0], 401)
            self.assertEqual(h.raw("execute", body, headers={"Host": "localhost:" + port})[0], 400)
            self.assertEqual(h.raw("execute", body, headers={"Origin": "http://127.0.0.1:" + port})[0], 400)
            self.assertEqual(h.raw("execute", body, headers={"Content-Type": "text/plain"})[0], 400)
            # The service refuses an oversized body without reading it, so the
            # connection may be reset before the 413 arrives. Either way it never runs.
            self.assertIn(h.raw("execute", b"x" * 16385)[0], (413, None))
            self.assertEqual(h.raw("execute", b"")[0], 413)
            self.assertEqual(h.raw("execute", b"not json")[0], 400)
            self.assertEqual(h.raw("execute", b"[]")[0], 400)
            self.assertEqual(h.raw("execute", canonical(self.message("execute", {**request, "extra": 1})))[0], 400)
            self.assertEqual(h.raw("execute", canonical(self.message("result", request)))[0], 409)
            self.assertEqual(h.raw("identity", body)[0], 400)
            self.assertEqual(h.raw("shell", body)[0], 404)
            self.assertEqual(h.raw("identity", b"", method="GET")[0], 405)
            unauthenticated = h.raw("identity", canonical({"schema_version": VERSION, "kind": "identity", "nonce": uid()}), tag="0" * 64)
            self.assertEqual(unauthenticated[0], 401)
            self.assertNotIn(b"account_sha256", unauthenticated[1])
        self.assertEqual(list(h.journal.iterdir()), [])

    def test_profile_workspace_and_recipe_are_broker_policy(self):
        self.start()
        h = self.harness
        client = ServiceClient(h.config)
        elsewhere = Path(self.temp.name) / "elsewhere"
        elsewhere.mkdir()
        cases = ({"mode": "docker", "image": IMAGE}, {"workspace": str(elsewhere / uid())},
                 {"workspace": str(self.engine.store.root / "workspaces" / uid())}, {"source_digest": "b" * 64},
                 {"recipe": "edit", "args": []}, {"recipe": "test", "args": ["hello world\n"]}, {"generation": 0})
        with patch.object(h.service.executor, "run_direct", side_effect=AssertionError("No execution")):
            for overrides in cases:
                with self.subTest(overrides=overrides):
                    with self.assertRaises(Rejected):
                        client.call("execute", self.message("execute", self.request(**overrides)))
        self.assertEqual(list(h.journal.iterdir()), [])
        # A Docker-profile service refuses to downgrade to local fixture execution.
        h.close()
        self.harness = Harness(self.store_root.resolve() / "workspaces", "docker", IMAGE)
        with patch.object(self.harness.service.executor, "run_direct", side_effect=AssertionError("No execution")):
            with self.assertRaises(Rejected):
                ServiceClient(self.harness.config).call("execute", self.message("execute", self.request()))
            with self.assertRaises(Rejected):
                ServiceClient(self.harness.config).call("execute", self.message("execute", self.request(mode="docker", image="python@sha256:" + "b" * 64)))
        self.assertEqual(list(self.harness.journal.iterdir()), [])

    def test_identical_redelivery_returns_retained_envelope_without_reexecution(self):
        engine = self.start()
        task = self.create()
        self.to_action(task)
        rows = engine.store.db.execute("SELECT operation_id, request FROM broker_requests").fetchall()
        self.assertEqual(len(rows), 2)
        client = ServiceClient(self.harness.config)
        with patch.object(self.harness.service.executor, "run_direct", side_effect=AssertionError("No re-execution")):
            for row in rows:
                request = json.loads(row["request"])
                envelope = json.loads((self.harness.journal / (row["operation_id"] + ".json")).read_text())
                self.assertEqual(client.call("execute", self.message("execute", request))["envelope"], envelope)
                self.assertEqual(client.call("result", self.message("result", request))["envelope"], envelope)
                with self.assertRaises(Rejected):
                    client.call("execute", self.message("execute", {**request, "nonce": uid()}))
                with self.assertRaises(Rejected):
                    client.call("result", self.message("result", {**request, "nonce": uid()}))
                self.assertEqual(json.loads((self.harness.journal / (row["operation_id"] + ".json")).read_text()), envelope)

    def test_result_route_never_executes_and_reports_missing_results(self):
        self.start()
        request = self.request()
        with patch.object(self.harness.service.executor, "run_direct", side_effect=AssertionError("No execution")):
            with self.assertRaises(Rejected):
                ServiceClient(self.harness.config).call("result", self.message("result", request))
            self.assertEqual(self.harness.raw("result", canonical(self.message("result", request)))[0], 404)
        self.assertFalse((self.harness.journal / (request["operation_id"] + ".json")).exists())

    def test_crash_after_execution_before_journal_blocks_without_relaunch(self):
        engine = self.start()
        service = self.harness.service
        task = self.create()
        self.to_implementation(task)
        with patch.object(service.executor, "run_direct", wraps=service.executor.run_direct) as executed:
            with patch("orch.broker_process.atomic_json", side_effect=RuntimeError("crash before journal")):
                engine.advance(task)
            state = engine.task(task)["state"]
            self.assertEqual(state["state"], "BLOCKED")
            self.assertEqual(state["error"]["code"], "broker_uncertain")
            operation = state["active_operation_id"]
            self.assertEqual(executed.call_count, 1)
            self.assertEqual((engine.store.root / "workspaces" / operation / "greeting.txt").read_bytes(), b"hello world\n")
            self.assertFalse((self.harness.journal / (operation + ".json")).exists())
            self.assertEqual(engine.store.db.execute("SELECT count(*) FROM broker_identities").fetchone()[0], 0)
            # Local fixture execution cannot be proven stopped, exactly as with the subprocess broker.
            with self.assertRaises(Rejected):
                engine.recover(task, state["revision"])
            self.assertEqual(engine.task(task)["state"]["state"], "BLOCKED")
            self.assertEqual(executed.call_count, 1)

    def test_docker_profile_recovery_retires_through_the_broker(self):
        engine = self.start("docker", IMAGE)
        service = self.harness.service

        def simulated(workspace, script, args, operation_id):
            path = Path(workspace) / "greeting.txt"
            if script == EDIT_CODE:
                path.write_text(args[0], newline="")
            code = 0 if script == EDIT_CODE or path.read_text() == "hello world\n" else 1
            return {"command": ["docker", "run", "simulated"], "started": now(), "ended": now(), "code": code,
                    "stdout": "", "stderr": "", "truncated": False, "failure": None}

        with patch.object(service.executor, "run_direct", side_effect=simulated), \
                patch.object(service.executor, "reconcile", return_value=False) as reconcile, \
                patch.object(engine.executor, "reconcile", side_effect=AssertionError("Orchestrator must not touch Docker")), \
                patch.object(engine.executor, "run_direct", side_effect=AssertionError("Orchestrator must not execute recipes")):
            task = self.create()
            self.to_implementation(task)
            with patch("orch.broker_process.atomic_json", side_effect=RuntimeError("crash before journal")):
                engine.advance(task)
            state = engine.task(task)["state"]
            self.assertEqual(state["state"], "BLOCKED")
            operation = state["active_operation_id"]
            with self.assertRaises(Rejected):
                engine.recover(task, state["revision"])
            self.assertTrue((self.harness.journal / (operation + ".retired")).is_file())
            self.assertEqual(engine.task(task)["state"]["state"], "BLOCKED")
            reconcile.return_value = True
            result = engine.recover(task, state["revision"])
            self.assertEqual(result["state"]["state"], "CHANGES_REQUESTED")
            self.assertEqual(json.loads(engine.store.db.execute("SELECT result FROM operations WHERE id=?", (operation,)).fetchone()[0])["execution"],
                             {"failure": "stopped_without_receipt"})
            # A retired generation can never be executed late, even with the exact durable request.
            request = json.loads(engine.store.db.execute("SELECT request FROM broker_requests WHERE operation_id=?", (operation,)).fetchone()[0])
            with self.assertRaises(Rejected):
                ServiceClient(self.harness.config).call("execute", self.message("execute", request))
            self.assertFalse((self.harness.journal / (operation + ".json")).exists())
            engine.run(task)
            self.assertEqual(engine.task(task)["state"]["state"], "AWAITING_ACTION_APPROVAL")
        audit(engine.store)

    def test_tampered_reply_is_uncertain_and_retained_journal_recovers_without_relaunch(self):
        engine = self.start()
        service = self.harness.service
        task = self.create()
        self.to_implementation(task)
        original = http.client.HTTPResponse.read

        def tampered(response, amt=None):
            return original(response, amt).replace(b'"code":0', b'"code":1')

        with patch.object(service.executor, "run_direct", wraps=service.executor.run_direct) as executed:
            with patch.object(http.client.HTTPResponse, "read", tampered):
                engine.advance(task)
            state = engine.task(task)["state"]
            self.assertEqual(state["state"], "BLOCKED")
            operation = state["active_operation_id"]
            self.assertEqual(executed.call_count, 1)
            self.assertTrue((self.harness.journal / (operation + ".json")).is_file())
            self.assertEqual(engine.store.db.execute("SELECT count(*) FROM execution_provenance").fetchone()[0], 0)
            self.assertEqual(engine.store.db.execute("SELECT count(*) FROM broker_identities").fetchone()[0], 0)
            result = engine.recover(task, state["revision"])
            self.assertEqual(result["state"]["state"], "CHANGES_REQUESTED")
            self.assertEqual(executed.call_count, 1)
            self.assertEqual(json.loads(engine.store.db.execute("SELECT result FROM operations WHERE id=?", (operation,)).fetchone()[0])["execution"]["code"], 0)
            self.assertEqual(engine.store.db.execute("SELECT count(*) FROM broker_identities WHERE operation_id=?", (operation,)).fetchone()[0], 1)
        audit(engine.store)

    def test_secret_endpoint_and_directory_guards(self):
        for value in ("localhost:80", "0.0.0.0:80", "127.0.0.1:0", "127.0.0.1:65536", "127.0.0.1", "http://127.0.0.1:80", "127.0.0.1:80 ", 8080, None):
            with self.subTest(value=value), self.assertRaises(Rejected):
                endpoint(value)
        self.assertEqual(endpoint("127.0.0.1:8081"), ("127.0.0.1", 8081))
        root = Path(self.temp.name)
        for content in ("short", "g" * 64, "A" * 64, secrets.token_hex(32) + "\n" + "x" * 70):
            (root / "bad").write_text(content, encoding="ascii")
            with self.subTest(content=content[:8]), self.assertRaises(Rejected):
                load_secret(root / "bad")
        good = write_secret(root / "good")
        self.assertEqual(len(load_secret(good)), 32)
        with self.assertRaises(Rejected):
            load_secret(root / "missing")
        try:
            os.symlink(good, root / "link")
        except (OSError, NotImplementedError):
            pass
        else:
            with self.assertRaises(Rejected):
                load_secret(root / "link")
        if os.name != "nt":
            os.chmod(good, 0o604)
            with self.assertRaises(Rejected):
                load_secret(good)
            os.chmod(good, 0o600)
        with self.assertRaises(Rejected):
            ServiceClient({"endpoint": "127.0.0.1:1"})
        with self.assertRaises(Rejected):
            ServiceClient({"endpoint": "127.0.0.1:1", "secret": str(root / "missing")})
        with self.assertRaises(Rejected):
            BrokerService(root / "j", good, root / "j" / "w", "trusted-fixture")
        with self.assertRaises(Rejected):
            BrokerService(root / "j" / "inner", good, root / "j", "trusted-fixture")
        with self.assertRaises(Rejected):
            BrokerService(root / "j", good, root / "w", "docker", None)
        with self.assertRaises(Rejected):
            BrokerService(root / "j", good, root / "w", "shell")
        with self.assertRaises(Rejected):
            BrokerService(root / "j", good, root / "w", "trusted-fixture", None, 70000)
        self.assertFalse((root / "j").exists())

    def test_identity_assessment_and_deployment_gate_never_authorize(self):
        self.start()
        config = self.harness.config
        report = assess_identity(config)
        self.assertEqual(report["status"], "shared")
        self.assertFalse(report["separate_identity"])
        self.assertTrue(report["source_digest_match"])
        self.assertFalse(report["live_authorized"])
        self.assertEqual(report["broker"], identity())
        # This harness configures no dispatch credential, so the broker says it cannot
        # reach outside; the assertion names that rather than omitting it.
        self.assertEqual(report["profile"], {"mode": "trusted-fixture", "image": None, "dispatch": False})
        base = identity()
        other = {**base, "account_sha256": "f" * 64, "uid": None if base["uid"] is None else base["uid"] + 1}
        with patch.object(self.harness.service, "identity", other):
            self.assertEqual(assess_identity(config)["status"], "separate")
        with patch("orch.broker_service.source_digest", return_value="0" * 64):
            self.assertEqual(assess_identity(config)["status"], "source_mismatch")
        wrong = write_secret(Path(self.temp.name) / "wrong")
        with self.assertRaises(Rejected):
            assess_identity({"endpoint": config["endpoint"], "secret": str(wrong)})
        closed = {"endpoint": "127.0.0.1:" + str(free_port()), "secret": config["secret"]}
        with self.assertRaises(Rejected):
            assess_identity(closed)
        self.assertFalse(separate(base, base))
        self.assertTrue(separate(other, base))
        self.assertFalse(separate({**other, "system": "Other"}, base))
        if base["uid"] is not None:
            self.assertFalse(separate({**base, "account_sha256": "f" * 64}, base))
        with self.assertRaises(Rejected):
            separate({**base, "uid": -1}, base)
        assessment = deployment_check("github_copilot_cli", broker=config)
        self.assertEqual(assessment["broker_identity"]["status"], "shared")
        self.assertIn("broker_identity", assessment["blocking_gates"])
        # Named rather than counted, so adding a gate does not need this test edited.
        self.assertEqual(assessment["blocking_gates"], [gate["id"] for gate in assessment["gates"]])
        self.assertFalse(assessment["provider_authorized"])
        with patch("orch.deployment.assess_identity", return_value={"status": "separate"}):
            assessment = deployment_check("github_copilot_cli", broker=config)
        self.assertNotIn("broker_identity", assessment["blocking_gates"])
        self.assertEqual(assessment["status"], "blocked")
        self.assertFalse(assessment["provider_authorized"])
        self.assertEqual(deployment_check("github_copilot_cli", broker=closed)["broker_identity"]["status"], "rejected")
        self.assertEqual(deployment_check("github_copilot_cli")["broker_identity"]["status"], "not_supplied")

    def test_restart_preserves_identity_evidence_and_audit_detects_tampering(self):
        engine = self.start()
        task = self.create()
        self.to_action(task)
        rows = [dict(row) for row in engine.store.db.execute("SELECT * FROM broker_identities ORDER BY operation_id")]
        self.assertEqual(len(rows), 2)
        engine.close()
        self.engine = None
        store = Store(self.store_root, read_only=True)
        try:
            self.assertEqual([dict(row) for row in store.db.execute("SELECT * FROM broker_identities ORDER BY operation_id")], rows)
            audit(store)
        finally:
            store.close()
        store = Store(self.store_root)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                store.db.execute("DELETE FROM broker_identities")
            with self.assertRaises(sqlite3.DatabaseError):
                store.db.execute("UPDATE broker_identities SET sha256='0'")
            store.db.execute("DROP TRIGGER broker_identities_update")
            first = rows[0]
            forged = json.loads(first["record"])
            forged["separate_identity"] = True
            store.db.execute("UPDATE broker_identities SET record=?, sha256=? WHERE operation_id=?",
                             (canonical(forged).decode(), digest(forged), first["operation_id"]))
            self.assertEqual(integrity_report(store)["status"], "failed")
            store.db.execute("UPDATE broker_identities SET record=?, sha256=? WHERE operation_id=?",
                             (first["record"], "0" * 64, first["operation_id"]))
            self.assertEqual(integrity_report(store)["status"], "failed")
            store.db.execute("UPDATE broker_identities SET record=?, sha256=? WHERE operation_id=?",
                             (first["record"], first["sha256"], first["operation_id"]))
            self.assertEqual(integrity_report(store)["status"], "passed")
        finally:
            store.close()

    def test_subprocess_transport_records_shared_identity(self):
        self.engine = Engine(self.store_root, Executor("trusted-fixture"))
        task = self.create()
        self.to_action(task)
        rows = self.engine.store.db.execute("SELECT record FROM broker_identities").fetchall()
        self.assertEqual(len(rows), 2)
        for row in rows:
            record = json.loads(row[0])
            self.assertEqual(record["transport"], "subprocess")
            self.assertFalse(record["separate_identity"])
            self.assertEqual(record["broker"], record["orchestrator"])
        self.assertTrue((self.engine.store.root / "broker").is_dir())
        audit(self.engine.store)

    def test_cli_broker_serve_and_check(self):
        root = Path(self.temp.name)
        secret = write_secret(root / "cli-secret")
        command = [sys.executable, "-m", "orch", "broker-serve", "--broker-root", str(root / "cli-journal"), "--broker-secret", str(secret),
                   "--workspaces", str(root / "cli-workspaces"), "--trusted-fixture", "--port", "0"]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=flags)
        try:
            line = process.stdout.readline().strip()
            self.assertTrue(line.startswith("Broker service: http://127.0.0.1:"), line)
            address = line.split("http://", 1)[1]

            def check(*extra):
                return subprocess.run([sys.executable, "-m", "orch", "broker-check", *extra], cwd=ROOT, capture_output=True, text=True, timeout=60)

            result = check("--broker-endpoint", address, "--broker-secret", str(secret))
            self.assertEqual(result.returncode, 2)
            report = json.loads(result.stdout)
            self.assertEqual(report["kind"], "BrokerIdentityAssessment")
            self.assertEqual(report["status"], "shared")
            self.assertFalse(report["live_authorized"])
            wrong = write_secret(root / "cli-wrong")
            self.assertEqual(check("--broker-endpoint", address, "--broker-secret", str(wrong)).returncode, 2)
            self.assertEqual(check("--broker-endpoint", address).returncode, 2)
            self.assertEqual(check("--broker-endpoint", address, "--broker-secret", str(wrong)).stdout, "")
        finally:
            process.terminate()
            process.communicate(timeout=10)
        missing = subprocess.run([sys.executable, "-m", "orch", "broker-serve", "--broker-root", str(root / "x"), "--broker-secret", str(secret),
                                  "--workspaces", str(root / "y")], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(missing.returncode, 2)


if __name__ == "__main__":
    unittest.main()
