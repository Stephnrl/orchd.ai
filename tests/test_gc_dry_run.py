import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import test_workflow as helpers
from orch.contracts import Rejected
from orch.maintenance import collect_orphans, plain


class GCDryRunTests(unittest.TestCase):
    setUp = helpers.WorkflowTests.setUp
    tearDown = helpers.WorkflowTests.tearDown
    create = helpers.WorkflowTests.create

    def orphan(self, name, age=90000):
        path = self.engine.store.root / "artifacts" / name
        path.write_bytes(b"orphan-data")
        os.utime(path, (time.time() - age,) * 2)
        return path

    def test_dry_run_reports_exact_candidates_without_deletion(self):
        self.create()
        old = self.orphan("f" * 64)
        fresh = self.orphan("e" * 64, age=30)
        report = collect_orphans(self.engine.store, dry_run=True)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["removed"], [])
        self.assertEqual([item["name"] for item in report["candidates"]], [old.name])
        self.assertEqual(report["candidates"][0]["size_bytes"], len(b"orphan-data"))
        self.assertTrue(old.exists())
        self.assertTrue(fresh.exists())
        removed = collect_orphans(self.engine.store)
        self.assertFalse(old.exists())
        self.assertEqual(removed["removed"], [old.name])
        self.assertEqual(removed["candidates"][0]["name"], old.name)

    def test_dry_run_revalidates_links_age_and_minimum_grace(self):
        self.create()
        with patch("orch.maintenance.plain", side_effect=Rejected("Maintenance refuses links/junctions")):
            with self.assertRaises(Rejected):
                collect_orphans(self.engine.store, dry_run=True)
        with self.assertRaises(Rejected):
            collect_orphans(self.engine.store, grace_seconds=3599, dry_run=True)

    def test_cli_dry_run_requires_existing_store_and_never_opens_provider(self):
        self.create()
        old = self.orphan("c" * 64)
        result = subprocess.run([sys.executable, "-m", "orch", "gc", "--data", str(self.engine.store.root), "--dry-run"], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["removed"], [])
        self.assertTrue(old.exists())
        with tempfile.TemporaryDirectory() as base:
            missing = Path(base) / "missing"
            result = subprocess.run([sys.executable, "-m", "orch", "gc", "--data", str(missing), "--dry-run"], capture_output=True, text=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(missing.exists())

    def test_late_scan_rejection_preserves_all_candidates(self):
        old = self.orphan("a" * 64)
        late = self.orphan("f" * 64)
        def reject_late(path):
            if path == late:
                raise Rejected("Unsafe late entry")
            return plain(path)
        for preview in (False, True):
            with self.subTest(preview=preview), patch("orch.maintenance.plain", side_effect=reject_late):
                with self.assertRaises(Rejected):
                    collect_orphans(self.engine.store, dry_run=preview)
            self.assertEqual(old.read_bytes(), b"orphan-data")
            self.assertTrue(late.exists())

    def test_invalid_grace_never_removes_files(self):
        old = self.orphan("a" * 64)
        for grace in (float("nan"), float("inf"), -float("inf"), True, None, "86400", 3599):
            with self.subTest(grace=grace), self.assertRaises(Rejected):
                collect_orphans(self.engine.store, grace_seconds=grace)
            self.assertTrue(old.exists())

    def test_cli_rejects_old_database_without_migration(self):
        with tempfile.TemporaryDirectory() as base:
            database = Path(base) / "orch.sqlite"
            db = sqlite3.connect(database)
            db.execute("PRAGMA user_version=1")
            db.close()
            before = database.read_bytes()
            for extra in ([], ["--dry-run"]):
                result = subprocess.run([sys.executable, "-m", "orch", "gc", "--data", base, *extra], capture_output=True, text=True, timeout=20)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(database.read_bytes(), before)
                self.assertFalse((Path(base) / "artifacts").exists())


if __name__ == "__main__":
    unittest.main()
