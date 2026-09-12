import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import test_workflow as helpers
from orch.broker import BrokerUncertain, atomic_json, file_lock
from orch.contracts import Rejected, uid
from orch.engine import Engine
from orch.execution import Executor
from orch.maintenance import audit, backup, collect_orphans, restore
from orch.process import capture
from orch.storage import Store


class RuntimeTests(unittest.TestCase):
    setUp = helpers.WorkflowTests.setUp
    tearDown = helpers.WorkflowTests.tearDown
    create = helpers.WorkflowTests.create
    approve = helpers.WorkflowTests.approve
    to_action = helpers.WorkflowTests.to_action
    count = helpers.WorkflowTests.count

    def test_broker_subprocess_provenance_and_cleanup(self):
        task = self.create()
        self.to_action(task)
        rows = self.engine.store.db.execute("SELECT * FROM execution_provenance").fetchall()
        self.assertEqual(len(rows), 2)
        for row in rows:
            envelope = json.loads(self.engine.store.read_artifact(json.loads(row["artifact"]), task))
            self.assertEqual(envelope["task_id"], task)
            self.assertEqual(envelope["generation"], 1)
            self.assertEqual(envelope["result"]["code"], 0)
            self.assertFalse((self.engine.store.root / "workspaces" / row["operation_id"]).exists())

    def interrupted_broker(self):
        task = self.create()
        self.engine.run(task)
        self.approve(task)
        self.engine.advance(task)
        original = self.engine.executor.broker.result
        with patch.object(self.engine.executor.broker, "result", side_effect=Rejected("lost acknowledgement")):
            self.engine.advance(task)
        state = self.engine.task(task)["state"]
        self.assertEqual(state["state"], "BLOCKED")
        return task, state

    def test_operator_recovery_fences_late_results(self):
        task, state = self.interrupted_broker()
        operation = state["active_operation_id"]
        result = self.engine.recover(task, state["revision"])
        self.assertEqual(result["state"]["state"], "CHANGES_REQUESTED")
        with self.assertRaises(Rejected):
            self.engine.store.complete_operation(operation, 1, {})
        with self.assertRaises(Rejected):
            self.engine.executor.broker.result(operation, 1)
        self.assertEqual(self.engine.recover(task, state["revision"]), result)
        self.engine.run(task)
        self.assertEqual(self.engine.task(task)["state"]["state"], "AWAITING_ACTION_APPROVAL")

    def test_tampered_envelope_and_active_broker_rejected(self):
        task, state = self.interrupted_broker()
        operation = state["active_operation_id"]
        root = self.engine.executor.broker.root
        with file_lock(root / (operation + ".lock")), self.assertRaises(Rejected):
            self.engine.recover(task, state["revision"])
        path = root / (operation + ".json")
        envelope = json.loads(path.read_text())
        envelope["nonce"] = uid()
        path.write_text(json.dumps(envelope))
        with self.assertRaises(Rejected):
            self.engine.recover(task, state["revision"])
        self.assertEqual(self.engine.task(task)["state"]["state"], "BLOCKED")

    def test_no_receipt_local_process_not_assumed_stopped(self):
        task = self.create()
        self.engine.run(task)
        self.approve(task)
        self.engine.advance(task)
        with patch.object(self.engine.executor.broker, "run", side_effect=BrokerUncertain("no journal")):
            self.engine.advance(task)
        state = self.engine.task(task)["state"]
        with self.assertRaises(Rejected):
            self.engine.recover(task, state["revision"])

    def test_uncertain_cleanup_blocks_automatic_retry(self):
        task = self.create()
        self.engine.run(task)
        self.approve(task)
        self.engine.advance(task)
        with patch.object(self.engine.executor.broker, "result", return_value={"failure": "cleanup_uncertain"}):
            self.engine.advance(task)
        self.assertEqual(self.engine.task(task)["state"]["state"], "BLOCKED")
        self.assertEqual(self.engine.task(task)["context"]["attempt"], 1)

    def test_restore_drill_continues_approval_paused_task(self):
        task = self.create(reviews=("NEEDS_CHANGES", "ACCEPT"))
        self.to_action(task)
        expected = self.engine.task(task)
        with tempfile.TemporaryDirectory() as base:
            snapshot, restored = Path(base) / "backup", Path(base) / "restored"
            backup(self.engine.store, snapshot)
            restore(snapshot, restored)
            other = Engine(restored, Executor("trusted-fixture"))
            try:
                self.assertEqual(other.task(task), expected)
                audit(other.store)
                state = other.task(task)["state"]
                other.approve(task, state["pending_approval"]["id"], "approve", state["revision"])
                other.run(task)
                self.assertEqual(other.task(task)["state"]["state"], "COMPLETED")
            finally:
                other.close()

    def test_restore_rejects_corruption_and_existing_destination(self):
        task = self.create()
        with tempfile.TemporaryDirectory() as base:
            snapshot = Path(base) / "backup"
            backup(self.engine.store, snapshot)
            with self.assertRaises(Rejected):
                restore(snapshot, snapshot)
            artifact = next((snapshot / "artifacts").iterdir())
            artifact.write_bytes(b"corrupt")
            with self.assertRaises(Rejected):
                restore(snapshot, Path(base) / "restored")
            self.assertFalse((Path(base) / "restored").exists())

    def test_quotas_and_orphan_collection_preserve_evidence(self):
        task = self.create()
        self.engine.store.task_quota = 1
        with self.assertRaises(Rejected):
            self.engine.store.artifact(task, "too much")
        root = self.engine.store.root / "artifacts"
        orphan = root / ("f" * 64)
        orphan.write_bytes(b"orphan")
        os.utime(orphan, (time.time() - 90000, time.time() - 90000))
        collected = collect_orphans(self.engine.store)
        self.assertIn(orphan.name, collected["removed"])
        audit(self.engine.store)

    def test_unknown_database_version_rejected(self):
        with tempfile.TemporaryDirectory() as base:
            db = sqlite3.connect(Path(base) / "orch.sqlite")
            db.execute("PRAGMA user_version=999")
            db.close()
            with self.assertRaises(Rejected):
                Store(base)

    def test_v1_migration_preserves_operation(self):
        with tempfile.TemporaryDirectory() as base:
            db = sqlite3.connect(Path(base) / "orch.sqlite")
            db.executescript("CREATE TABLE tasks(id TEXT PRIMARY KEY,state TEXT NOT NULL,context TEXT NOT NULL); CREATE TABLE operations(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,stage TEXT NOT NULL,slot TEXT NOT NULL,status TEXT NOT NULL,result TEXT,UNIQUE(task_id,stage,slot)); PRAGMA user_version=1;")
            db.execute("INSERT INTO tasks VALUES('legacy','{}','{}')")
            db.execute("INSERT INTO operations VALUES('operation','legacy','planning','0','done','{}')")
            db.commit()
            db.close()
            store = Store(base)
            try:
                self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 2)
                row = store.db.execute("SELECT generation,status FROM operations WHERE id='operation'").fetchone()
                self.assertEqual(tuple(row), (1, "done"))
            finally:
                store.close()

    def test_capture_bounds_output_and_deadline(self):
        result = capture([sys.executable, "-I", "-c", "print('x'*200000)"], limit=1024)
        self.assertEqual(result["failure"], "output_limit")
        self.assertLessEqual(len(result["stdout"]), 1024)
        result = capture([sys.executable, "-I", "-c", "import time;time.sleep(30)"], timeout=0.1)
        self.assertEqual(result["failure"], "timeout")

    def test_cleanup_refuses_unexpected_content(self):
        operation = uid()
        directory = self.engine.worker.workspace(operation)
        (directory / "unexpected.txt").write_text("retain")
        with self.assertRaises(Rejected):
            self.engine.worker.cleanup(operation)
        self.assertTrue((directory / "unexpected.txt").exists())

    def test_docker_reconciliation_fails_closed(self):
        executor = Executor("docker", "python@sha256:" + "a" * 64)
        failure = {"code": 1, "stdout": "", "stderr": "daemon down", "failure": None}
        with patch("orch.execution.capture", return_value=failure):
            self.assertFalse(executor.reconcile(uid()))
        absent = {"code": 0, "stdout": "", "stderr": "", "failure": None}
        with patch("orch.execution.capture", return_value=absent):
            self.assertTrue(executor.reconcile(uid()))

    def test_docker_auto_removal_races_require_proven_absence(self):
        executor = Executor("docker", "python@sha256:" + "a" * 64)
        operation = uid()
        absent = {"code": 0, "stdout": "", "stderr": "", "failure": None}
        present = {**absent, "stdout": "container-id\n"}
        failure = {**absent, "code": 1}
        inspected = {**absent, "stdout": json.dumps([{"Id": "container-id", "Config": {"Labels": {"ai.orchd.operation": operation}}}])}
        for prefix in ([present, failure], [present, inspected, failure]):
            for final, expected in ((absent, True), (present, False), (failure, False)):
                with patch("orch.execution.capture", side_effect=[*prefix, final]):
                    self.assertEqual(executor.reconcile(operation), expected)
        wrong_label = {**absent, "stdout": json.dumps([{"Id": "unrelated", "Config": {"Labels": {"ai.orchd.operation": "other"}}}])}
        with patch("orch.execution.capture", side_effect=[present, wrong_label]) as command:
            self.assertFalse(executor.reconcile(operation))
            self.assertEqual(command.call_count, 2)  # Never remove an unrelated container.

    def test_output_truncation_survives_uncertain_cleanup(self):
        executor = Executor("docker", "python@sha256:" + "a" * 64)
        flooded = {"code": -1, "stdout": "bounded", "stderr": "", "failure": "output_limit", "truncated": True}
        from orch.fixtures import TEST_CODE
        with patch("orch.execution.capture", return_value=flooded), patch.object(executor, "reconcile", return_value=False):
            result = executor.run_direct(self.temp.name, TEST_CODE, [], uid())
        self.assertEqual(result["failure"], "cleanup_uncertain")
        self.assertTrue(result["truncated"])
        self.assertIsNone(result["code"])


if __name__ == "__main__":
    unittest.main()
