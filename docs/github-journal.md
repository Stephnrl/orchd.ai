# Local GitHub operation journal

The offline broker foundation now has a separate SQLite intent journal. It does not
enable a transport, accept an approval decision or change the workflow engine's
simulated actions.

```sh
python -m orch github-stage --intent intent.json --allow-repository example/project --repository-id 123 --journal .runtime/github-journal.sqlite
```

The command validates the [intent](github-preview.md) and expected repository identity
before creating the journal. It persists the prepared preview and numeric repository ID
under a canonical digest, then returns `state: prepared`. Repeating exactly the same
task/operation/scope is idempotent. Changing the content, destination or repository ID
is rejected. One operation per task is enforced even if a caller supplies a new operation
ID and corresponding branch. Replacement, cancellation and retirement policies are not
implemented; this deliberately conservative milestone does not allow intent revision.

## Reservation behavior

The Python `GitHubJournal.reserve(operation_id, expected_sha256)` API compares the
expected digest and transitions `prepared` to `uncertain` in one immediate transaction,
with `synchronous=FULL`. It commits before returning. The journal has no network function.
The CLI stages and inspects; it does not expose reservation or dispatch.

Exactly one concurrent caller can reserve an operation. Reopening after a process exit
retains `uncertain`; restaging never resets it. Further reservations are rejected. A
reservation records that an attempt slot was consumed, not proof that a request was
sent. An interruption before a future network call would conservatively remain uncertain
too. Every API result still reports `live_authorized: false` and `retry_allowed: false`.

SQLite constraints/triggers preserve scope, prohibit deletion and prevent resetting a
reservation through ordinary SQL updates. The scope digest detects accidental content
changes. This is not protection against an administrator replacing or editing the file,
nor a signed authorization record. Process-crash tests do not establish physical power-loss
durability on every filesystem or network-mounted storage.

## Read-only inspection

```sh
python -m orch github-inspect --journal .runtime/github-journal.sqlite --operation-id bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
```

Use the operation ID returned by staging. Inspection opens an existing journal in
SQLite read-only mode, without creating a missing file or parent directory or initializing
a schema. It returns the retained scope, digest, state and reservation timestamp.
An unknown operation, unsupported database or invalid record exits with code 2.
Success only means the local record passed validation; the result still reports
`live_authorized: false` and `retry_allowed: false`.

Every journal read now reconstructs the expected preview and verifies its request shape,
canonical encoding, nested and outer digests, task/operation binding and reservation-state
consistency. Invalid stored records also prevent staging or reservation from succeeding.
These checks detect malformed evidence; they do not authenticate the repository identity
against GitHub or protect against an administrator rewriting a fully consistent record.
Inspection does not reconcile, repair, reset or dispatch an operation.

Python callers can use `GitHubJournal(path, read_only=True)` and `get(operation_id)`;
both mutation methods reject this mode, and SQLite itself rejects writes.

## Reconcile observations against a retained reservation

```sh
python -m orch github-reconcile-journal --journal .runtime/github-journal.sqlite --operation-id bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb --expected-sha256 <scope-digest> --observations observations.json
```

Use the outer scope `sha256` returned by staging or inspection, not the nested preview
digest. This command requires an existing `uncertain` reservation and an exact expected
scope digest. It validates the retained record and derives the intent and repository
identity from it. Intent and repository overrides are rejected. A prepared operation has
no consumed attempt to reconcile and is rejected; the standalone `github-reconcile`
command remains available for offline proposal assessment.

The bounded observations file uses the same [reconciliation envelope](github-reconciliation.md)
as the standalone classifier, including the nested preview digest. Exit code 0 means one
matching candidate was observed; exit code 2 means unresolved observations or invalid
input. A classification report includes the complete assessment, task/operation IDs,
scope digest, state and reservation timestamp under a new digest. Its checksum covers
the assessment result as well as its input bindings; it is not a signature or approval.

The journal is opened read-only. No report is persisted, no network request is made,
and no reservation is changed. Even a matching candidate leaves the operation uncertain
and unavailable for another reservation. Supplied observations are unauthenticated, and
the retained repository identity is not a fresh allowlist decision. Every result keeps
`retry_allowed`, `remote_effect_confirmed` and `live_authorized` false. Python callers
can use `GitHubJournal.reconcile(operation_id, expected_sha256, observations)`.

## Capacity and retained evidence

```sh
python -m orch github-journal-usage --journal .runtime/github-journal.sqlite
```

Usage opens an existing journal read-only and reports record count, retained scope bytes,
remaining capacity and limits without returning scope contents. Missing databases are not
created. These are logical storage measurements, not record-integrity verification or
physical disk usage.

New intent admission is limited to 1,000 retained records, 16 MiB of combined canonical
UTF-8 scope data, and 64 KiB per scope. Both prepared and uncertain records count.
The aggregate check and insertion share an immediate writer transaction, so concurrent
processes using this implementation cannot claim the same final capacity. Oversized
stored scopes are also rejected before Python materializes or decodes their contents.

Capacity exhaustion rejects a new intent without eviction or a partial insert. Repeating
an existing valid intent remains idempotent, and inspection, reconciliation and reservation
of an already-staged operation remain available. Journals already above the aggregate
limits remain readable and reject new admissions. Individual records must still pass
scope validation and the per-record byte limit.

These fixed application limits do not cap SQLite indexes, page overhead, sidecars or total
filesystem usage, and are not enforced against direct SQL, older code or an administrator.
Do not delete records or start a replacement journal to regain dispatch capacity: that can
lose consumed-attempt evidence. Supported archival and retirement remain future work.

## Whole-journal audit

```sh
python -m orch github-journal-audit --journal .runtime/github-journal.sqlite
```

The audit opens an existing journal read-only, checks SQLite storage with `quick_check`,
and validates every retained record within one read transaction. Its report contains
operation/task IDs, scope digests, states and reservation timestamps in operation-ID order,
plus record count and retained scope bytes. The report digest covers this complete binding.
It omits proposal titles and bodies. Unchanged journal content produces the same digest;
consuming a reservation changes it even though the immutable scope digest stays the same.

Concurrent writers cannot mix old and new records within one audit snapshot. This does
not make the report current after its transaction ends. A corrupt record, failed storage
check or exceeded audit budget exits with code 2 and emits no partial report. Successful
validation exits 0, including for an empty journal. Audits accept at most the admission
record and aggregate scope-byte limits; oversized existing journals still support individual
inspection but need a future bounded recovery procedure for a complete audit.

The report is a diagnostic checksum, not a backup, signature, remote verification or
dispatch authorization. It does not prove that no records were removed before the audit,
authenticate the file against a trusted historical checkpoint, or verify schema triggers.
No records or reports are persisted or repaired, and retry/live authorization remain false.
Python callers can use `GitHubJournal.audit()` on a connection without an active transaction.

## Verified backup bundles

```sh
python -m orch github-journal-backup --journal .runtime/github-journal.sqlite --destination .runtime/github-backup-001
python -m orch github-verify-journal-backup --destination .runtime/github-backup-001
```

Creation requires a new destination directory whose parent already exists. Existing
directories and files are never reused or overwritten. The source is opened read-only,
audited, and copied using SQLite's backup API with a pinned read snapshot, including
committed WAL content. Concurrent changes after that snapshot are not included. The
copied database is converted to a standalone rollback-journal database and audited again.

A complete bundle contains exactly `journal.sqlite` and a canonical `manifest.json`.
The manifest binds the database byte size/hash and complete audit report, and is written
last after syncing the copied database. Creation then runs the independent verifier.
Verification recomputes the file hash and audit, checks the complete manifest, and rejects
extra files or SQLite sidecars. Database copies are limited to 32 MiB, manifests to 1 MiB,
and records to the existing audit budgets. Copy progress has a 30-second deadline.

Verification returns a compact report with `status: valid`, the bundle and audit digests,
and record count. Errors exit 2 without a success report. Interrupted or failed creation
can leave an incomplete destination; it is not reusable and must not be treated as a
verified backup. The command does not delete that output. A truncated or missing manifest
fails verification. File synchronization is not a claim of tested power-loss durability
on every filesystem.

Bundles contain full proposal text and require the same access protection as the source
journal. They are not encrypted or signed. Use trusted local directories; these operations
do not protect against an administrator concurrently replacing files or rewriting a fully
consistent bundle. A valid backup can be stale and lack later consumed reservations.
There is no restore/adoption command: `restore_allowed`, `retry_allowed` and
`live_authorized` remain false. Never replace the active journal with an older backup to
recover capacity or retry an operation.

## Compare backup evidence with the current journal

```sh
python -m orch github-compare-journal-backup --journal .runtime/github-journal.sqlite --destination .runtime/github-backup-001 --expected-sha256 <bundle-digest>
```

Use the bundle `sha256` from backup creation or verification, not its audit or database
digest. Comparison independently verifies the bundle, checks that expected digest, then
audits the current journal read-only. It uses the verified backup audit directly; it does
not reload unverified manifest content for comparison.

The report binds both audit digests, the selected bundle digest and ordered differences:

- `missing_from_backup`: an operation exists only in the current journal.
- `backup_only_operation`: an operation exists only in the backup.
- `scope_mismatch`: the same operation has a different task or immutable scope digest.
- `reservation_mismatch`: its state or reservation timestamp differs.

Exit 0 and `status: matches` mean the audited operation metadata matches. Differences
yield exit 2 and `status: different`; invalid inputs also exit 2, without a comparison
report. Proposal text is omitted. A backup taken before a reservation is consumed is
therefore reported as different even when its immutable intent still matches.

Comparison neither changes the journal nor restores, merges or repairs evidence. It is
not a freshness guarantee or proof of common history: both local inputs could be stale,
rewritten or from different journals. The current journal can change after its audit.
Every report keeps `restore_allowed`, `retry_allowed` and `live_authorized` false,
including exact matches. An operator recovery procedure and trusted provenance are still
required before any future restore or success adoption.

## Disposable recovery drill

```sh
python -m orch github-journal-recovery-drill --destination .runtime/github-backup-001 --expected-sha256 <bundle-digest>
```

The drill verifies the selected bundle and copies its database into a private temporary
directory. It checks the copied bytes and audit before exercising any reservation. Each
prepared operation is reserved once in the disposable copy; every subsequent reservation
must be rejected, including those already uncertain in the backup. The copy is audited,
closed and reopened to check that the resulting evidence persists. The report binds the
bundle and original audit digests plus counts of exercised and rejected reservations.

The source bundle is read-only, and the command accepts no active `--journal` argument.
Scratch storage is removed on normal completion and handled failures; a killed process
can leave temporary files containing proposal text. The reservation loop has a 30-second
deadline. This is an API/reopening drill, not a physical power-loss, disk-failure or process-
crash certification, and it does not certify SQL trigger behavior against direct writes.
An empty backup has zero operations to exercise and can still pass verification.

Success exits 0 with `status: passed`; failure exits 2 with no success report. The drill
does not copy results back, restore a journal, prove backup freshness or authorize retries.
`restore_allowed`, `retry_allowed` and `live_authorized` remain false. Compare against
current evidence separately when it is available; recovery adoption still needs a reviewed
procedure and trusted provenance.

## Storage and remaining recovery work

The database has its own application identity/version and rejects other SQLite databases.
It never migrates or opens the workflow store through Engine. Do not point it at orch.sqlite.
The in-memory SQLite special path and journal-file symlinks are rejected.

This journal is separate from the existing workflow backup, audit and restore commands;
they do not include or verify it. Use the journal-specific backup commands above. There
is no supported journal reset, success adoption, automatic retry or restore procedure yet.
Restoring an older journal could lose
reservation evidence and must not be used to authorize dispatch. Integration with durable
approval/evidence binding, authenticated reconciliation, journal archival/retirement and a
reviewed recovery procedure remains required before live use. The current offline
[reconciliation report](github-reconciliation.md) cannot clear or finalize a reservation.
