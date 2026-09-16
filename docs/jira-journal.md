# Retained Jira comment operations

The [consolidated action package](jira-integration.md) extends this same journal to
creation, transitions, Epic attachment and issue/GitHub links. Legacy comment records
remain readable without migration. One operation per task and all reservation invariants
apply across action types; metadata-only reports omit raw captures for both scope shapes.

The offline Jira adapter has a separate SQLite operation journal. It retains the exact
inputs behind a [comment preview](jira-preview.md) and its expected author, so
[comment recovery](jira-comment-recovery.md) can use saved scope after restart. It does
not enable Jira transport, accept approval, or change workflow task state.

## Stage and inspect

```sh
python -m orch jira-stage --journal .runtime/jira-journal.sqlite --intent jira-comment.json --jira-target jira-target.json --observations jira-response.json --expected-sha256 COMMENT_PREVIEW_SHA256 --jira-author-key EXPECTED_USER_KEY
python -m orch jira-inspect --journal .runtime/jira-journal.sqlite --operation-id OPERATION_ID
```

Staging validates all inputs before creating storage. The retained scope contains the
intent, explicit target, original issue response, expected preview digest, author key
and reconstructed read plan. Reads reconstruct and compare the entire scope, including
canonical encoding, digests and task/operation identities.

The returned `JiraJournalRecord` distinguishes these digests:

| Field | Meaning |
| --- | --- |
| `binding.sha256` | Retained **scope digest**, used for reservation and journal recovery |
| `sha256` | Digest of the inspection report binding, including current state |
| `binding.preview_sha256` | Original comment preview digest |
| `binding.read_plan_sha256` | Comment read-plan digest used by captured response envelopes |

Inspection includes target identity, digests, task/operation IDs, state and reservation
time. It omits comment text, issue text and author profiles. Python `get()` is an internal
API that also returns the full retained scope; callers must treat it as untrusted data.

Repeating the exact task/operation/scope is idempotent, including after reservation and
when full. A different body, visibility, author, issue snapshot or destination cannot
replace saved scope. One operation per task is enforced; assigning a new operation ID
cannot create another comment slot for that task. This bounded contract does not support
a sequence of comments per task. A full journal can now be [retired into a recorded
successor](journal-succession.md) that refuses a second attempt for any task its chain
retains, and a prepared operation can be [withdrawn](journal-revision.md) so its task can be
prepared again under a new operation ID. Editing a retained scope in place remains
unimplemented; records are immutable.

## Reservation and restart behavior

The internal Python method `JiraJournal.reserve(operation_id, scope_sha256)` consumes
one `prepared` record by changing it to `uncertain` with a UTC reservation timestamp.
It uses an immediate SQLite transaction with `synchronous=FULL` and commits before
returning. Exactly one competing process can succeed. Failed transactions roll back;
committed reservations survive process exit. Restaging never resets the state.

The CLI has no reservation or dispatch command. The primitive exists for broker
development and tests; it does not approve live posting. A reservation means an attempt
slot was consumed, not that a request was sent. A future crash between reservation and
network dispatch would conservatively remain uncertain. No API can mark a record
delivered, reset it, delete it or authorize another attempt.

## Recovery from the saved record

For an existing uncertain reservation:

```sh
python -m orch jira-journal-read-plan --journal .runtime/jira-journal.sqlite --operation-id OPERATION_ID --expected-sha256 SCOPE_SHA256
python -m orch jira-reconcile-journal --journal .runtime/jira-journal.sqlite --operation-id OPERATION_ID --expected-sha256 SCOPE_SHA256 --transcript jira-comments.json
```

The first command returns `JiraJournalRecoveryPlan`. Its `binding.read_plan` is the
original `JiraCommentReadPlan`: use that nested plan's `sha256` for the capture's
`plan_sha256`, and its first request for the comment URL. Capture format, pagination
rules and byte limits are described in [comment recovery](jira-comment-recovery.md).
The outer report binds retained scope digest, operation, state and reservation time.

Reconciliation returns `JiraJournalReconciliation`, with the comment assessment under
`binding.assessment`. Exactly one eligible captured comment can yield `candidate_observed`;
unavailable, incomplete or ambiguous observations yield `unresolved`. These commands
require the exact scope digest and an uncertain record. Target, intent, issue-observation
and author overrides are rejected. Original source files are not needed after staging.
Both operations use a consistent read transaction and leave the journal unchanged.

Success exits 0. Unresolved assessments exit 2 with diagnostic JSON. Invalid input,
unknown operations, digest/state mismatch, malformed storage or capacity errors exit 2
without partial stdout. Errors do not echo stored text or SQLite details. Every journal
report has `live_authorized: false`, `retry_allowed: false` and `remote_effect_confirmed:
false`. A matching comment could predate the operation or have been independently posted
by the same user; it is not delivery provenance.

## Capacity, integrity and local storage

```sh
python -m orch jira-journal-usage --journal .runtime/jira-journal.sqlite
python -m orch jira-journal-audit --journal .runtime/jira-journal.sqlite
```

Admission allows 1,000 records, 128 KiB per canonical scope, and 16 MiB of aggregate scope
bytes. Source JSON files still use the existing 64 KiB input loader. Capacity checks and
insertion share a writer transaction across processes. Usage is a logical admission
metric, not file size or an integrity check. Exhaustion never deletes retained evidence.

Audit checks the exact schema and database identity, budgets, SQLite quick check, and
every record in one read transaction. It returns sorted metadata and a digest without
source text. An invalid record fails the whole audit. Reads and mutations reject changed
schemas, missing/extra triggers and disabled check constraints. Ordinary SQL updates
cannot replace scope, delete records or reset state.

Jira journals have their own application ID; workflow and GitHub databases are rejected.
Read commands use an existing database in SQLite read-only/query-only mode, without
creating missing files or directories. Symlinks and Windows reparse points in the journal
path are rejected before opening. These checks assume a trusted local directory; they
do not prevent a privileged actor racing path changes or rewriting a fully consistent
database. Digests and triggers are not signatures or authentication.

Storage retains the **original unredacted issue response** to reproduce its exact snapshot
digest, plus proposed comment and expected author key. Source text remains untrusted and
potentially confidential even though CLI reports omit it. The module does not encrypt
storage. Use approved local storage and host access controls; an inspection report is
not a sanitized copy of the database. [Verified backups and recovery drills](jira-backups.md)
now exercise snapshot integrity and reservation persistence on disposable copies.
Actual restore/retention policy and independent credential/containment review remain
prerequisites for live deployment. Process-crash
tests do not establish power-loss durability on every filesystem or network share.

Tests cover restart recovery, abrupt exit, competing process admission and reservation,
pre-commit rollback, idempotence, scope conflicts, malformed records, schema tampering,
capacity, wrong databases, links, metadata-only output, CLI validation and unchanged
storage during recovery. No live Jira calls are made.
