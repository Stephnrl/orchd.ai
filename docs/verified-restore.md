# Verified restore publication

Restore now verifies a private staging copy before publishing the requested data
directory. Previously, a failed copy or replay audit could leave a partially restored
database at that destination. The command and version-1 backup format are unchanged:

```sh
python -m orch backup --data .runtime/managed --destination .runtime/backup-001
python -m orch restore --data .runtime/backup-001 --destination .runtime/restored-001
```

Both commands require an explicit destination. Restore requires a new destination
outside the backup, under an existing operator-controlled parent directory. Links
and junctions in maintenance paths are rejected.

Restore validates the manifest and source hashes, copies into a unique
`.orch-restore-*` sibling directory, and checks the copied hashes again before
opening the database. The manifest must name exactly the artifact hashes referenced
by the database. SQLite integrity, contract validation, artifact integrity and
event/projection replay checks must all pass. Restore closes the database before
renaming the verified directory into place on the same filesystem.

Ordinary copy, validation and publication errors remove staging and leave the
requested destination absent. If another process creates the destination during
verification, restore rejects it and preserves that directory. The parent directory
must remain under operator control; this is not a defense against a hostile process
with write access racing the final filesystem operation.

A process kill or power loss can leave a `.orch-restore-*` staging directory. It is
not a published restore: do not launch the application against it. After ensuring no
restore process is active, the operator may remove that exact abandoned directory
and retry from the backup. This change does not claim power-loss durability of the
directory rename or add automated deletion of abandoned directories.

The backup remains unchanged. Approval pauses and cancellation state retain their
existing replay behavior. Broker journals and live process ownership are still not
restored; uncertain execution still needs the existing reconciliation path.

Manifest hashes detect corruption, not authenticity against someone able to rewrite
both the backup and its manifest. Backups must remain in trusted operator storage.

Regression tests inject projection corruption, missing/extra manifest entries,
changed copied bytes, partial copy failure and a destination created during audit.
They check that no unverified database is published, staging is removed on handled
failure, operator data is retained and a clean retry succeeds. CLI tests verify
missing destinations are rejected before storage is opened.

Validated on Windows on September 12, 2026: all 90 tests passed with zero skips,
including the five real Docker gates and existing approval-resume and cancelled-task
restore drills. Contract validation passed 19 examples and 366 negative cases;
Python compilation and whitespace checks also passed.
