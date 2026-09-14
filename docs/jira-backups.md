# Jira journal backups and recovery drills

The [Jira operation journal](jira-journal.md) now supports verified snapshot bundles,
comparison with retained state and recovery drills on disposable copies. These are
offline diagnostics. They never restore over an active database or authorize posting.

## Create a snapshot

```sh
python -m orch jira-journal-backup --journal .runtime/jira-journal.sqlite --destination .runtime/jira-backup-001
```

The destination must not exist, and its parent must already exist. A source integrity
audit and database size check run before creating output. SQLite's backup API copies a
pinned read snapshot, including committed WAL state. A concurrent reservation may occur
after that snapshot: the bundle remains internally consistent but can already be stale.
Creation never reserves an operation or modifies source records.

The bundle contains exactly two files:

| File | Contents |
| --- | --- |
| `journal.sqlite` | Standalone Jira database, capped at 32 MiB |
| `manifest.json` | Canonical `JiraJournalBackup`, capped at 1 MiB |

The manifest binds schema version, database SHA-256 and byte length, and the full journal
audit report. Its `sha256` is the digest of that binding, **not** a whole-directory or
manifest-file hash. The successful command returns this digest in a
`JiraJournalBackupVerification` report. Retain it separately for later review.

The database is audited and flushed before the manifest is written last and flushed.
Interrupted or timed-out creation can leave an incomplete directory. It must not be used
or reused as a new destination. Verification requires both files and their exact content;
manifest presence alone is not sufficient. Copy progress has a 30-second deadline.
File flushing and process tests do not establish power-loss durability on every host.

## Verify a received bundle

```sh
python -m orch jira-verify-journal-backup --destination .runtime/jira-backup-001 --expected-sha256 BACKUP_SHA256
```

CLI verification requires an expected backup digest. It rejects noncanonical JSON,
duplicate keys, extra manifest fields, unexpected files (including SQLite sidecars),
wrong database identity, inconsistent hashes, malformed records and exceeded budgets.
Symlinks and Windows reparse points in input/output paths are rejected before opening.

Verification reads and hashes the database, makes a bounded temporary copy, checks the
copy's digest, and audits that copy. SQLite does not open the received database itself,
so verification does not create sidecars or recover transactions inside the bundle.
The temporary directory is removed on success or failure. The Python verification API
can omit the expected digest for local integrity inspection; the CLI always requires it.
Neither digest matching nor schema validation authenticates who created the bundle.

## Compare against retained state

```sh
python -m orch jira-compare-journal-backup --journal .runtime/jira-journal.sqlite --destination .runtime/jira-backup-001 --expected-sha256 BACKUP_SHA256
```

Comparison verifies the bundle and audits the active journal in read-only mode. It
compares task/scope digests, state and reservation timestamps by operation ID. Results
contain metadata-only differences with these fixed reasons:

| Reason | Meaning |
| --- | --- |
| `missing_from_backup` | Active operation is absent from the bundle |
| `backup_only_operation` | Bundle operation is absent from the active journal |
| `scope_mismatch` | Same operation has a different task or retained scope digest |
| `reservation_mismatch` | State or reservation timestamp differs |

An old `prepared` snapshot compared with a currently `uncertain` operation produces a
reservation mismatch. Replacing the current database with that snapshot could reopen a
consumed slot. Even `matches` is only a comparison of observed local snapshots, not a lock
against subsequent changes and not permission to replace the journal.

## Exercise recovery on a disposable copy

```sh
python -m orch jira-journal-recovery-drill --destination .runtime/jira-backup-001 --expected-sha256 BACKUP_SHA256
```

The drill accepts no active `--journal`. It verifies the bundle, copies its database into
a separate temporary directory and rechecks the exact database digest and audit. There it:

1. Consumes each prepared reservation once and rejects a second attempt.
2. Rejects another reservation for every previously uncertain operation.
3. Confirms all scopes and previously consumed reservation timestamps are preserved.
4. Reopens the disposable database, checks persistence and reconstructs each recovery plan.

The temporary copy is deleted afterward, including on failure. The input bundle and active
journal are unchanged. The report binds the backup/audit digests and counts prepared slots
exercised, existing uncertain records checked, repeat reservations rejected and recovery
plans reconstructed. Record exercises have a 30-second deadline; verification and copying
are separately bounded by size. A passed drill proves only those local checks, not that
a Jira comment was sent or that live recovery is approved.

All commands return `restore_allowed`, `retry_allowed`, `live_authorized` and
`remote_effect_confirmed` as false. Valid verification, matching comparison and passed
drills exit 0. A differing comparison exits 2 with JSON; invalid inputs or failed checks
exit 2 without partial stdout. Verification and drills reject active-journal arguments;
all backup commands reject replacement scope inputs.

Bundles contain the journal's original **unredacted issue responses and proposed comment
text**. Temporary verification/drill copies contain the same confidential data. This
module does not encrypt or securely erase files. Use approved host storage and temporary
storage access controls. Reports omit raw text, but the bundle is not a sanitized export.
Path checks assume a trusted local directory and cannot defeat privileged filesystem races.

Actual restoration, monotonic recovery admission, retention policy, independent security
review and live credentials remain separate work. Keep the active reservation history;
never treat a successful drill as permission to overwrite it with an older backup.
