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
from orch.maintenance import backup, verify_backup


class VerifyBackupTests(unittest.TestCase):
    setUp = helpers.WorkflowTests.setUp
    tearDown = helpers.WorkflowTests.tearDown
    create = helpers.WorkflowTests.create

    def snapshot(self, root):
        self.create()
        source = Path(root) / "backup"
        backup(self.engine.store, source)
        return source

    def test_cli_passes_without_restoration_or_changed_snapshot_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            source = self.snapshot(root)
            before = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()}
            result = subprocess.run([sys.executable, "-m", "orch", "verify-backup", "--data", str(source)], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "passed")
            after = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()}
            self.assertEqual(before, after)
            self.assertEqual(list(Path(root).iterdir()), [source])
            self.assertEqual(verify_backup(source)["status"], "passed")

    def test_corrupt_and_missing_inputs_fail_with_bounded_report(self):
        with tempfile.TemporaryDirectory() as root:
            missing = Path(root) / "missing"
            result = subprocess.run([sys.executable, "-m", "orch", "verify-backup", "--data", str(missing)], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(missing.exists())
            source = self.snapshot(root)
            manifest = json.loads((source / "manifest.json").read_text())
            blob = source / "artifacts" / manifest["artifacts"][0]
            raw = blob.read_bytes()
            blob.write_bytes(b"x" * len(raw))
            report = verify_backup(source)
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["reason"], "backup_integrity_failure")
            self.assertIsNone(report["tasks"])
            self.assertIsNone(report["artifacts"])
            self.assertNotIn(str(source), json.dumps(report))

    def test_projection_corruption_with_recomputed_manifest_still_fails(self):
        with tempfile.TemporaryDirectory() as root:
            source = self.snapshot(root)
            db = sqlite3.connect(source / "orch.sqlite")
            db.execute("UPDATE tasks SET context='{}'")
            db.commit()
            db.close()
            manifest_path = source / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["database_sha256"] = hashlib.sha256((source / "orch.sqlite").read_bytes()).hexdigest()
            manifest_path.write_text(json.dumps(manifest))
            self.assertEqual(verify_backup(source)["status"], "failed")

    def test_manifest_reference_set_sidecars_and_paths_reject(self):
        with tempfile.TemporaryDirectory() as root:
            source = self.snapshot(root)
            for suffix in ("-wal", "-journal"):
                sidecar = source / ("orch.sqlite" + suffix)
                sidecar.write_bytes(b"retain")
                with patch("orch.maintenance.Store", side_effect=AssertionError("Do not open a live backup")):
                    self.assertEqual(verify_backup(source)["status"], "failed")
                self.assertEqual(sidecar.read_bytes(), b"retain")
                sidecar.unlink()
            from orch.contracts import Rejected
            with patch("orch.maintenance.plain", side_effect=Rejected("link")):
                self.assertEqual(verify_backup(source)["status"], "failed")
            manifest_path = source / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["artifacts"].pop()
            manifest_path.write_text(json.dumps(manifest))
            self.assertEqual(verify_backup(source)["status"], "failed")


if __name__ == "__main__":
    unittest.main()
