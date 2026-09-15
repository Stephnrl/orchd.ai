"""Retiring a full pilot journal and starting its successor, without deleting a row."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_pilot as fixtures
from orch.contracts import Rejected, canonical, digest
from orch.pilot import Pilot
from orch.pilot_backup import audit_journal, backup_journal
from orch.pilot_retention import reclaim, usage
from orch.pilot_succession import TABLE, chain, record, retire, state

ROOT = Path(__file__).resolve().parents[1]
REASON = "Journal full; continuing in its successor"


class SuccessionTests(unittest.TestCase):
    git = fixtures.PilotTests.git

    def setUp(self):
        fixtures.PilotTests.setUp(self)
        self.successor = self.root / "journal-002"
        self.snapshot = self.root / "snapshot-cli"
        self.counter = 0

    def tearDown(self):
        fixtures.PilotTests.tearDown(self)

    def settle(self):
        prepared = self.pilot.prepare(self.intent)
        return self.pilot.run(prepared["id"], prepared["scope_sha256"], 0)

    def snap(self, root=None, destination=None):
        """Each snapshot gets its own directory; a bundle path is never reused."""
        self.counter += 1
        destination = destination or self.root / ("snapshot-%02d" % self.counter)
        return {**backup_journal(root or self.pilot.root, destination), "destination": destination}

    def retire(self, **overrides):
        created = overrides.pop("created", None) or self.snap()
        arguments = {"root": self.pilot.root, "snapshot": created["destination"], "expected_sha256": created["sha256"],
                     "successor": self.successor, "reason": REASON}
        arguments.update(overrides)
        return retire(**arguments)

    def test_retirement_closes_one_journal_and_opens_the_next_without_deleting_a_row(self):
        done = self.settle()
        before = audit_journal(self.pilot.root)
        report = self.retire()
        self.assertEqual(report["status"], "retired")
        self.assertEqual(report["rows_deleted"], 0)
        self.assertEqual(report["retained_rows"], 1)
        self.assertEqual(report["successor"], str(self.successor.resolve()))
        # Every retained row is untouched and still verifies.
        self.assertEqual(canonical(audit_journal(self.pilot.root)), canonical(before))
        self.assertEqual(self.pilot.inspect(done["id"])["status"], "passed")
        self.assertTrue(self.pilot.inspect(done["id"])["matches_receipt"])
        # The retired journal refuses only preparation.
        with self.assertRaisesRegex(Rejected, "retired; prepare in its successor"):
            self.pilot.prepare(self.intent)
        self.assertEqual(self.pilot.db.execute("SELECT count(*) FROM pilots").fetchone()[0], 1)
        # The successor is an ordinary working journal that names its predecessor.
        heir = Pilot(self.successor)
        try:
            prepared = heir.prepare(self.intent)
            self.assertEqual(heir.run(prepared["id"], prepared["scope_sha256"], 0)["status"], "passed")
            self.assertEqual(state(heir)["predecessor"], str(self.pilot.root))
            self.assertEqual(state(heir)["status"], "active")
        finally:
            heir.close()

    def test_a_retired_journal_can_still_be_reconciled_reclaimed_and_inspected(self):
        done = self.settle()
        self.retire()
        self.assertEqual(self.pilot.inspect(done["id"])["status"], "passed")
        reclaimed = reclaim(self.pilot, done["id"], done["scope_sha256"], 2, reason=REASON)
        self.assertTrue(reclaimed["deleted"])
        self.assertEqual(self.pilot.inspect(done["id"])["reclamation"], "reclaimed")
        self.assertEqual(self.pilot.db.execute("SELECT count(*) FROM pilots").fetchone()[0], 1)

    def test_retirement_needs_a_matching_snapshot_and_an_audited_reason(self):
        self.settle()
        created = self.snap()
        with self.assertRaises(Rejected):
            self.retire(created=created, expected_sha256="a" * 64)
        for reason in (None, "", "   ", "x" * 501, "line\nbreak", "token=secret-value"):
            with self.subTest(reason=reason), self.assertRaises(Rejected):
                self.retire(created=created, reason=reason)
        # A pilot prepared after the snapshot makes it stale, and retirement refuses.
        self.pilot.prepare(self.intent)
        with self.assertRaisesRegex(Rejected, "matches the journal exactly"):
            self.retire(created=created)
        self.assertEqual(state(self.pilot)["status"], "active")
        self.assertFalse((self.successor / "pilot.sqlite").exists())

    def test_every_pilot_must_be_settled_first(self):
        self.settle()
        prepared = self.pilot.prepare(self.intent)
        with self.assertRaisesRegex(Rejected, prepared["id"]):
            self.retire()
        self.pilot.abandon(prepared["id"], prepared["scope_sha256"], 0)
        self.assertEqual(self.retire()["status"], "retired")

    def test_a_reserved_pilot_blocks_retirement_until_it_is_reconciled(self):
        prepared = self.pilot.prepare(self.intent)
        with patch("orch.pilot.evaluate", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                self.pilot.run(prepared["id"], prepared["scope_sha256"], 0)
        self.assertEqual(self.pilot.inspect(prepared["id"])["status"], "reserved")
        with self.assertRaisesRegex(Rejected, "once every pilot is settled"):
            self.retire()
        self.pilot.reconcile(prepared["id"], prepared["scope_sha256"], 1)
        self.assertEqual(self.retire()["status"], "retired")

    def test_successor_paths_are_refused_when_unsafe_or_already_in_use(self):
        self.settle()
        created = self.snap()
        for successor, message in ((self.pilot.root, "separate directory"),
                                   (self.pilot.root / "inside", "contain, or sit inside"),
                                   (self.root, "contain, or sit inside")):
            with self.subTest(successor=str(successor)), self.assertRaisesRegex(Rejected, message):
                self.retire(created=created, successor=successor)
        occupied = Pilot(self.root / "occupied")
        try:
            occupied.prepare(self.intent)
        finally:
            occupied.close()
        with self.assertRaisesRegex(Rejected, "must not already exist"):
            self.retire(created=created, successor=self.root / "occupied")
        self.assertEqual(state(self.pilot)["status"], "active")
        self.assertEqual(self.retire(created=created)["status"], "retired")
        # A journal reports its own state before anything about the successor path.
        with self.assertRaisesRegex(Rejected, "already retired"):
            self.retire(created=created, successor=self.root / "journal-004")

    def test_a_successor_already_in_use_cannot_be_claimed_again(self):
        self.settle()
        self.retire()
        other = Pilot(self.root / "journal-003")
        try:
            prepared = other.prepare(self.intent)
            other.run(prepared["id"], prepared["scope_sha256"], 0)
            self.assertIsNone(state(other)["predecessor"])
        finally:
            other.close()
        second = self.snap(root=self.root / "journal-003")
        with self.assertRaisesRegex(Rejected, "must not already exist"):
            retire(self.root / "journal-003", second["destination"], second["sha256"], self.successor, REASON)
        heir = Pilot(self.successor, read_only=True)
        try:
            self.assertEqual(state(heir)["predecessor"], str(self.pilot.root))
        finally:
            heir.close()

    def test_the_chain_walks_predecessors_and_checks_every_link(self):
        self.settle()
        self.retire()
        heir = Pilot(self.successor)
        try:
            heir.prepare(self.intent)
        finally:
            heir.close()
        report = chain(self.successor)
        self.assertEqual(report["journal_count"], 2)
        self.assertEqual(report["retained_rows"], 2)
        self.assertEqual([entry["status"] for entry in report["journals"]], ["active", "retired"])
        self.assertEqual(report["journals"][0]["journal_root"], str(self.successor.resolve()))
        self.assertEqual(report["journals"][1]["successor"], str(self.successor.resolve()))
        self.assertEqual(report["rows_deleted"], 0)
        self.assertFalse(report["live_authorized"])
        # A journal with no predecessor is a chain of one.
        self.assertEqual(chain(self.pilot.root)["journal_count"], 1)
        with self.assertRaises(Rejected):
            chain(self.successor, limit=1)

    def test_a_broken_or_missing_link_is_refused_rather_than_reported(self):
        self.settle()
        self.retire()
        heir = Pilot(self.successor)
        try:
            follows = record(heir, "follows")
            forged = {**follows, "predecessor": str(self.root / "absent")}
            heir.db.execute("UPDATE " + TABLE + " SET record=?,hash=? WHERE role='follows'",
                            (canonical(forged).decode(), digest(forged)))
        finally:
            heir.close()
        with self.assertRaisesRegex(Rejected, "predecessor journal named in the chain is missing"):
            chain(self.successor)
        # A predecessor that does not name this journal back is refused too.
        elsewhere = Pilot(self.root / "unrelated")
        try:
            elsewhere.prepare(self.intent)
        finally:
            elsewhere.close()
        heir = Pilot(self.successor)
        try:
            forged = {**record(heir, "follows"), "predecessor": str((self.root / "unrelated").resolve())}
            heir.db.execute("UPDATE " + TABLE + " SET record=?,hash=? WHERE role='follows'",
                            (canonical(forged).decode(), digest(forged)))
        finally:
            heir.close()
        with self.assertRaisesRegex(Rejected, "does not name this journal as its successor"):
            chain(self.successor)

    def test_a_tampered_succession_record_is_refused(self):
        self.settle()
        self.retire()
        original = self.pilot.db.execute("SELECT record,hash FROM " + TABLE + " WHERE role='retired'").fetchone()
        forged = {**json.loads(original[0]), "successor": str(self.root / "elsewhere")}
        self.pilot.db.execute("UPDATE " + TABLE + " SET record=? WHERE role='retired'", (canonical(forged).decode(),))
        with self.assertRaisesRegex(Rejected, "succession record integrity"):
            state(self.pilot)
        self.pilot.db.execute("UPDATE " + TABLE + " SET record=?,hash=? WHERE role='retired'", original)
        self.assertEqual(state(self.pilot)["status"], "retired")

    def test_usage_reports_the_journal_status_and_both_neighbours(self):
        self.settle()
        active = usage(self.pilot)
        self.assertEqual(active["journal_status"], "active")
        self.assertIsNone(active["successor"])
        self.assertIsNone(active["predecessor"])
        self.retire()
        retired = usage(self.pilot)
        self.assertEqual(retired["journal_status"], "retired")
        self.assertEqual(retired["successor"], str(self.successor.resolve()))
        heir = Pilot(self.successor, read_only=True)
        try:
            self.assertEqual(usage(heir)["predecessor"], str(self.pilot.root))
        finally:
            heir.close()

    def test_cli_retire_and_chain(self):
        done = self.settle()
        env = {**os.environ, "PYTHONPATH": str(ROOT)}

        def cli(*args, data=None, expect=0):
            result = subprocess.run([sys.executable, "-m", "orch", *args, "--data", str(data or self.pilot.root)],
                                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, expect, result.stderr[-400:])
            return json.loads(result.stdout) if result.returncode == 0 else result

        created = cli("pilot-journal-backup", "--destination", str(self.snapshot))
        cli("pilot-journal-retire", "--destination", str(self.snapshot), "--expected-sha256", created["sha256"],
            "--successor", str(self.successor), expect=2)  # A reason is required.
        report = cli("pilot-journal-retire", "--destination", str(self.snapshot), "--expected-sha256", created["sha256"],
                     "--successor", str(self.successor), "--reason", REASON)
        self.assertEqual((report["status"], report["rows_deleted"]), ("retired", 0))
        walked = cli("pilot-journal-chain", data=self.successor)
        self.assertEqual(walked["journal_count"], 2)
        self.assertEqual(walked["journals"][0]["predecessor"], str(self.pilot.root))
        self.assertEqual(cli("pilot-inspect", "--pilot-id", done["id"])["status"], "passed")


if __name__ == "__main__":
    unittest.main()
