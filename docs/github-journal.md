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

## Storage and remaining recovery work

The database has its own application identity/version and rejects other SQLite databases.
It never migrates or opens the workflow store through Engine. Do not point it at orch.sqlite.
The in-memory SQLite special path and journal-file symlinks are rejected.

This journal is separate from the existing workflow backup, audit and restore commands;
they do not include or verify it. There is no supported journal reset, success adoption,
automatic retry or backup/restore procedure yet. Restoring an older journal could lose
reservation evidence and must not be used to authorize dispatch. Integration with durable
approval/evidence binding, authenticated reconciliation, journal lifecycle/quotas and a
reviewed recovery procedure remains required before live use. The current offline
[reconciliation report](github-reconciliation.md) cannot clear or finalize a reservation.
