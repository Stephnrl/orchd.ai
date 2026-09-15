"""Verified journal snapshot bundles. No restore, replacement or dispatch authority."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import shutil
import tempfile
import time

from .contracts import Rejected, canonical, digest
from .github_journal import GitHubJournal
from . import journal_succession

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


def _verified_manifest(directory):
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
    return manifest


def verify_journal_backup(directory):
    manifest = _verified_manifest(directory)
    audit = manifest['binding']['audit']
    return {"kind": "GitHubJournalBackupVerification", "status": "valid", "sha256": manifest['sha256'],
            "audit_sha256": audit['sha256'], "record_count": audit['binding']['record_count'],
            "restore_allowed": False, "retry_allowed": False, "live_authorized": False}


def compare_journal_backup(source, directory, expected_sha256):
    """Compare verified retained evidence; matching snapshots never authorize restore."""
    manifest = _verified_manifest(directory)
    if manifest['sha256'] != expected_sha256:
        raise Rejected("Unexpected journal backup digest")
    journal = GitHubJournal(source, read_only=True)
    try:
        current = journal.audit()
        # Bound to this journal: a succession record copied here from another names the
        # wrong file and is refused rather than compared.
        live_succession = journal_succession.summary(journal.db, journal.path)
    finally:
        journal.close()
    copy = GitHubJournal(Path(directory) / 'journal.sqlite', read_only=True)
    try:
        # The bundle's record legitimately names the journal it was copied from.
        saved_succession = journal_succession.summary(copy.db)
    finally:
        copy.close()
    saved = manifest['binding']['audit']
    current_rows = {row['operation_id']: row for row in current['binding']['records']}
    saved_rows = {row['operation_id']: row for row in saved['binding']['records']}
    differences = []
    for operation in sorted(current_rows.keys() | saved_rows.keys()):
        live, backup = current_rows.get(operation), saved_rows.get(operation)
        reasons = []
        if backup is None:
            reasons.append('missing_from_backup')
        elif live is None:
            reasons.append('backup_only_operation')
        else:
            if live['task_id'] != backup['task_id'] or live['sha256'] != backup['sha256']:
                reasons.append('scope_mismatch')
            if live['state'] != backup['state'] or live['reserved_at'] != backup['reserved_at']:
                reasons.append('reservation_mismatch')
        if reasons:
            differences.append({"operation_id": operation, "reasons": reasons})
    # Retirement changes no record, so a snapshot taken before it still matches row for
    # row. Comparing succession as well keeps a stale bundle from claiming to describe a
    # journal that has since closed to new admissions.
    changed = live_succession != saved_succession
    binding = {"schema_version": "1.0.0", "backup_sha256": manifest['sha256'],
               "backup_audit_sha256": saved['sha256'], "journal_audit_sha256": current['sha256'],
               "differences": differences, "succession_changed": changed,
               "backup_succession": saved_succession, "journal_succession": live_succession}
    return {"kind": "GitHubJournalBackupComparison", "binding": binding, "sha256": digest(binding),
            "status": "different" if differences or changed else "matches", "restore_allowed": False,
            "retry_allowed": False, "live_authorized": False}


def drill_journal_backup(directory, expected_sha256):
    """Exercise reservation recovery on a disposable copy, never the active journal."""
    manifest = _verified_manifest(directory)
    if manifest['sha256'] != expected_sha256:
        raise Rejected("Unexpected journal backup digest")
    prepared = uncertain = 0
    with tempfile.TemporaryDirectory(prefix='orchd-journal-drill-') as scratch:
        database = Path(scratch) / 'journal.sqlite'
        shutil.copyfile(Path(directory) / 'journal.sqlite', database)
        sha, size = file_digest(database)
        if sha != manifest['binding']['database_sha256'] or size != manifest['binding']['database_bytes']:
            raise Rejected("Backup changed before drill copy")
        journal = GitHubJournal(database)
        try:
            original = journal.audit()
            if canonical(original) != canonical(manifest['binding']['audit']):
                raise Rejected("Drill copy differs from verified audit")
            deadline = time.monotonic() + 30
            for record in original['binding']['records']:
                if time.monotonic() > deadline:
                    raise Rejected("Journal recovery drill timed out")
                operation, scope = record['operation_id'], record['sha256']
                if record['state'] == 'prepared':
                    reserved = journal.reserve(operation, scope)
                    if reserved['state'] != 'uncertain' or reserved['sha256'] != scope:
                        raise Rejected("Drill reservation failed")
                    prepared += 1
                else:
                    uncertain += 1
                try:
                    journal.reserve(operation, scope)
                except Rejected:
                    pass
                else:
                    raise Rejected("Drill allowed a consumed reservation")
            final = journal.audit()
            if any(row['state'] != 'uncertain' for row in final['binding']['records']):
                raise Rejected("Drill did not retain consumed reservations")
        finally:
            journal.close()
        reopened = GitHubJournal(database, read_only=True)
        try:
            if canonical(reopened.audit()) != canonical(final):
                raise Rejected("Drill reservations did not survive reopening")
        finally:
            reopened.close()
    binding = {"schema_version": "1.0.0", "backup_sha256": manifest['sha256'],
               "audit_sha256": original['sha256'], "prepared_exercised": prepared,
               "existing_uncertain_checked": uncertain, "repeat_reservations_rejected": prepared + uncertain}
    return {"kind": "GitHubJournalRecoveryDrill", "binding": binding, "sha256": digest(binding),
            "status": "passed", "restore_allowed": False, "retry_allowed": False, "live_authorized": False}


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
