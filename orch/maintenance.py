"""Conservative offline lifecycle and verified backup/restore operations."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time

from .contracts import Rejected, canonical, digest, validate, now
from .broker import separate, validate_wire
from .storage import Store, AUDIT_RESERVE_BYTES


def plain(path):
    for component in (path, *path.parents):
        if component.is_symlink() or (hasattr(component, "is_junction") and component.is_junction()):
            raise Rejected("Maintenance refuses links/junctions")
    return path


def real_path(path):
    """Canonical absolute path, with links refused before and after resolution.

    Windows can spell the same directory in 8.3 short form; Git and SQLite report the
    long one. Comparing canonical paths keeps those spellings from looking different.
    """
    return plain(plain(Path(path).absolute()).resolve())


def copy_bytes(source, target):
    plain(source)
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        while block := incoming.read(1024 * 1024):
            outgoing.write(block)
        outgoing.flush()
        os.fsync(outgoing.fileno())


def audit_provenance(store):
    """Check persisted bindings without consulting journals or the current runtime."""
    for row in store.db.execute("SELECT * FROM broker_requests"):
        request = json.loads(row["request"])
        validate_wire(request, "Request")
        operation = store.db.execute("SELECT task_id,generation FROM operations WHERE id=?", (row["operation_id"],)).fetchone()
        if (not operation or request["operation_id"] != row["operation_id"]
                or request["generation"] != row["generation"]
                or request["task_id"] != operation["task_id"]
                or row["generation"] > operation["generation"]):
            raise Rejected("Broker request binding mismatch")
    for row in store.db.execute("SELECT * FROM execution_provenance"):
        saved = store.db.execute("SELECT request FROM broker_requests WHERE operation_id=? AND generation=?",
                                 (row["operation_id"], row["generation"])).fetchone()
        if not saved:
            raise Rejected("Execution provenance has no durable request")
        request = json.loads(saved["request"])
        reference = json.loads(row["artifact"])
        envelope = json.loads(store.read_artifact(reference, request["task_id"]))
        validate_wire(envelope, "Envelope")
        if (reference["producer_id"] != "action_broker" or reference["trust"] != "trusted_receipt"
                or digest(envelope) != row["envelope_sha256"]
                or envelope["request_sha256"] != digest(request)):
            raise Rejected("Execution provenance digest or producer mismatch")
        for key in ("schema_version", "operation_id", "task_id", "generation", "nonce", "source_digest"):
            if envelope[key] != request[key]:
                raise Rejected("Execution provenance binding mismatch")
    # Legacy databases gain this table on their next writable open; read-only copies may lack it.
    if store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='broker_identities'").fetchone():
        for row in store.db.execute("SELECT * FROM broker_identities"):
            record = json.loads(row["record"])
            validate_wire(record, "ExecutionIdentity")
            provenance = store.db.execute("SELECT 1 FROM execution_provenance WHERE operation_id=? AND generation=?",
                                          (row["operation_id"], row["generation"])).fetchone()
            if not provenance or digest(record) != row["sha256"]:
                raise Rejected("Broker identity record has no provenance or a digest mismatch")
            if record["separate_identity"] != separate(record["broker"], record["orchestrator"]):
                raise Rejected("Broker identity claim does not follow from its accounts")


def audit(store):
    if store.db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or store.db.execute("PRAGMA foreign_key_check").fetchall():
        raise Rejected("Database integrity failure")
    for row in store.db.execute("SELECT payload FROM records"):
        validate(json.loads(row[0]))
    for row in store.db.execute("SELECT task_id,payload FROM artifacts"):
        store.read_artifact(json.loads(row["payload"]), row["task_id"])
    audit_provenance(store)
    for row in store.db.execute("SELECT id FROM tasks"):
        replay = store.reconstruct(row["id"])
        state, context = store.task(row["id"])
        if not replay or replay["state"] != state or replay["context"] != context:
            raise Rejected("Projection does not match event history")


def integrity_report(store):
    """Read-only integrity evidence; busy dispatchers reject rather than give a verdict."""
    with store.exclusive():
        store.quiescent()
        report = {"version": 1, "generated_at": now(), "status": "failed", "reason": None,
                  "tasks": None, "records": None, "artifact_references": None}
        try:
            plain(store.root / "artifacts")
            audit(store)
            report.update(status="passed", tasks=store.db.execute("SELECT count(*) FROM tasks").fetchone()[0],
                          records=store.db.execute("SELECT count(*) FROM records").fetchone()[0],
                          artifact_references=store.db.execute("SELECT count(*) FROM artifacts").fetchone()[0])
        except (Rejected, OSError, sqlite3.DatabaseError, ValueError, TypeError, KeyError):
            report["reason"] = "database_artifact_or_replay_integrity_failure"
        return report


def backup(store, destination):
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink() or store.root == destination or store.root in destination.parents:
        raise Rejected("Backup needs a new destination outside live data")
    plain(destination.parent)
    with store.exclusive():
        store.quiescent()
        audit(store)
        with tempfile.TemporaryDirectory(prefix=".orch-backup-", dir=destination.parent) as temporary:
            staged = Path(temporary)
            artifacts = staged / "artifacts"
            artifacts.mkdir()
            connection = sqlite3.connect(staged / "orch.sqlite")
            try:
                store.db.backup(connection)
            finally:
                connection.close()
            hashes = sorted({json.loads(row[0])["sha256"] for row in store.db.execute("SELECT payload FROM artifacts")})
            for sha in hashes:
                copy_bytes(store.root / "artifacts" / sha, artifacts / sha)
            copied = Store(staged, read_only=True)
            try:
                audit(copied)
                copied_hashes = {json.loads(row[0])["sha256"] for row in copied.db.execute("SELECT payload FROM artifacts")}
                if copied_hashes != set(hashes):
                    raise Rejected("Copied backup references changed")
            finally:
                copied.close()
            # Only committed database records and artifacts are authoritative;
            # live broker locks/journals and workspace ownership are not copied.
            manifest = {"version": 1, "database_sha256": hashlib.sha256((staged / "orch.sqlite").read_bytes()).hexdigest(), "artifacts": hashes}
            with (staged / "manifest.json").open("xb") as stream:
                stream.write(canonical(manifest))
                stream.flush()
                os.fsync(stream.fileno())
            plain(destination.parent)
            if destination.exists() or destination.is_symlink():
                raise Rejected("Backup destination appeared during verification")
            staged.rename(destination)
        return manifest


def backup_manifest(source):
    """Validate the manifest and saved bytes shared by verification and restore."""
    source = Path(source).absolute()
    plain(source)
    manifest_file = plain(source / "manifest.json")
    if manifest_file.stat().st_size > 8 * 1024 * 1024:
        raise Rejected("Manifest too large")
    manifest = json.loads(manifest_file.read_text())
    if not isinstance(manifest, dict) or set(manifest) != {"version", "database_sha256", "artifacts"} or type(manifest["version"]) is not int or manifest["version"] != 1:
        raise Rejected("Unsupported backup manifest")
    if not isinstance(manifest["database_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", manifest["database_sha256"]):
        raise Rejected("Invalid database digest")
    hashes = manifest["artifacts"]
    if not isinstance(hashes, list) or any(not isinstance(x, str) or not re.fullmatch(r"[a-f0-9]{64}", x) for x in hashes) or len(hashes) != len(set(hashes)):
        raise Rejected("Invalid artifact manifest")
    db_path = plain(source / "orch.sqlite")
    if hashlib.sha256(db_path.read_bytes()).hexdigest() != manifest["database_sha256"]:
        raise Rejected("Backup database digest mismatch")
    plain(source / "artifacts")
    for sha in hashes:
        if hashlib.sha256(plain(source / "artifacts" / sha).read_bytes()).hexdigest() != sha:
            raise Rejected("Backup artifact digest mismatch")
    return manifest


def verify_backup(source):
    """Report saved-backup consistency without restoring or repairing it."""
    report = {"version": 1, "generated_at": now(), "status": "failed", "reason": None,
              "artifacts": None, "tasks": None}
    try:
        source = Path(source).absolute()
        manifest = backup_manifest(source)
        # Published backups are quiescent database snapshots, not live WAL stores.
        for suffix in ("-wal", "-shm", "-journal"):
            path = source / ("orch.sqlite" + suffix)
            plain(path)
            if path.exists() and (not path.is_file() or (suffix != "-shm" and path.stat().st_size)):
                raise Rejected("Backup has live database sidecars")
        store = Store(source, read_only=True, immutable=True)
        try:
            referenced = {json.loads(row[0])["sha256"] for row in store.db.execute("SELECT payload FROM artifacts")}
            if referenced != set(manifest["artifacts"]):
                raise Rejected("Artifact manifest does not match database")
            audit(store)
            tasks = store.db.execute("SELECT count(*) FROM tasks").fetchone()[0]
        finally:
            store.close()
        if backup_manifest(source) != manifest:
            raise Rejected("Backup changed during verification")
        report.update(status="passed", tasks=tasks, artifacts=len(referenced))
    except (Rejected, OSError, sqlite3.DatabaseError, ValueError, TypeError, KeyError):
        report["reason"] = "backup_integrity_failure"
    return report


def restore(source, destination):
    source, destination = Path(source).absolute(), Path(destination).absolute()
    plain(source)
    if destination.exists() or destination.is_symlink() or destination == source or source in destination.parents:
        raise Rejected("Restore requires a new destination outside backup")
    plain(destination.parent)
    manifest = backup_manifest(source)
    hashes = manifest["artifacts"]
    db_path = source / "orch.sqlite"
    # A failed copy or audit must never publish a runnable destination. Stage on
    # the same filesystem so the final directory rename publishes the whole copy.
    with tempfile.TemporaryDirectory(prefix=".orch-restore-", dir=destination.parent) as temporary:
        staged = Path(temporary)
        (staged / "artifacts").mkdir()
        copy_bytes(db_path, staged / "orch.sqlite")
        if hashlib.sha256((staged / "orch.sqlite").read_bytes()).hexdigest() != manifest["database_sha256"]:
            raise Rejected("Copied database digest mismatch")
        for sha in hashes:
            copy_bytes(source / "artifacts" / sha, staged / "artifacts" / sha)
            if hashlib.sha256((staged / "artifacts" / sha).read_bytes()).hexdigest() != sha:
                raise Rejected("Copied artifact digest mismatch")
        recovered = Store(staged)
        try:
            referenced = {json.loads(row[0])["sha256"] for row in recovered.db.execute("SELECT payload FROM artifacts")}
            if referenced != set(hashes):
                raise Rejected("Artifact manifest does not match database")
            audit(recovered)
        finally:
            recovered.close()
        plain(destination.parent)
        if destination.exists() or destination.is_symlink():
            raise Rejected("Restore destination appeared during verification")
        staged.rename(destination)
    return {"restored": str(destination), "artifacts": len(hashes)}


def storage_usage(store, task_id=None):
    """Report quota accounting and orphan candidates without changing workflow data."""
    with store.exclusive():
        store.quiescent()
        if task_id is not None:
            store.task(task_id)
        rows = store.db.execute("SELECT task_id,payload FROM artifacts").fetchall()
        referenced = {}
        logical_bytes = references = 0
        for row in rows:
            item = json.loads(row["payload"])
            previous = referenced.setdefault(item["sha256"], item["size_bytes"])
            if previous != item["size_bytes"]:
                raise Rejected("Conflicting artifact sizes")
            if task_id is None or row["task_id"] == task_id:
                references += 1
                logical_bytes += item["size_bytes"]
        physical = orphan_bytes = orphan_count = eligible_bytes = eligible_count = unexpected = 0
        present = {}
        scanned_at = time.time()
        for path in plain(store.root / "artifacts").iterdir():
            plain(path)
            if not path.is_file():
                unexpected += 1
                continue
            stat = path.stat()
            physical += stat.st_size
            if path.name in referenced:
                present[path.name] = stat.st_size
            elif re.fullmatch(r"[a-f0-9]{64}(?:\.[a-f0-9]{32})?", path.name):
                orphan_count += 1
                orphan_bytes += stat.st_size
                if scanned_at - stat.st_mtime >= 86400:
                    eligible_count += 1
                    eligible_bytes += stat.st_size
            else:
                unexpected += 1
        database_bytes = {}
        for name in ("orch.sqlite", "orch.sqlite-wal", "orch.sqlite-shm"):
            path = plain(store.root / name)
            database_bytes[name] = path.stat().st_size if path.is_file() else 0
        return {"version": 1, "generated_at": now(), "task_id": task_id,
                "references": references, "logical_bytes": logical_bytes,
                "task_quota_bytes": store.task_quota,
                "task_headroom_bytes": max(0, store.task_quota - logical_bytes) if task_id is not None else None,
                "disk_quota_bytes": store.disk_quota, "physical_artifact_bytes": physical,
                "disk_headroom_bytes": max(0, store.disk_quota - physical),
                "audit_reserve_bytes": AUDIT_RESERVE_BYTES,
                "orphan_files": orphan_count, "orphan_bytes": orphan_bytes,
                "gc_eligible_files": eligible_count, "gc_eligible_bytes": eligible_bytes,
                "gc_grace_seconds": 86400, "unexpected_entries": unexpected,
                "missing_referenced_files": len(set(referenced) - set(present)),
                "size_mismatches": sum(size != referenced[name] for name, size in present.items()),
                "database_file_bytes": database_bytes}


def collect_orphans(store, grace_seconds=86400, dry_run=False):
    if type(dry_run) is not bool:
        raise Rejected("Invalid GC dry-run flag")
    if type(grace_seconds) not in (int, float) or (type(grace_seconds) is float and not math.isfinite(grace_seconds)) or grace_seconds < 3600:
        raise Rejected("Orphan grace must be finite and at least one hour")
    with store.exclusive():
        store.quiescent()
        root = plain(store.root / "artifacts")
        retained = {json.loads(r[0])["sha256"] for r in store.db.execute("SELECT payload FROM artifacts")}
        removed = []
        candidates = []
        eligible = []
        scanned_at = time.time()
        # Finish all scan/path checks before the first unlink. A later invalid
        # entry must not cause a partially executed cleanup during validation.
        for path in sorted(root.iterdir()):
            plain(path)
            if not path.is_file() or not re.fullmatch(r"[a-f0-9]{64}(?:\.[a-f0-9]{32})?", path.name):
                continue
            stat = path.stat()
            if path.name in retained or scanned_at - stat.st_mtime < grace_seconds:
                continue
            candidates.append({"name": path.name, "size_bytes": stat.st_size})
            eligible.append(path)
        if not dry_run:
            for path in eligible:
                # Exact files only; never recurse. Recheck paths immediately
                # before deletion as well as during preflight.
                plain(path)
                path.unlink()
                removed.append(path.name)
        return {"dry_run": dry_run, "candidates": candidates, "removed": removed,
                "grace_seconds": grace_seconds}
