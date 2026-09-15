"""Run the fixture workflow through a real broker service process and exercise its crash boundary."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orch.broker import BrokerClient, ServiceClient, VERSION, rotate_secret
from orch.contracts import uid
from orch.engine import Engine
from orch.execution import Executor
from orch.maintenance import audit, integrity_report
from orch.storage import Store


def start_service(journal, secret, workspaces):
    command = [sys.executable, "-m", "orch", "broker-serve", "--broker-root", str(journal), "--broker-secret", str(secret),
               "--workspaces", str(workspaces), "--trusted-fixture", "--port", "0"]
    process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    line = process.stdout.readline().strip()
    if not line.startswith("Broker service: http://127.0.0.1:"):
        stop(process)
        raise RuntimeError("Broker service did not start")
    return process, line.split("http://", 1)[1]


def stop(process):
    process.terminate()
    process.communicate(timeout=10)


def approve(engine, task):
    state = engine.task(task)["state"]
    engine.approve(task, state["pending_approval"]["id"], "approve", state["revision"])


def main():
    with tempfile.TemporaryDirectory(prefix="orchd-broker-") as directory:
        root = Path(directory)
        store, journal, secret = root / "store", root / "broker-journal", root / "broker-secret"
        secret.write_text(secrets.token_hex(32), encoding="ascii")
        os.chmod(secret, 0o600)
        workspaces = store.resolve() / "workspaces"
        process, address = start_service(journal, secret, workspaces)
        engine = None
        try:
            check = subprocess.run([sys.executable, "-m", "orch", "broker-check", "--broker-endpoint", address, "--broker-secret", str(secret)],
                                   cwd=ROOT, capture_output=True, text=True, timeout=60)
            report = json.loads(check.stdout)
            assert report["kind"] == "BrokerIdentityAssessment" and report["live_authorized"] is False
            assert report["source_digest_match"] and report["status"] in ("separate", "shared"), report["status"]
            assert check.returncode == (0 if report["status"] == "separate" else 2)
            config = {"endpoint": address, "secret": str(secret)}
            engine = Engine(store, Executor("trusted-fixture"), broker_service=config)
            task = engine.create_task("Broker service acceptance")
            engine.run(task)
            approve(engine, task)
            engine.run(task)
            assert engine.task(task)["state"]["state"] == "AWAITING_ACTION_APPROVAL"
            approve(engine, task)
            engine.run(task)
            assert engine.task(task)["state"]["state"] == "COMPLETED"
            rows = engine.store.db.execute("SELECT * FROM broker_identities").fetchall()
            records = [json.loads(row["record"]) for row in rows]
            assert len(records) == 2 and all(r["transport"] == "service" for r in records)
            assert all(r["separate_identity"] == report["separate_identity"] for r in records)
            assert all((journal / (row["operation_id"] + ".json")).is_file() for row in rows)
            assert not (engine.store.root / "broker").exists()
            # Rotation without restarting either process: the overlap keeps the running
            # engine working, and completion leaves only the new secret accepted.
            def ask():
                return ServiceClient(config).call("identity", {"schema_version": VERSION, "kind": "identity", "nonce": uid()})["secret"]
            assert ask() == {"accepted": 1, "rotating": False, "age_seconds": ask()["age_seconds"], "stale": False}
            begun = rotate_secret(secret)
            assert begun["stage"] == "overlap" and begun["accepted"] == 2 and begun["rotating"]
            assert ask()["accepted"] == 2
            finished = rotate_secret(secret, complete=True)
            assert finished["stage"] == "completed" and finished["accepted"] == 1 and not finished["rotating"]
            assert ask() == {"accepted": 1, "rotating": False, "age_seconds": ask()["age_seconds"], "stale": False}
            # Crash after the broker's durable result, before the orchestrator accepted it.
            second = engine.create_task("Broker service crash boundary")
            engine.run(second)
            approve(engine, second)
            engine.advance(second)
            original, calls = BrokerClient._accept, []

            def crash_once(client, *args, **kwargs):
                if not calls:
                    calls.append(1)
                    raise RuntimeError("Injected orchestrator crash after the broker journal commit")
                return original(client, *args, **kwargs)

            with patch.object(BrokerClient, "_accept", crash_once):
                try:
                    engine.advance(second)
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("Crash injection did not run")
            state = engine.task(second)["state"]
            assert state["state"] == "IMPLEMENTING" and state["active_operation_id"], state
            operation = state["active_operation_id"]
            engine.advance(second)  # A restarted dispatcher finds the unresolved operation and blocks.
            assert engine.task(second)["state"]["state"] == "BLOCKED"
            request = json.loads(engine.store.db.execute("SELECT request FROM broker_requests WHERE operation_id=?", (operation,)).fetchone()[0])
            # Restart both processes: the broker journal is durable and is served without re-execution.
            engine.close()
            engine = None
            stop(process)
            process, address = start_service(journal, secret, workspaces)
            config = {"endpoint": address, "secret": str(secret)}
            served = ServiceClient(config).call("result", {"schema_version": VERSION, "kind": "result", "request": request})
            assert served["envelope"]["result"]["code"] == 0
            engine = Engine(store, Executor("trusted-fixture"), broker_service=config)
            state = engine.task(second)["state"]
            recovered = engine.recover(second, state["revision"])
            assert recovered["state"]["state"] == "CHANGES_REQUESTED"
            assert engine.store.db.execute("SELECT count(*) FROM execution_provenance WHERE operation_id=?", (operation,)).fetchone()[0] == 1
            assert engine.store.db.execute("SELECT count(*) FROM broker_identities WHERE operation_id=?", (operation,)).fetchone()[0] == 1
            assert sorted(p.name for p in journal.glob(operation + ".*")) == [operation + ".json", operation + ".lock"]
            engine.run(second)
            assert engine.task(second)["state"]["state"] == "AWAITING_ACTION_APPROVAL"
            assert integrity_report(engine.store)["status"] == "passed"
            engine.close()
            engine = None
            copy = Store(store, read_only=True)
            try:
                audit(copy)
            finally:
                copy.close()
        finally:
            if engine is not None:
                engine.close()
            stop(process)
    print("PASS: broker service identity check, authenticated fixed-route execution, broker-owned journal, "
          "secret rotation without restart, crash-boundary recovery without relaunch and broker/orchestrator "
          "restart (" + report["status"] + " identity)")


if __name__ == "__main__":
    main()
