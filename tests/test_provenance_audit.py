import json
import hashlib
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_runtime as runtime
from orch.contracts import Rejected, canonical, digest, uid
from orch.maintenance import audit, backup, integrity_report, restore
from orch.storage import Store


class ProvenanceAuditTests(unittest.TestCase):
    setUp = runtime.RuntimeTests.setUp
    tearDown = runtime.RuntimeTests.tearDown
    create = runtime.RuntimeTests.create
    approve = runtime.RuntimeTests.approve
    to_action = runtime.RuntimeTests.to_action
    interrupted_broker = runtime.RuntimeTests.interrupted_broker

    def evidence(self):
        task = self.create()
        self.to_action(task)
        store = self.engine.store
        row = store.db.execute("SELECT * FROM execution_provenance LIMIT 1").fetchone()
        envelope = json.loads(store.read_artifact(json.loads(row["artifact"]), task))
        return task, row, envelope

    def test_envelope_bindings_reject_even_when_artifact_and_envelope_hashes_match(self):
        task, row, original = self.evidence()
        store = self.engine.store
        for field, value in (("operation_id", uid()), ("task_id", uid()), ("generation", 2),
                             ("nonce", uid()), ("source_digest", "a" * 64), ("request_sha256", "b" * 64)):
            with self.subTest(field=field):
                store.db.execute("BEGIN IMMEDIATE")
                try:
                    store.db.execute("DROP TRIGGER execution_provenance_update")
                    envelope = {**original, field: value}
                    artifact = store.artifact(task, envelope, producer="action_broker", trust="trusted_receipt")
                    store.db.execute("UPDATE execution_provenance SET artifact=?,envelope_sha256=? WHERE operation_id=?",
                                     (canonical(artifact).decode(), digest(envelope), row["operation_id"]))
                    self.assertEqual(integrity_report(store)["status"], "failed")
                finally:
                    store.db.execute("ROLLBACK")
        audit(store)

    def test_request_row_bindings_and_missing_request_reject(self):
        task, row, _ = self.evidence()
        store = self.engine.store
        raw = store.db.execute("SELECT request FROM broker_requests WHERE operation_id=?", (row["operation_id"],)).fetchone()[0]
        for field, value in (("operation_id", uid()), ("task_id", uid()), ("generation", 2)):
            with self.subTest(field=field):
                store.db.execute("BEGIN IMMEDIATE")
                try:
                    store.db.execute("DROP TRIGGER broker_requests_update")
                    request = {**json.loads(raw), field: value}
                    store.db.execute("UPDATE broker_requests SET request=? WHERE operation_id=?",
                                     (canonical(request).decode(), row["operation_id"]))
                    with self.assertRaises(Rejected):
                        audit(store)
                finally:
                    store.db.execute("ROLLBACK")
        store.db.execute("DROP TRIGGER broker_requests_delete")
        store.db.execute("DELETE FROM broker_requests WHERE operation_id=?", (row["operation_id"],))
        self.assertEqual(integrity_report(store)["status"], "failed")

    def test_hash_trust_and_cross_task_artifact_reject(self):
        task, row, envelope = self.evidence()
        other = self.create()
        store = self.engine.store
        for owner, producer, trust, sha in ((task, "action_broker", "trusted_receipt", "0" * 64),
                                          (task, "orchestrator", "untrusted", digest(envelope)),
                                          (other, "action_broker", "trusted_receipt", digest(envelope))):
            with self.subTest(owner=owner, producer=producer, sha=sha):
                store.db.execute("BEGIN IMMEDIATE")
                try:
                    store.db.execute("DROP TRIGGER execution_provenance_update")
                    artifact = store.artifact(owner, envelope, producer=producer, trust=trust)
                    store.db.execute("UPDATE execution_provenance SET artifact=?,envelope_sha256=? WHERE operation_id=?",
                                     (canonical(artifact).decode(), sha, row["operation_id"]))
                    with self.assertRaises(Rejected):
                        audit(store)
                finally:
                    store.db.execute("ROLLBACK")

    def test_pending_and_retired_evidence_survive_backup_restore_and_code_upgrade(self):
        task, state = self.interrupted_broker()
        # A request without committed provenance is a legitimate interrupted state.
        audit(self.engine.store)
        self.engine.recover(task, state["revision"])
        with patch("orch.broker.source_digest", side_effect=AssertionError("No live runtime checks")), tempfile.TemporaryDirectory() as root:
            before = self.engine.store.db.total_changes
            audit(self.engine.store)
            self.assertEqual(self.engine.store.db.total_changes, before)
            source, target = Path(root) / "backup", Path(root) / "restored"
            backup(self.engine.store, source)
            restore(source, target)
            recovered = Store(target, read_only=True)
            try:
                audit(recovered)
            finally:
                recovered.close()

    def test_corrupt_provenance_blocks_backup_publication(self):
        _, row, _ = self.evidence()
        store = self.engine.store
        store.db.execute("DROP TRIGGER execution_provenance_update")
        store.db.execute("UPDATE execution_provenance SET envelope_sha256=? WHERE operation_id=?", ("0" * 64, row["operation_id"]))
        with tempfile.TemporaryDirectory() as root:
            destination = Path(root) / "backup"
            with self.assertRaises(Rejected):
                backup(store, destination)
            self.assertFalse(destination.exists())

    def test_corrupt_provenance_blocks_restore_even_with_updated_manifest(self):
        _, row, _ = self.evidence()
        with tempfile.TemporaryDirectory() as root:
            source, destination = Path(root) / "backup", Path(root) / "restored"
            backup(self.engine.store, source)
            db = sqlite3.connect(source / "orch.sqlite")
            try:
                db.execute("DROP TRIGGER execution_provenance_update")
                db.execute("UPDATE execution_provenance SET envelope_sha256=? WHERE operation_id=?",
                           ("0" * 64, row["operation_id"]))
                db.commit()
            finally:
                db.close()
            manifest = json.loads((source / "manifest.json").read_text())
            manifest["database_sha256"] = hashlib.sha256((source / "orch.sqlite").read_bytes()).hexdigest()
            (source / "manifest.json").write_bytes(canonical(manifest))
            with self.assertRaises(Rejected):
                restore(source, destination)
            self.assertFalse(destination.exists())
            self.assertEqual(list(Path(root).iterdir()), [source])


if __name__ == "__main__":
    unittest.main()
