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

## Storage and remaining recovery work

The database has its own application identity/version and rejects other SQLite databases.
It never migrates or opens the workflow store through Engine. Do not point it at orch.sqlite.
The in-memory SQLite special path and journal-file symlinks are rejected.

This journal is separate from the existing workflow backup, audit and restore commands;
they do not include or verify it. There is no supported journal reset, success adoption,
automatic retry or backup/restore procedure yet. Restoring an older journal could lose
reservation evidence and must not be used to authorize dispatch. Integration with durable
approval/evidence binding, authenticated reconciliation, journal archival/retirement and a
reviewed recovery procedure remains required before live use. The current offline
[reconciliation report](github-reconciliation.md) cannot clear or finalize a reservation.
