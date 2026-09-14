"""Verified journal snapshot bundles. No restore, replacement or dispatch authority."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from .contracts import Rejected, canonical, digest
from .github_journal import GitHubJournal

MAX_DATABASE_BYTES = 32 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024


def file_digest(path):
    if path.is_symlink() or not path.is_file():
        raise Rejected("Backup database must be a regular file")
    sha = hashlib.sha256()
    size = 0
    with path.open('rb') as stream:
        while chunk := stream.read(65536):
            size += len(chunk)
            if size > MAX_DATABASE_BYTES:
                raise Rejected("Backup database exceeds budget")
            sha.update(chunk)
    return sha.hexdigest(), size


def verify_journal_backup(directory):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir() or {p.name for p in directory.iterdir()} != {'journal.sqlite', 'manifest.json'}:
        raise Rejected("Incomplete or unsupported journal backup")
    manifest_path = directory / 'manifest.json'
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise Rejected("Invalid backup manifest")
    try:
        with manifest_path.open('rb') as stream:
            raw = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise Rejected("Backup manifest exceeds budget")
        manifest = json.loads(raw)
        if canonical(manifest) != raw:
            raise Rejected("Noncanonical backup manifest")
        database = directory / 'journal.sqlite'
        sha, size = file_digest(database)
        journal = GitHubJournal(database, read_only=True)
        try:
            audit = journal.audit()
        finally:
            journal.close()
        binding = {"schema_version": "1.0.0", "database_sha256": sha, "database_bytes": size, "audit": audit}
        expected = {"kind": "GitHubJournalBackup", "binding": binding, "sha256": digest(binding)}
        if canonical(manifest) != canonical(expected):
            raise Rejected("Backup content does not match manifest")
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise Rejected("Invalid journal backup") from exc
    return {"kind": "GitHubJournalBackupVerification", "status": "valid", "sha256": manifest['sha256'],
            "audit_sha256": audit['sha256'], "record_count": audit['binding']['record_count'],
            "restore_allowed": False, "retry_allowed": False, "live_authorized": False}


def backup_journal(source, destination):
    """Create a new bundle; manifest is the completion marker, written last."""
    journal = GitHubJournal(source, read_only=True)
    destination = Path(destination)
    try:
        journal.audit()  # Reject invalid sources before creating output.
        journal.db.execute('BEGIN')
        journal.usage()  # Establish the snapshot before measuring/copying pages.
        pages = journal.db.execute('PRAGMA page_count').fetchone()[0]
        page_size = journal.db.execute('PRAGMA page_size').fetchone()[0]
        if pages * page_size > MAX_DATABASE_BYTES:
            raise Rejected("Journal database exceeds backup budget")
        destination.mkdir()  # Exclusive: never reuse even an empty existing directory.
        database = destination / 'journal.sqlite'
        deadline = time.monotonic() + 30

        def progress(status, remaining, total):
            if time.monotonic() > deadline:
                raise Rejected("Journal backup timed out")

        target = sqlite3.connect(database)
        try:
            journal.db.backup(target, pages=128, progress=progress, sleep=0.05)
            target.execute('PRAGMA journal_mode=DELETE')
        finally:
            target.close()
        journal.db.execute('COMMIT')
        copy = GitHubJournal(database, read_only=True)
        try:
            audit = copy.audit()
        finally:
            copy.close()
        with database.open('r+b') as stream:
            os.fsync(stream.fileno())
        sha, size = file_digest(database)
        binding = {"schema_version": "1.0.0", "database_sha256": sha, "database_bytes": size, "audit": audit}
        manifest = {"kind": "GitHubJournalBackup", "binding": binding, "sha256": digest(binding)}
        encoded = canonical(manifest)
        if len(encoded) > MAX_MANIFEST_BYTES:
            raise Rejected("Backup manifest exceeds budget")
        with (destination / 'manifest.json').open('xb') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        return verify_journal_backup(destination)
    finally:
        if journal.db.in_transaction:
            journal.db.execute('ROLLBACK')
        journal.close()
