import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_workflow as helpers
from orch import maintenance
from orch.contracts import Rejected
from orch.storage import Store


class BackupPublicationTests(unittest.TestCase):
    setUp = helpers.WorkflowTests.setUp
    tearDown = helpers.WorkflowTests.tearDown
    create = helpers.WorkflowTests.create

    def assert_unpublished(self, destination):
        self.assertFalse(destination.exists())
        self.assertEqual(list(destination.parent.glob(".orch-backup-*")), [])

    def test_partial_copy_failure_cleans_staging_and_retry_restores(self):
        task = self.create()
        self.engine.run(task)
        before = self.engine.task(task)
        with tempfile.TemporaryDirectory() as base:
            destination = Path(base) / "backup"
            def interrupted(source, target):
                target.write_bytes(b"partial")
                raise OSError("disk failure")
            with patch.object(maintenance, "copy_bytes", side_effect=interrupted), self.assertRaises(OSError):
                maintenance.backup(self.engine.store, destination)
            self.assert_unpublished(destination)
            self.assertEqual(self.engine.task(task), before)
            manifest = maintenance.backup(self.engine.store, destination)
            self.assertEqual(manifest, json.loads((destination / "manifest.json").read_text()))
            self.assertEqual(manifest["database_sha256"], hashlib.sha256((destination / "orch.sqlite").read_bytes()).hexdigest())
            target = Path(base) / "restored"
            maintenance.restore(destination, target)
            restored = Store(target)
            try:
                maintenance.audit(restored)
                self.assertEqual(restored.task(task), self.engine.store.task(task))
            finally:
                restored.close()

    def test_corrupt_copied_bytes_are_not_published(self):
        self.create()
        original = maintenance.copy_bytes
        def corrupted(source, target):
            original(source, target)
            target.write_bytes(b"corrupted after copy")
        with tempfile.TemporaryDirectory() as base:
            destination = Path(base) / "backup"
            with patch.object(maintenance, "copy_bytes", side_effect=corrupted), self.assertRaises(Rejected):
                maintenance.backup(self.engine.store, destination)
            self.assert_unpublished(destination)

    def test_existing_or_concurrently_created_destination_is_preserved(self):
        self.create()
        with tempfile.TemporaryDirectory() as base:
            destination = Path(base) / "backup"
            original = maintenance.audit
            def concurrent(store):
                original(store)
                if store.root != self.engine.store.root:
                    destination.mkdir()
                    (destination / "keep.txt").write_text("operator data")
            with patch.object(maintenance, "audit", side_effect=concurrent), self.assertRaises(Rejected):
                maintenance.backup(self.engine.store, destination)
            with self.assertRaises(Rejected):
                maintenance.backup(self.engine.store, destination)
            self.assertEqual((destination / "keep.txt").read_text(), "operator data")
            self.assertFalse((destination / "orch.sqlite").exists())
            self.assertEqual(list(Path(base).glob(".orch-backup-*")), [])

    def test_publication_failure_cleans_private_staging(self):
        self.create()
        with tempfile.TemporaryDirectory() as base:
            destination = Path(base) / "backup"
            with patch.object(Path, "rename", side_effect=OSError("publication denied")), self.assertRaises(OSError):
                maintenance.backup(self.engine.store, destination)
            self.assert_unpublished(destination)

    def test_source_audit_failure_never_creates_staging(self):
        task = self.create()
        item = self.engine.store.artifact(task, "audit fixture")
        (self.engine.store.root / "artifacts" / item["sha256"]).write_bytes(b"bad")
        with tempfile.TemporaryDirectory() as base:
            destination = Path(base) / "backup"
            with self.assertRaises(Rejected):
                maintenance.backup(self.engine.store, destination)
            self.assert_unpublished(destination)


if __name__ == "__main__":
    unittest.main()
