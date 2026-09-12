import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_workflow as helpers
from orch.contracts import Rejected
from orch import maintenance
from orch.storage import Store


class RestorePublicationTests(unittest.TestCase):
    setUp = helpers.WorkflowTests.setUp
    tearDown = helpers.WorkflowTests.tearDown
    create = helpers.WorkflowTests.create

    def snapshot(self, base):
        task = self.create()
        self.engine.run(task)
        source = Path(base) / "backup"
        maintenance.backup(self.engine.store, source)
        return source, Path(base) / "restored", task

    def manifest(self, source, change):
        path = source / "manifest.json"
        value = json.loads(path.read_text())
        change(value)
        path.write_text(json.dumps(value))

    def assert_unpublished(self, source, destination, error=Rejected):
        with self.assertRaises(error):
            maintenance.restore(source, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(list(destination.parent.glob(".orch-restore-*")), [])

    def test_projection_corruption_never_publishes_destination(self):
        with tempfile.TemporaryDirectory() as base:
            source, destination, task = self.snapshot(base)
            connection = sqlite3.connect(source / "orch.sqlite")
            connection.execute("UPDATE tasks SET context='{}' WHERE id=?", (task,))
            connection.commit()
            connection.close()
            digest = hashlib.sha256((source / "orch.sqlite").read_bytes()).hexdigest()
            self.manifest(source, lambda m: m.update(database_sha256=digest))
            self.assert_unpublished(source, destination)

    def test_manifest_must_match_database_artifacts_exactly(self):
        for extra in (False, True):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as base:
                source, destination, _ = self.snapshot(base)
                if extra:
                    raw = b"unreferenced backup content"
                    digest = hashlib.sha256(raw).hexdigest()
                    (source / "artifacts" / digest).write_bytes(raw)
                    self.manifest(source, lambda m: m["artifacts"].append(digest))
                else:
                    self.manifest(source, lambda m: m["artifacts"].pop())
                self.assert_unpublished(source, destination)

    def test_partial_copy_failure_cleans_staging_and_allows_retry(self):
        with tempfile.TemporaryDirectory() as base:
            source, destination, task = self.snapshot(base)
            original = maintenance.copy_bytes
            def interrupted(incoming, outgoing):
                if incoming.parent.name == "artifacts":
                    outgoing.write_bytes(b"partial")
                    raise OSError("injected disk failure")
                original(incoming, outgoing)
            with patch.object(maintenance, "copy_bytes", side_effect=interrupted):
                self.assert_unpublished(source, destination, OSError)
            maintenance.restore(source, destination)
            restored = Store(destination)
            try:
                self.assertEqual(restored.task(task), self.engine.store.task(task))
                maintenance.audit(restored)
            finally:
                restored.close()

    def test_copied_bytes_are_checked_before_opening_database(self):
        for database in (False, True):
            with self.subTest(database=database), tempfile.TemporaryDirectory() as base:
                source, destination, _ = self.snapshot(base)
                original = maintenance.copy_bytes
                def changed(incoming, outgoing):
                    original(incoming, outgoing)
                    if (incoming.name == "orch.sqlite") == database:
                        outgoing.write_bytes(b"changed after initial validation")
                with patch.object(maintenance, "copy_bytes", side_effect=changed):
                    self.assert_unpublished(source, destination)

    def test_destination_created_during_audit_is_preserved(self):
        with tempfile.TemporaryDirectory() as base:
            source, destination, _ = self.snapshot(base)
            original = maintenance.audit
            def concurrent_destination(store):
                original(store)
                destination.mkdir()
                (destination / "keep.txt").write_text("operator data")
            with patch.object(maintenance, "audit", side_effect=concurrent_destination), self.assertRaises(Rejected):
                maintenance.restore(source, destination)
            self.assertEqual((destination / "keep.txt").read_text(), "operator data")
            self.assertFalse((destination / "orch.sqlite").exists())
            self.assertEqual(list(Path(base).glob(".orch-restore-*")), [])

    def test_malformed_manifest_is_rejected_without_destination(self):
        for value in ([], {"version": True, "database_sha256": "a" * 64, "artifacts": []},
                      {"version": 1, "database_sha256": [], "artifacts": []}):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as base:
                source, destination, _ = self.snapshot(base)
                (source / "manifest.json").write_text(json.dumps(value))
                self.assert_unpublished(source, destination)

    def test_cli_requires_destination_before_opening_storage(self):
        with tempfile.TemporaryDirectory() as base:
            missing = Path(base) / "missing"
            for command in ("backup", "restore"):
                result = subprocess.run([sys.executable, "-m", "orch", command, "--data", str(missing)],
                                        capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 2)
                self.assertIn(command + " requires --destination", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
