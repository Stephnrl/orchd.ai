# Verify a saved backup

Check a published backup before selecting a restore destination:

```sh
python -m orch verify-backup --data .runtime/backup-001
```

The command prints a versioned JSON report with `status`, `reason`, `tasks` and
`artifacts`. A passed check exits zero; missing, corrupt or unsupported backups return
`status: failed`, `reason: backup_integrity_failure`, null counts and exit status two.
Raw errors and artifact contents are not exposed. `--destination` and `--task` are
rejected because verification neither restores nor selects part of a backup.

Verification shares restore's manifest shape and file-hash validation, then checks
the exact referenced artifact set, database integrity, contracts, event replay and
persisted broker provenance. It rechecks the manifest and saved file hashes after
the database audit. Verification does not invoke providers, contact Docker, create
a destination, repair files or migrate the database.

Use only an inactive, operator-controlled published backup. SQLite is opened read-only
with immutable mode; it does not create or update SQLite lock/shared-memory files.
Empty WAL files and existing shared-memory files left by backup creation are accepted.
Nonempty WAL or rollback journals are rejected because the manifest covers the saved
database, not uncheckpointed changes. Links/junctions in source paths are rejected.
Do not run verification while another process is modifying the snapshot: immutable
mode deliberately does not coordinate with writers, and this is not a concurrency
or authenticity guarantee. Someone able to rewrite all data and hashes can forge a
consistent backup. A passed report is point-in-time evidence, not a restore approval.

Tests cover a successful CLI check with every snapshot file unchanged, missing and
corrupt inputs, projection tampering with a recomputed database manifest, reference-set
mismatches, unsafe paths, and nonempty database journals. Restore continues to perform
its own copied-byte and staged-audit checks before publication.
