"""Conservative offline lifecycle and verified backup/restore operations."""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time

from .contracts import Rejected, canonical, validate
from .storage import Store


def plain(path):
    for component in (path, *path.parents):
        if component.is_symlink() or (hasattr(component, "is_junction") and component.is_junction()):
            raise Rejected("Maintenance refuses links/junctions")
    return path


def copy_bytes(source, target):
    plain(source)
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        while block := incoming.read(1024 * 1024):
            outgoing.write(block)
        outgoing.flush()
        os.fsync(outgoing.fileno())


def audit(store):
    if store.db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or store.db.execute("PRAGMA foreign_key_check").fetchall():
        raise Rejected("Database integrity failure")
    for row in store.db.execute("SELECT payload FROM records"):
        validate(json.loads(row[0]))
    for row in store.db.execute("SELECT task_id,payload FROM artifacts"):
        store.read_artifact(json.loads(row["payload"]), row["task_id"])
    for row in store.db.execute("SELECT id FROM tasks"):
        replay = store.reconstruct(row["id"])
        state, context = store.task(row["id"])
        if not replay or replay["state"] != state or replay["context"] != context:
            raise Rejected("Projection does not match event history")


def backup(store, destination):
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink() or store.root == destination or store.root in destination.parents:
        raise Rejected("Backup needs a new destination outside live data")
    plain(destination.parent)
    with store.exclusive():
        audit(store)
        destination.mkdir()
        artifacts = destination / "artifacts"
        artifacts.mkdir()
        connection = sqlite3.connect(destination / "orch.sqlite")
        try:
            store.db.backup(connection)
        finally:
            connection.close()
        hashes = sorted({json.loads(row[0])["sha256"] for row in store.db.execute("SELECT payload FROM artifacts")})
        for sha in hashes:
            copy_bytes(store.root / "artifacts" / sha, artifacts / sha)
        # Journals are not authoritative workflow records; unresolved restored operations
        # stay BLOCKED. Do not copy live locks or pretend a new host owns old processes.
        manifest = {"version": 1, "database_sha256": hashlib.sha256((destination / "orch.sqlite").read_bytes()).hexdigest(), "artifacts": hashes}
        (destination / "manifest.json").write_bytes(canonical(manifest))
        return manifest


def restore(source, destination):
    source, destination = Path(source).absolute(), Path(destination).absolute()
    plain(source)
    if destination.exists() or destination.is_symlink() or destination == source or source in destination.parents:
        raise Rejected("Restore requires a new destination outside backup")
    plain(destination.parent)
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


def collect_orphans(store, grace_seconds=86400):
    if grace_seconds < 3600:
        raise Rejected("Orphan grace must be at least one hour")
    with store.exclusive():
        root = plain(store.root / "artifacts")
        retained = {json.loads(r[0])["sha256"] for r in store.db.execute("SELECT payload FROM artifacts")}
        removed = []
        for path in root.iterdir():
            plain(path)
            if not path.is_file() or not re.fullmatch(r"[a-f0-9]{64}(?:\.[a-f0-9]{32})?", path.name):
                continue
            if path.name in retained or time.time() - path.stat().st_mtime < grace_seconds:
                continue
            # Exact files only; never recurse or remove referenced audit evidence.
            path.unlink()
            removed.append(path.name)
        return {"removed": removed}
