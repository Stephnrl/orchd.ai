"""Verified pilot journal audits and snapshot bundles. No restore, retry or execution authority.

The pilot journal holds the only evidence that local work actually ran: retained scopes,
receipts, worker journals and reclamation records. Standard workflow backups deliberately
exclude it, so until now it had no verified snapshot at all, unlike the GitHub and Jira
journals. These commands audit it, copy it, verify a received bundle and compare one with
the live journal. Nothing here restores over an active journal or grants execution.

Workspaces are never copied. They are disposable by design, reclamation deletes them, and
a receipt binds their bytes by hash, so the audit is deliberately workspace-independent
and gives the same answer for a live journal and for a copy of it.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time

from .contracts import Rejected, canonical, digest, now
from .maintenance import plain, real_path
from .pilot import Pilot, evaluate
from . import pilot_retention

MAX_DATABASE_BYTES = 32 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
FLAGS = {"restore_allowed": False, "retry_allowed": False, "execution_authorized": False, "live_authorized": False}


def _checked_path(path):
    return plain(Path(path).absolute())


def file_digest(path):
    path = _checked_path(path)
    if path.is_symlink() or not path.is_file():
        raise Rejected("Pilot backup database must be a regular file")
    sha, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            size += len(chunk)
            if size > MAX_DATABASE_BYTES:
                raise Rejected("Pilot backup database exceeds budget")
            sha.update(chunk)
    return sha.hexdigest(), size


def _copy_database(source, destination):
    source = _checked_path(source)
    if not source.is_file():
        raise Rejected("Pilot backup database must be a regular file")
    size = 0
    with source.open("rb") as reader, destination.open("xb") as writer:
        while chunk := reader.read(65536):
            size += len(chunk)
            if size > MAX_DATABASE_BYTES:
                raise Rejected("Pilot backup database exceeds budget")
            writer.write(chunk)


def _record(db, identifier, scope_json, scope_hash, revision, status, receipt_json):
    """One pilot's retained evidence, checked without reading any workspace."""
    try:
        scope = json.loads(scope_json)
    except ValueError as exc:
        raise Rejected("Unreadable retained pilot scope") from exc
    if digest(scope) != scope_hash or scope.get("id") != identifier:
        raise Rejected("Pilot scope integrity mismatch")
    revisions = {"prepared": {0}, "reserved": range(1, 1002), "passed": {2}, "failed": {2},
                 "abandoned": {1}, "interrupted": range(2, 1002)}
    if status not in revisions or revision not in revisions[status]:
        raise Rejected("Pilot lifecycle integrity mismatch")
    if (receipt_json is not None) != (status in ("passed", "failed")):
        raise Rejected("Pilot receipt missing or premature")
    receipt_sha = None
    if receipt_json is not None:
        envelope = json.loads(receipt_json)
        receipt = envelope["receipt"]
        if (digest(receipt) != envelope["sha256"] or receipt["scope_sha256"] != scope_hash
                or receipt["checks"] != evaluate(scope["replacements"], scope["checks"])
                or receipt["files"] != {p: hashlib.sha256(v.encode()).hexdigest() for p, v in scope["replacements"].items()}):
            raise Rejected("Pilot receipt integrity mismatch")
        if (status == "passed") != all(check["passed"] for check in receipt["checks"]):
            raise Rejected("Pilot outcome mismatch")
        receipt_sha = envelope["sha256"]
    workers_sha, dispatched = None, []
    row = db.execute("SELECT journal,hash FROM pilot_workers WHERE id=?", (identifier,)).fetchone()
    if row is not None:
        journal = json.loads(row[0])
        if digest(journal) != row[1] or journal.get("scope_sha256") != scope_hash:
            raise Rejected("Worker journal integrity mismatch")
        if set(journal.get("phases", {})) != {"edit", "test"}:
            raise Rejected("Incomplete worker journal")
        workers_sha = row[1]
        dispatched = sorted(p for p, e in journal["phases"].items() if e["status"] == "dispatched")
    if receipt_sha and workers_sha and json.loads(receipt_json)["receipt"].get("worker_journal_sha256") != workers_sha:
        raise Rejected("Worker receipt binding mismatch")
    reclamation, reclamation_sha = None, None
    try:
        saved = db.execute("SELECT status,record,hash FROM pilot_reclamations WHERE id=?", (identifier,)).fetchone()
    except sqlite3.OperationalError:
        saved = None  # A legacy journal has no reclamation table.
    if saved is not None:
        record = json.loads(saved[1])
        if (digest(record) != saved[2] or record["pilot_id"] != identifier or record["status"] != saved[0]
                or saved[0] not in ("reclaiming", "reclaimed")):
            raise Rejected("Reclamation record integrity mismatch")
        reclamation, reclamation_sha = saved[0], saved[2]
    binding = scope.get("task_binding")
    return {"id": identifier, "status": status, "revision": revision, "scope_sha256": scope_hash,
            "receipt_sha256": receipt_sha, "worker_journal_sha256": workers_sha,
            "unresolved_dispatches": dispatched, "reclamation": reclamation,
            "reclamation_sha256": reclamation_sha, "created_at": scope["created_at"],
            "expires_at": scope["expires_at"], "executor_mode": scope.get("executor", {}).get("mode"),
            "task_id": binding["task_id"] if binding else None}


def audit(db, journal_root=None):
    """Integrity of every retained row, derived only from the database.

    Identical for a live journal and for a snapshot of it, so the two can be compared.
    `journal_root` is the directory the database is being read from, which a live
    journal knows and a snapshot cannot; supplying it binds the retained scopes and the
    succession record to that directory, so a record borrowed from another journal is
    refused even when there are no pilots to derive the root from.
    """
    rows = db.execute("SELECT id,scope,hash,revision,status,receipt FROM pilots ORDER BY id").fetchall()
    if len(rows) > pilot_retention.JOURNAL_CAP:
        raise Rejected("Pilot journal exceeds its retained row cap")
    if db.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
        raise Rejected("Pilot journal storage check failed")
    records, roots, scope_bytes = [], set(), 0
    for identifier, scope_json, scope_hash, revision, status, receipt_json in rows:
        scope_bytes += len(scope_json.encode())
        if scope_bytes > MAX_DATABASE_BYTES:
            raise Rejected("Pilot journal exceeds audit budget")
        roots.add(json.loads(scope_json).get("journal_root"))
        records.append(_record(db, identifier, scope_json, scope_hash, revision, status, receipt_json))
    if len(roots) > 1:
        raise Rejected("Retained scopes name more than one journal root")
    orphans = db.execute("SELECT count(*) FROM pilot_workers WHERE id NOT IN (SELECT id FROM pilots)").fetchone()[0]
    try:
        orphans += db.execute("SELECT count(*) FROM pilot_reclamations WHERE id NOT IN (SELECT id FROM pilots)").fetchone()[0]
    except sqlite3.OperationalError:
        pass
    if orphans:
        raise Rejected("Retained worker or reclamation rows reference no pilot")
    # Bind the succession state too: retiring a journal is a real change to it, and a
    # snapshot that ignored it would keep reporting a match after the journal closed.
    from .pilot_succession import summary
    if journal_root is not None and roots and str(journal_root) not in roots:
        raise Rejected("Retained scopes name another journal root")
    bound = journal_root if journal_root is not None else (next(iter(roots)) if roots else None)
    succession = summary(db, bound)
    binding = {"schema_version": "1.0.0", "records": records, "record_count": len(records),
               "scope_bytes": scope_bytes, "live_pilots": pilot_retention.live_count(db),
               "succession": succession,
               "journal_cap": pilot_retention.JOURNAL_CAP, "live_cap": pilot_retention.LIVE_CAP}
    return {"kind": "PilotJournalAudit", "binding": binding, "sha256": digest(binding), "status": "valid", **FLAGS}


def audit_journal(root):
    """Audit a live journal read-only. Workspaces are neither read nor changed."""
    pilot = Pilot(root, read_only=True)
    try:
        return audit(pilot.db, pilot.root)
    finally:
        pilot.close()


def _verified_manifest(directory):
    directory = _checked_path(directory)
    if not directory.is_dir() or {p.name for p in directory.iterdir()} != {"pilot.sqlite", "manifest.json"}:
        raise Rejected("Incomplete or unsupported pilot journal backup")
    manifest_path = _checked_path(directory / "manifest.json")
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise Rejected("Invalid backup manifest")
    try:
        with manifest_path.open("rb") as stream:
            raw = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise Rejected("Backup manifest exceeds budget")
        manifest = json.loads(raw)
        if canonical(manifest) != raw:
            raise Rejected("Noncanonical backup manifest")
        database = directory / "pilot.sqlite"
        sha, size = file_digest(database)
        # Audit a disposable copy so verification cannot create SQLite sidecars in,
        # or recover a hot journal from, the received bundle directory.
        with tempfile.TemporaryDirectory(prefix="orchd-pilot-verify-") as scratch:
            copied = Path(scratch) / "pilot.sqlite"
            _copy_database(database, copied)
            if file_digest(copied) != (sha, size):
                raise Rejected("Pilot backup changed during verification")
            connection = sqlite3.connect(copied.as_uri() + "?mode=ro", uri=True)
            try:
                report = audit(connection)
            finally:
                connection.close()
        binding = {"schema_version": "1.0.0", "database_sha256": sha, "database_bytes": size, "audit": report}
        if canonical(manifest) != canonical({"kind": "PilotJournalBackup", "binding": binding, "sha256": digest(binding)}):
            raise Rejected("Backup content does not match manifest")
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError, sqlite3.Error) as exc:
        raise Rejected("Invalid pilot journal backup") from exc
    return manifest


def _refuses_execution(directory):
    """A snapshot names its original journal root, so it can never be run in place.

    Opening a bundle read-only keeps it byte-identical; opening one for writing would
    leave SQLite sidecars behind, and verification refuses a directory holding them.
    """
    report = _verified_manifest(directory)
    records = report["binding"]["audit"]["binding"]["records"]
    if not records:
        return True
    pilot = Pilot(directory, read_only=True)
    try:
        pilot.inspect(records[0]["id"])
    except Rejected:
        return True
    finally:
        pilot.close()
    return False


def verify_journal_backup(directory, expected_sha256=None):
    manifest = _verified_manifest(directory)
    if expected_sha256 is not None and manifest["sha256"] != expected_sha256:
        raise Rejected("Unexpected pilot backup digest")
    if not _refuses_execution(directory):
        raise Rejected("A snapshot that accepts its own scopes is not a snapshot")
    report = manifest["binding"]["audit"]
    return {"kind": "PilotJournalBackupVerification", "status": "valid", "sha256": manifest["sha256"],
            "audit_sha256": report["sha256"], "record_count": report["binding"]["record_count"],
            "live_pilots": report["binding"]["live_pilots"],
            "journal_status": report["binding"]["succession"]["status"],
            "workspaces_included": False, **FLAGS}


def compare_journal_backup(root, directory, expected_sha256):
    """Compare a verified snapshot with the live journal. Matching never authorizes restore."""
    manifest = _verified_manifest(directory)
    if manifest["sha256"] != expected_sha256:
        raise Rejected("Unexpected pilot backup digest")
    current = audit_journal(root)
    saved = manifest["binding"]["audit"]
    live = {row["id"]: row for row in current["binding"]["records"]}
    backup = {row["id"]: row for row in saved["binding"]["records"]}
    differences = []
    for identifier in sorted(live.keys() | backup.keys()):
        here, there = live.get(identifier), backup.get(identifier)
        reasons = []
        if there is None:
            reasons.append("missing_from_backup")
        elif here is None:
            reasons.append("backup_only_pilot")
        else:
            if here["scope_sha256"] != there["scope_sha256"]:
                reasons.append("scope_mismatch")
            if here["status"] != there["status"] or here["revision"] != there["revision"]:
                reasons.append("lifecycle_advanced")
            if here["receipt_sha256"] != there["receipt_sha256"]:
                reasons.append("receipt_mismatch")
            if here["worker_journal_sha256"] != there["worker_journal_sha256"]:
                reasons.append("worker_journal_mismatch")
            if here["reclamation_sha256"] != there["reclamation_sha256"]:
                reasons.append("reclamation_mismatch")
        if reasons:
            differences.append({"id": identifier, "reasons": reasons})
    succession_changed = saved["binding"].get("succession") != current["binding"]["succession"]
    binding = {"schema_version": "1.0.0", "backup_sha256": manifest["sha256"],
               "backup_audit_sha256": saved["sha256"], "journal_audit_sha256": current["sha256"],
               "succession_changed": succession_changed,
               "backup_succession": saved["binding"].get("succession"),
               "journal_succession": current["binding"]["succession"],
               "differences": differences}
    return {"kind": "PilotJournalBackupComparison", "binding": binding, "sha256": digest(binding),
            "status": "different" if differences or succession_changed else "matches", **FLAGS}


def backup_journal(root, destination):
    """Create a new bundle. The manifest is the completion marker and is written last."""
    destination = _checked_path(destination)
    root = real_path(root)
    if destination == root or root in destination.parents:
        raise Rejected("A pilot snapshot must be created outside its own journal")
    if destination.exists() or destination.is_symlink():
        raise Rejected("Use a new snapshot directory; existing evidence is never reused or overwritten")
    pilot = Pilot(root, read_only=True)
    try:
        audit(pilot.db, pilot.root)  # Reject an invalid source before creating any output.
        pilot.db.execute("BEGIN")
        pages = pilot.db.execute("PRAGMA page_count").fetchone()[0]
        page_size = pilot.db.execute("PRAGMA page_size").fetchone()[0]
        if pages * page_size > MAX_DATABASE_BYTES:
            raise Rejected("Pilot journal exceeds backup budget")
        destination.mkdir()  # Exclusive: never reuse even an empty existing directory.
        database = destination / "pilot.sqlite"
        deadline = time.monotonic() + 30

        def progress(status, remaining, total):
            if time.monotonic() > deadline:
                raise Rejected("Pilot journal backup timed out")

        target = sqlite3.connect(database)
        try:
            pilot.db.backup(target, pages=128, progress=progress, sleep=0.05)
            target.execute("PRAGMA journal_mode=DELETE")
        finally:
            target.close()
        pilot.db.execute("COMMIT")
    finally:
        pilot.close()
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        report = audit(connection)
    finally:
        connection.close()
    with database.open("r+b") as stream:
        os.fsync(stream.fileno())
    sha, size = file_digest(database)
    binding = {"schema_version": "1.0.0", "database_sha256": sha, "database_bytes": size, "audit": report}
    manifest = {"kind": "PilotJournalBackup", "binding": binding, "sha256": digest(binding)}
    encoded = canonical(manifest)
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise Rejected("Backup manifest exceeds budget")
    with (destination / "manifest.json").open("xb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    return {"kind": "PilotJournalBackupVerification", "status": "valid", "sha256": manifest["sha256"],
            "audit_sha256": report["sha256"], "record_count": report["binding"]["record_count"],
            "live_pilots": report["binding"]["live_pilots"], "created_at": now(),
            "journal_status": report["binding"]["succession"]["status"],
            "workspaces_included": False, **FLAGS}
