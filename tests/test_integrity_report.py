import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import test_workflow as helpers
from orch.api import Application
from orch.contracts import Rejected
from orch.maintenance import integrity_report
from orch.storage import Store


class IntegrityReportTests(unittest.TestCase):
    setUp = helpers.WorkflowTests.setUp
    tearDown = helpers.WorkflowTests.tearDown
    create = helpers.WorkflowTests.create

    def test_valid_report_leaves_workflow_and_artifacts_unchanged(self):
        task = self.create()
        self.engine.run(task)
        state, events = self.engine.task(task), self.engine.events(task)
        files = {p.name: p.read_bytes() for p in (self.engine.store.root / "artifacts").iterdir()}
        readonly = Store(self.engine.store.root, read_only=True)
        try:
            report = integrity_report(readonly)
        finally:
            readonly.close()
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["tasks"], 1)
        self.assertGreater(report["records"], 0)
        self.assertGreater(report["artifact_references"], 0)
        self.assertEqual(self.engine.task(task), state)
        self.assertEqual(self.engine.events(task), events)
        self.assertEqual({p.name: p.read_bytes() for p in (self.engine.store.root / "artifacts").iterdir()}, files)

    def test_same_size_corruption_fails_without_exposing_content(self):
        task = self.create()
        item = self.engine.store.artifact(task, "private-content")
        path = self.engine.store.root / "artifacts" / item["sha256"]
        path.write_bytes(b"x" * item["size_bytes"])
        report = integrity_report(self.engine.store)
        self.assertEqual(report["status"], "failed")
        self.assertIsNone(report["tasks"])
        self.assertNotIn(item["sha256"], json.dumps(report))
        self.assertNotIn("private-content", json.dumps(report))
        self.assertEqual(path.read_bytes(), b"x" * item["size_bytes"])

    def test_projection_drift_and_missing_artifacts_fail(self):
        task = self.create()
        self.engine.store.db.execute("UPDATE tasks SET context='{}' WHERE id=?", (task,))
        self.assertEqual(integrity_report(self.engine.store)["status"], "failed")
        next((self.engine.store.root / "artifacts").iterdir()).unlink()
        self.assertEqual(integrity_report(self.engine.store)["status"], "failed")

    def test_api_auth_closed_queries_and_busy_dispatcher(self):
        self.create()
        app = Application(self.engine)
        route = "/integrity-report"
        self.assertEqual(app.dispatch("GET", route, {}, "wrong")[0], 401)
        self.assertEqual(app.dispatch("GET", route + "?repair=true", {}, app.session)[0], 409)
        self.assertEqual(app.dispatch("POST", route, {}, app.session)[0], 404)
        self.assertEqual(app.dispatch("GET", route, {}, app.session)[1]["status"], "passed")
        with self.engine.store.exclusive():
            self.assertEqual(app.dispatch("GET", route, {}, app.session)[0], 409)

    def test_cli_exit_status_and_no_database_creation(self):
        task = self.create()
        command = [sys.executable, "-m", "orch", "audit", "--data"]
        result = subprocess.run([*command, str(self.engine.store.root)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "passed")
        self.engine.store.db.execute("UPDATE tasks SET context='{}' WHERE id=?", (task,))
        result = subprocess.run([*command, str(self.engine.store.root)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["status"], "failed")
        with tempfile.TemporaryDirectory() as base:
            missing = Path(base) / "missing"
            result = subprocess.run([*command, str(missing)], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
