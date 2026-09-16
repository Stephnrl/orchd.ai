# Withdrawing a prepared operation so its task can be prepared again

One operation per task and one attempt per operation are the [GitHub](github-journal.md)
and [Jira](jira-journal.md) journals' two hard rules, and together they left a task no way
forward when its prepared scope stopped being correct. A GitHub intent binds
`expected_base_sha`; once the branch moves, that prepared operation can never be
dispatched, and the task can never be prepared again, because its one operation is already
bound to the stale scope. The documentation called this "no intent revision" and stopped
there.

Withdrawal releases the task **without releasing an attempt**.

```text
python -m orch github-journal-backup --journal JOURNAL --destination .runtime/github-pre-upgrade
python -m orch github-journal-upgrade --journal JOURNAL --destination .runtime/github-pre-upgrade \
    --expected-sha256 BUNDLE_SHA256
python -m orch github-journal-withdraw --journal JOURNAL --operation-id OPERATION_ID \
    --expected-sha256 RETAINED_SCOPE_SHA256 --reason "The base branch moved before dispatch"
```

Both commands exist for both journals, over one implementation, so neither journal can
diverge from the other.

## What withdrawal does and refuses

A `prepared` operation becomes `withdrawn`. It keeps its scope, its digest, its reservation
history — there is none — and its place in the journal. Its task is then free, and the
replacement must carry a **new** operation identifier: identifiers are never reused, in any
state, so restaging the withdrawn one is refused.

Withdrawal requires the retained scope digest and an audited reason, validated exactly like
a cancellation reason.

An `uncertain` operation can never be withdrawn. Its attempt may already have reached the
remote, so it has to be reconciled, exactly as an uncertain task must be reconciled rather
than [cancelled](operator-cancellation.md). This is the rule the whole feature exists to
preserve: withdrawal never converts a consumed attempt back into an available one, which is
why every report says `attempt_released: false`.

## Storage enforces it, not this code

The rebuilt `intents` table carries a unique index over `task_id` restricted to operations
that are not withdrawn, so a second live operation for one task is refused by SQLite itself
even under direct SQL. The update trigger permits exactly two transitions, `prepared` to
`uncertain` and `prepared` to `withdrawn`, so a withdrawn record can never be reserved,
revived or rewritten, and the delete trigger still refuses to remove anything.

Each withdrawal also writes an append-only record naming the journal, the task, the scope
digest, the reason, the principal and the time. That table has the same no-update and
no-delete triggers, and every read checks the record's digest, so a rewritten or truncated
reason fails the audit that reads it.

## The upgrade

SQLite cannot drop the `UNIQUE` column constraint the original table carries, so reaching
that shape rebuilds `intents`. `*-journal-upgrade` does it in one transaction against a
[verified snapshot](github-journal.md) whose comparison is `matches`, copies the rows
through a scratch table so the stored schema text stays exactly what the code declares, and
refuses to commit unless every row comes through unchanged. It then reports the audit digest
before and after, which are equal because nothing about the records changed.

An interrupted upgrade rolls back whole: the journal stays at its previous schema version,
with every record where it was. Schema version 3 also carries the succession table, so a
journal that has been upgraded can still be [retired](journal-succession.md), and a retired
journal can still be upgraded.

Journals are never migrated implicitly. Opening one leaves its schema alone, so a
pre-upgrade journal keeps working exactly as before — it simply refuses withdrawal, naming
the upgrade.

## How it meets the rest of the lifecycle

- **Retirement.** A retired journal still withdraws, because withdrawal is not new
  admission; only preparing new work stops. So a stale prepared operation in a retired
  journal can be withdrawn there, and its task then prepared in the successor.
- **The chain.** The successor refuses any task a predecessor still holds live, and ignores
  withdrawn ones — but it still refuses a reused operation identifier, whatever its state.
- **Snapshots.** A withdrawal is reported as `withdrawal_mismatch` by backup comparison, not
  as a reservation difference, so a reviewer sees what actually happened.
- **Recovery drills.** A drill counts withdrawn records separately, proves each still
  refuses reservation, and plans no recovery for them, because nothing was attempted.
- **Usage.** `*-journal-usage` reports `withdrawn_records`.

## Boundaries

Withdrawal is not deletion, revision of a record, or retry authority. The withdrawn
operation and its scope stay in the journal and keep counting against its capacity, so
withdrawing does not recover admission capacity — [retirement](journal-succession.md) is the
answer to a full journal. Nothing here dispatches, reconciles or authorizes anything: every
report keeps `retry_allowed: false` and `live_authorized: false`.

The replacement operation is a new preparation and gets no special standing from the
withdrawal it follows; it is reviewed and approved like any other. These operations also do
not protect against a privileged actor with direct SQL access, beyond the constraints and
triggers above, which such an actor can drop — and a journal whose protections are missing
refuses audits and writes until they are restored.

`scripts/journal_lifecycle_acceptance.py` walks both journals through retirement,
succession, upgrade and withdrawal through the real CLI, including the refusal to withdraw a
consumed attempt, and the offline release lane runs it as the
`journal_lifecycle_acceptance` stage.
