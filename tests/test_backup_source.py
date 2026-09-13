import contextlib
import io
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_workflow as helpers
from orch.__main__ import main
from orch import maintenance
from orch.storage import Store


class BackupSourceTests(unittest.TestCase):
    setUp = helpers.WorkflowTests.setUp
    tearDown = helpers.WorkflowTests.tearDown
    create = helpers.WorkflowTests.create

    def command(self, source, destination):
        return [sys.executable, "-m", "orch", "backup", "--data", str(source), "--destination", str(destination)]

    def test_missing_source_never_creates_source_or_destination(self):
        with tempfile.TemporaryDirectory() as root:
            source, destination = Path(root) / "missing", Path(root) / "backup"
            result = subprocess.run(self.command(source, destination), capture_output=True, text=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("backup requires an existing workflow database", result.stderr)
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_legacy_source_is_unchanged_without_publication(self):
        with tempfile.TemporaryDirectory() as root:
            source, destination = Path(root) / "legacy", Path(root) / "backup"
            source.mkdir()
            database = source / "orch.sqlite"
            db = sqlite3.connect(database)
            db.execute("PRAGMA user_version=1")
            db.close()
            before = database.read_bytes()
            result = subprocess.run(self.command(source, destination), capture_output=True, text=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(database.read_bytes(), before)
            self.assertEqual(list(source.iterdir()), [database])
            self.assertFalse(destination.exists())

    def test_cli_source_rejects_writes_and_backup_restores(self):
        task = self.create()
        before = self.engine.store.task(task)
        database = list(self.engine.store.db.iterdump())
        backup = maintenance.backup
        observed = []
        def verify_readonly(store, destination):
            with self.assertRaises(sqlite3.OperationalError):
                store.db.execute("CREATE TABLE forbidden_write(value TEXT)")
            observed.append(True)
            return backup(store, destination)
        with tempfile.TemporaryDirectory() as root:
            destination, restored = Path(root) / "backup", Path(root) / "restored"
            with patch.object(sys, "argv", self.command(self.engine.store.root, destination)[2:]), patch.object(maintenance, "backup", side_effect=verify_readonly), contextlib.redirect_stdout(io.StringIO()):
                main()
            self.assertEqual(observed, [True])
            self.assertEqual(list(self.engine.store.db.iterdump()), database)
            maintenance.restore(destination, restored)
            copied = Store(restored, read_only=True)
            try:
                maintenance.audit(copied)
                self.assertEqual(copied.task(task), before)
            finally:
                copied.close()

    def test_rejected_source_path_never_opens_store(self):
        from orch.contracts import Rejected
        with patch.object(sys, "argv", self.command(self.engine.store.root, self.engine.store.root.parent / "unused")[2:]), patch.object(maintenance, "plain", side_effect=Rejected("unsafe source")), patch("orch.storage.Store", side_effect=AssertionError("No storage open")):
            with self.assertRaises(Rejected):
                main()


if __name__ == "__main__":
    unittest.main()
