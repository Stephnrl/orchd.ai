"""Pilot journal audits and snapshot bundles: integrity, drift and no execution authority."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_pilot as fixtures
from orch.contracts import Rejected, canonical, digest, uid
from orch.pilot import Pilot
from orch.pilot_backup import (MAX_MANIFEST_BYTES, audit, audit_journal, backup_journal,
                               compare_journal_backup, file_digest, verify_journal_backup)
from orch.pilot_retention import reclaim

ROOT = Path(__file__).resolve().parents[1]
REASON = "Snapshot taken; workspace no longer needed"


class PilotBackupTests(unittest.TestCase):
    git = fixtures.PilotTests.git

    def setUp(self):
        fixtures.PilotTests.setUp(self)
        self.bundle = self.root / "backup"

    def tearDown(self):
        fixtures.PilotTests.tearDown(self)

    def passed(self):
        prepared = self.pilot.prepare(self.intent)
        return self.pilot.run(prepared["id"], prepared["scope_sha256"], 0)

    def create(self, destination=None):
        return backup_journal(self.pilot.root, destination or self.bundle)

    def test_audit_covers_every_row_and_ignores_workspaces(self):
        done = self.passed()
        prepared = self.pilot.prepare(self.intent)
        report = audit_journal(self.pilot.root)
        self.assertEqual(report["kind"], "PilotJournalAudit")
        self.assertEqual(report["status"], "valid")
        self.assertEqual(report["binding"]["record_count"], 2)
        self.assertEqual(report["binding"]["live_pilots"], 2)
        by_id = {row["id"]: row for row in report["binding"]["records"]}
        self.assertEqual(by_id[done["id"]]["status"], "passed")
        self.assertEqual(by_id[done["id"]]["receipt_sha256"], done["receipt"] and digest(done["receipt"]))
        self.assertIsNone(by_id[prepared["id"]]["receipt_sha256"])
        self.assertIsNone(by_id[done["id"]]["reclamation"])
        for flag in ("restore_allowed", "retry_allowed", "execution_authorized", "live_authorized"):
            self.assertFalse(report[flag])
        # The audit is workspace-independent: deleting one changes nothing it reports.
        before = canonical(report)
        reclaim(self.pilot, done["id"], done["scope_sha256"], 2, reason=REASON)
        after = audit_journal(self.pilot.root)
        self.assertEqual(after["binding"]["records"][0]["id"], report["binding"]["records"][0]["id"])
        self.assertEqual(after["binding"]["live_pilots"], 1)
        changed = {row["id"]: row["reclamation"] for row in after["binding"]["records"]}
        self.assertEqual(changed[done["id"]], "reclaimed")
        self.assertNotEqual(canonical(after), before)  # Only the reclamation record moved.

    def test_tampered_rows_are_refused_by_the_audit(self):
        done = self.passed()
        cases = (
            ("UPDATE pilots SET hash=? WHERE id=?", ("f" * 64, done["id"]), "scope integrity"),
            ("UPDATE pilots SET revision=9 WHERE id=?", (done["id"],), "lifecycle integrity"),
            ("UPDATE pilots SET receipt=NULL WHERE id=?", (done["id"],), "receipt missing"),
            ("INSERT INTO pilot_workers VALUES(?,?,?)", (uid(), "{}", "a" * 64), "reference no pilot"),
        )
        for statement, arguments, message in cases:
            with self.subTest(message=message):
                self.pilot.db.execute("BEGIN")
                try:
                    self.pilot.db.execute(statement, arguments)
                    with self.assertRaisesRegex(Rejected, message):
                        audit(self.pilot.db)
                finally:
                    self.pilot.db.execute("ROLLBACK")
        audit(self.pilot.db)

    def test_a_receipt_that_no_longer_matches_its_scope_is_refused(self):
        done = self.passed()
        envelope = json.loads(self.pilot.db.execute("SELECT receipt FROM pilots WHERE id=?", (done["id"],)).fetchone()[0])
        receipt = {**envelope["receipt"], "files": {"config.json": "a" * 64}}
        self.pilot.db.execute("BEGIN")
        try:
            self.pilot.db.execute("UPDATE pilots SET receipt=? WHERE id=?",
                                  (canonical({"receipt": receipt, "sha256": digest(receipt)}).decode(), done["id"]))
            with self.assertRaisesRegex(Rejected, "receipt integrity"):
                audit(self.pilot.db)
        finally:
            self.pilot.db.execute("ROLLBACK")

    def test_backup_verify_and_compare_agree_then_detect_drift(self):
        done = self.passed()
        created = self.create()
        self.assertEqual(created["kind"], "PilotJournalBackupVerification")
        self.assertEqual(created["record_count"], 1)
        self.assertFalse(created["workspaces_included"])
        self.assertEqual({p.name for p in self.bundle.iterdir()}, {"pilot.sqlite", "manifest.json"})
        verified = verify_journal_backup(self.bundle, created["sha256"])
        self.assertEqual(verified["status"], "valid")
        self.assertEqual(verified["sha256"], created["sha256"])
        self.assertEqual(compare_journal_backup(self.pilot.root, self.bundle, created["sha256"])["status"], "matches")
        # The workspace is not in the bundle, and reclaiming it is drift the comparison names.
        reclaim(self.pilot, done["id"], done["scope_sha256"], 2, reason=REASON)
        drifted = compare_journal_backup(self.pilot.root, self.bundle, created["sha256"])
        self.assertEqual(drifted["status"], "different")
        self.assertEqual(drifted["binding"]["differences"], [{"id": done["id"], "reasons": ["reclamation_mismatch"]}])
        # A pilot created after the snapshot is missing from it, not an error.
        fresh = self.pilot.prepare(self.intent)
        later = compare_journal_backup(self.pilot.root, self.bundle, created["sha256"])
        self.assertIn({"id": fresh["id"], "reasons": ["missing_from_backup"]}, later["binding"]["differences"])
        for flag in ("restore_allowed", "retry_allowed", "execution_authorized", "live_authorized"):
            self.assertFalse(later[flag])

    def test_lifecycle_advance_and_backup_only_pilots_are_named(self):
        prepared = self.pilot.prepare(self.intent)
        created = self.create()
        self.pilot.abandon(prepared["id"], prepared["scope_sha256"], 0)
        report = compare_journal_backup(self.pilot.root, self.bundle, created["sha256"])
        self.assertEqual(report["binding"]["differences"], [{"id": prepared["id"], "reasons": ["lifecycle_advanced"]}])
        empty = Pilot(self.root / "empty-journal")
        try:
            backup_only = compare_journal_backup(empty.root, self.bundle, created["sha256"])
        finally:
            empty.close()
        self.assertEqual(backup_only["binding"]["differences"], [{"id": prepared["id"], "reasons": ["backup_only_pilot"]}])

    def test_a_snapshot_cannot_execute_or_be_mistaken_for_the_live_journal(self):
        prepared = self.pilot.prepare(self.intent)
        created = self.create()
        copy = Pilot(self.bundle, read_only=True)
        try:
            with self.assertRaisesRegex(Rejected, "scope integrity mismatch"):
                copy.inspect(prepared["id"])
        finally:
            copy.close()
        self.assertEqual(verify_journal_backup(self.bundle, created["sha256"])["status"], "valid")
        # A bundle whose scopes would accept its own directory is not a snapshot.
        with patch("orch.pilot_backup._refuses_execution", return_value=False):
            with self.assertRaisesRegex(Rejected, "not a snapshot"):
                verify_journal_backup(self.bundle, created["sha256"])
        runner = Pilot(self.bundle)
        try:
            with self.assertRaises(Rejected):
                runner.run(prepared["id"], prepared["scope_sha256"], 0)
        finally:
            runner.close()
        # Opening a bundle for writing disturbs it, and verification refuses it afterwards.
        with self.assertRaises(Rejected):
            verify_journal_backup(self.bundle, created["sha256"])

    def test_bundles_are_exclusive_bounded_and_outside_the_journal(self):
        self.create()
        with self.assertRaisesRegex(Rejected, "never reused or overwritten"):
            self.create()
        with self.assertRaisesRegex(Rejected, "outside its own journal"):
            backup_journal(self.pilot.root, self.pilot.root / "inside")
        with self.assertRaises(Rejected):
            backup_journal(self.pilot.root, self.pilot.root)
        with patch("orch.pilot_backup.MAX_DATABASE_BYTES", 1):
            with self.assertRaises(Rejected):
                backup_journal(self.pilot.root, self.root / "too-big")
        self.assertFalse((self.root / "too-big").exists())
        with patch("orch.pilot_backup.MAX_MANIFEST_BYTES", 10):
            with self.assertRaises(Rejected):
                backup_journal(self.pilot.root, self.root / "tiny-manifest")

    def test_incomplete_changed_and_noncanonical_bundles_are_refused(self):
        created = self.create()
        with self.assertRaises(Rejected):
            verify_journal_backup(self.bundle, "f" * 64)
        manifest = self.bundle / "manifest.json"
        original = manifest.read_bytes()
        for mutation in (b'{"kind":"PilotJournalBackup"}', original + b" ",
                         canonical({**json.loads(original), "sha256": "a" * 64})):
            with self.subTest(mutation=mutation[:24]):
                manifest.write_bytes(mutation)
                with self.assertRaises(Rejected):
                    verify_journal_backup(self.bundle, created["sha256"])
        manifest.write_bytes(original)
        self.assertEqual(verify_journal_backup(self.bundle, created["sha256"])["status"], "valid")
        database = self.bundle / "pilot.sqlite"
        database.write_bytes(database.read_bytes() + b"\0")
        with self.assertRaises(Rejected):
            verify_journal_backup(self.bundle, created["sha256"])
        shutil.rmtree(self.bundle)
        with self.assertRaises(Rejected):
            verify_journal_backup(self.bundle, created["sha256"])
        partial = self.root / "partial"
        partial.mkdir()
        (partial / "manifest.json").write_bytes(original)
        with self.assertRaisesRegex(Rejected, "Incomplete or unsupported"):
            verify_journal_backup(partial, created["sha256"])
        (partial / "extra.txt").write_text("unexpected", encoding="utf-8")
        with self.assertRaisesRegex(Rejected, "Incomplete or unsupported"):
            verify_journal_backup(partial, created["sha256"])

    def test_creation_leaves_the_journal_and_its_workspaces_untouched(self):
        done = self.passed()
        before = audit_journal(self.pilot.root)
        workspace = sorted(p.name for p in (self.pilot.root / done["id"]).iterdir())
        self.create()
        self.assertEqual(canonical(audit_journal(self.pilot.root)), canonical(before))
        self.assertEqual(sorted(p.name for p in (self.pilot.root / done["id"]).iterdir()), workspace)
        self.assertEqual(self.pilot.inspect(done["id"])["status"], "passed")
        self.assertTrue(self.pilot.inspect(done["id"])["matches_receipt"])

    def test_cli_audit_backup_verify_and_compare(self):
        done = self.passed()
        env = {**os.environ, "PYTHONPATH": str(ROOT)}

        def cli(*args, expect=0):
            result = subprocess.run([sys.executable, "-m", "orch", *args, "--data", str(self.pilot.root)],
                                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, expect, result.stderr[-400:])
            return json.loads(result.stdout) if result.returncode in (0, 2) and result.stdout else result

        audited = cli("pilot-journal-audit")
        self.assertEqual(audited["kind"], "PilotJournalAudit")
        destination = self.root / "cli-bundle"
        created = cli("pilot-journal-backup", "--destination", str(destination))
        self.assertEqual(created["record_count"], 1)
        verified = cli("pilot-verify-journal-backup", "--destination", str(destination), "--expected-sha256", created["sha256"])
        self.assertEqual(verified["status"], "valid")
        matched = cli("pilot-compare-journal-backup", "--destination", str(destination), "--expected-sha256", created["sha256"])
        self.assertEqual(matched["status"], "matches")
        reclaim(self.pilot, done["id"], done["scope_sha256"], 2, reason=REASON)
        drifted = cli("pilot-compare-journal-backup", "--destination", str(destination),
                      "--expected-sha256", created["sha256"], expect=2)
        self.assertEqual(drifted["status"], "different")
        cli("pilot-verify-journal-backup", "--destination", str(destination), "--expected-sha256", "a" * 64, expect=2)
        cli("pilot-journal-backup", "--destination", str(destination), expect=2)  # Never reuse a destination.


if __name__ == "__main__":
    unittest.main()
