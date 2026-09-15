# Pilot journal audits and verified snapshots

The pilot journal holds the only evidence that local work actually ran: retained scopes,
receipts, worker journals and reclamation records. Standard workflow backups deliberately
exclude it, and it had no verified snapshot of its own, unlike the
[GitHub](github-journal.md) and [Jira](jira-backups.md) journals. These four read-only
commands close that gap. None of them restores over an active journal, retries work or
grants execution.

```text
python -m orch pilot-journal-audit --data JOURNAL
python -m orch pilot-journal-backup --data JOURNAL --destination .runtime/pilot-backup-001
python -m orch pilot-verify-journal-backup --destination .runtime/pilot-backup-001 --expected-sha256 BACKUP_SHA256
python -m orch pilot-compare-journal-backup --data JOURNAL --destination .runtime/pilot-backup-001 --expected-sha256 BACKUP_SHA256
```

For task-bound pilots the journal is `STORE/repository-pilots`. None of these commands
needs Docker, an image or a daemon.

## The audit

`pilot-journal-audit` opens the journal read-only and checks every retained row: the
scope digest and its identifier, the lifecycle status against its permitted revisions,
whether a receipt is present exactly when the status requires one, the receipt's digest
and its binding to the scope's checks and file hashes, the worker journal's digest and
phase set, the receipt's binding to that worker journal, and each reclamation record's
digest. Worker or reclamation rows referencing no pilot are refused, as are scopes naming
more than one journal root.

The audit is deliberately **workspace-independent**. Workspaces are disposable, a receipt
binds their bytes by hash, and reclamation deletes them, so reading them would make the
audit depend on when it ran. This is what lets a live journal and a snapshot of it be
compared: both produce the same report for the same rows.

## Snapshots

`pilot-journal-backup` audits the source first, copies a pinned read snapshot with
SQLite's backup API, re-audits the copy, and writes the manifest last as the completion
marker. The destination must not exist, and it must lie outside the journal it snapshots.
A bundle contains exactly two files:

| File | Contents |
| --- | --- |
| `pilot.sqlite` | Standalone pilot database, capped at 32 MiB |
| `manifest.json` | Canonical `PilotJournalBackup`, capped at 4 MiB |

The manifest binds the schema version, the database SHA-256 and byte length, and the full
audit. Its `sha256` is the digest of that binding, not a whole-directory hash. Retain that
digest separately; verification and comparison both require it.

**Workspaces are never copied.** A snapshot preserves the evidence, not the disposable
files, and a reclaimed workspace cannot be restored from one. The verification report
says so in `workspaces_included: false`.

## A snapshot is not a journal

A retained scope names the journal root it was prepared in, so a snapshot placed anywhere
else refuses its own scopes. Verification proves this rather than asserting it: it opens
the bundle read-only and requires the first pilot to be refused. A bundle that accepted
its own scopes would not be a snapshot, and is rejected.

Open a bundle read-only. Opening one for writing leaves SQLite sidecar files behind, and
verification then refuses the directory because it no longer holds exactly the two
expected files.

## Comparison

`pilot-compare-journal-backup` verifies the bundle, audits the live journal and reports
per-pilot differences: `missing_from_backup`, `backup_only_pilot`, `scope_mismatch`,
`lifecycle_advanced`, `receipt_mismatch`, `worker_journal_mismatch` and
`reclamation_mismatch`. A difference exits 2. Differences are expected and are not
failures: preparing a pilot, advancing one, or reclaiming a workspace after the snapshot
all show up here. The command tells an operator what has changed since the snapshot; it
never reconciles, restores or retries.

## Boundaries

There is no restore command. Recreating a journal elsewhere would not recreate execution
authority, because scopes are bound to their original journal root and to the pilot's
live worker reservations. Snapshots do not extend the retained-row cap, so a journal that
reaches it still needs a new journal. Manifests detect accidental corruption, not
deliberate replacement by someone who can rewrite both the bundle and its retained digest,
and file flushing is not a power-loss durability claim on every host. `pilot_acceptance`
exercises the audit, snapshot, verification, matching comparison and drift detection, so
the offline release lane covers them.
