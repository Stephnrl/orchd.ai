# Verified backup publication

Backup creation now follows the same staged-publication approach as restore:

```sh
python -m orch backup --data .runtime/managed --destination .runtime/backup-001
```

Use [saved-backup verification](verify-backup.md) to check the published snapshot
without restoring it or selecting a destination.

The destination must be new, outside live data, under an existing operator-controlled
parent directory. The CLI requires an existing current-version source database and
opens it read-only. Missing sources are rejected without creating storage; legacy
sources are rejected without migration. Source paths and ancestors are checked for
links/junctions before opening SQLite. Upgrade legacy data through the normal
application path before requesting a backup.

The command takes the dispatcher lock, audits live state, then
creates a unique `.orch-backup-*` sibling staging directory. SQLite's backup API copies
the database and only referenced artifact blobs are copied.

Before publication, the copied database is opened read-only. Its integrity, contracts,
artifact content and task/event replay are audited, and its artifact references must
match the copied set. The version-1 manifest is written, flushed and fsynced. All
database handles are closed before the directory is renamed to its final destination
on the same filesystem.

Handled copy, audit, manifest-write or publication failures remove private staging
and do not publish a backup at the requested destination. A destination that appears
during verification is rejected and preserved. The source workflow is not modified.
The manifest format and restore command remain compatible with existing snapshots.
Read-only describes the database connection, not every filesystem operation: the
dispatcher lock and SQLite shared-memory handling can still touch source metadata.
The destination receives the verified backup as before.

The parent directory must remain operator-controlled; this does not defend against a
hostile same-user process racing the final filesystem operation. A hard process kill
or power loss can leave unpublished `.orch-backup-*` staging. After confirming no backup
process is active, the operator can remove that exact abandoned directory and retry.
Do not treat abandoned staging as a completed backup. This change does not claim
power-loss durability for the directory rename or add automatic abandoned-directory
collection.

Hashes detect corruption, not malicious replacement of both data and manifest.
Backups still require trusted storage. Broker journals, active process ownership and
worker workspaces are not copied. Host-specific runtime checks and external-provider
admission remain separate from backup correctness.

Five new failure tests cover source corruption, changed copied bytes, interrupted
copy with successful retry/restore, concurrent destination creation and failed
publication. Existing restore and recovery retry drills also validate compatibility.
