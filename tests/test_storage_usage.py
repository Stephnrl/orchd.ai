import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

import test_workflow as helpers
from orch.api import Application
from orch.contracts import Rejected
from orch.maintenance import collect_orphans, storage_usage
from orch.storage import Store


class StorageUsageTests(unittest.TestCase):
    setUp = helpers.WorkflowTests.setUp
    tearDown = helpers.WorkflowTests.tearDown
    create = helpers.WorkflowTests.create

    def test_duplicates_count_logically_but_not_physically(self):
        task = self.create()
        before = storage_usage(self.engine.store, task)
        for _ in range(2):
            self.engine.store.artifact(task, "unique storage usage sample")
        after = storage_usage(self.engine.store, task)
        size = len(b"unique storage usage sample")
        self.assertEqual(after["logical_bytes"] - before["logical_bytes"], size * 2)
        self.assertEqual(after["physical_artifact_bytes"] - before["physical_artifact_bytes"], size)
        self.assertEqual(after["references"] - before["references"], 2)
        self.assertEqual(after["task_headroom_bytes"], self.engine.store.task_quota - after["logical_bytes"])
        self.engine.store.task_quota = 1
        self.engine.store.disk_quota = 1
        exhausted = storage_usage(self.engine.store, task)
        self.assertEqual(exhausted["task_headroom_bytes"], 0)
        self.assertEqual(exhausted["disk_headroom_bytes"], 0)
        self.assertEqual(exhausted["audit_reserve_bytes"], 4 * 1024 * 1024)

    def test_gc_candidates_match_collection_without_deleting_on_read(self):
        task = self.create()
        root = self.engine.store.root / "artifacts"
        old = root / ("f" * 64)
        staged = root / ("e" * 64 + "." + "d" * 32)
        fresh = root / ("c" * 64)
        unknown = root / "operator-notes.txt"
        for file in (old, staged, fresh, unknown):
            file.write_bytes(b"abc")
        for file in (old, staged, unknown):
            os.utime(file, (time.time() - 90000,) * 2)
        (root / "unexpected-directory").mkdir()
        before = self.engine.task(task)
        report = storage_usage(self.engine.store)
        self.assertEqual((report["orphan_files"], report["orphan_bytes"]), (3, 9))
        self.assertEqual((report["gc_eligible_files"], report["gc_eligible_bytes"]), (2, 6))
        self.assertEqual(report["unexpected_entries"], 2)
        self.assertTrue(all(p.exists() for p in (old, staged, fresh, unknown)))
        self.assertEqual(self.engine.task(task), before)
        self.assertEqual(set(collect_orphans(self.engine.store)["removed"]), {old.name, staged.name})
        self.assertEqual(storage_usage(self.engine.store)["gc_eligible_files"], 0)

    def test_missing_and_wrong_size_are_reported_without_content_exposure(self):
        task = self.create()
        item = self.engine.store.artifact(task, "private artifact contents")
        file = self.engine.store.root / "artifacts" / item["sha256"]
        file.write_bytes(b"wrong size")
        report = storage_usage(self.engine.store, task)
        self.assertEqual(report["size_mismatches"], 1)
        file.unlink()
        report = storage_usage(self.engine.store, task)
        self.assertEqual(report["missing_referenced_files"], 1)
        self.assertNotIn(item["sha256"], json.dumps(report))
        self.assertNotIn("private artifact contents", json.dumps(report))

    def test_api_requires_auth_and_rejects_queries_unknown_tasks_and_busy_store(self):
        task = self.create()
        app = Application(self.engine)
        for route in ("/storage", f"/tasks/{task}/storage"):
            self.assertEqual(app.dispatch("GET", route, {}, "wrong")[0], 401)
            self.assertEqual(app.dispatch("GET", route + "?limit=1", {}, app.session)[0], 409)
            self.assertEqual(app.dispatch("GET", route, {}, app.session)[0], 200)
            with self.engine.store.exclusive():
                self.assertEqual(app.dispatch("GET", route, {}, app.session)[0], 409)
        self.assertEqual(app.dispatch("GET", "/tasks/unknown/storage", {}, app.session)[0], 409)

    def test_task_scope_and_read_only_connection(self):
        task, other = self.create(), self.create()
        report = storage_usage(self.engine.store, task)
        all_tasks = storage_usage(self.engine.store)
        self.assertEqual(all_tasks["logical_bytes"], report["logical_bytes"] + storage_usage(self.engine.store, other)["logical_bytes"])
        self.assertIsNone(all_tasks["task_headroom_bytes"])
        readonly = Store(self.engine.store.root, read_only=True)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                readonly.db.execute("DELETE FROM tasks")
            self.assertEqual(storage_usage(readonly, task)["logical_bytes"], report["logical_bytes"])
        finally:
            readonly.close()

    def test_cli_no_docker_or_creation_and_no_legacy_migration(self):
        task = self.create()
        command = [sys.executable, "-m", "orch", "storage-usage", "--data"]
        result = subprocess.run([*command, str(self.engine.store.root), "--task", task], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["task_id"], task)
        with tempfile.TemporaryDirectory() as base:
            missing = Path(base) / "missing"
            result = subprocess.run([*command, str(missing)], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(missing.exists())
            db = sqlite3.connect(Path(base) / "orch.sqlite")
            db.execute("PRAGMA user_version=1")
            db.close()
            with self.assertRaises(Rejected):
                Store(base, read_only=True)
            db = sqlite3.connect(Path(base) / "orch.sqlite")
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master").fetchone()[0], 0)
            db.close()


if __name__ == "__main__":
    unittest.main()
