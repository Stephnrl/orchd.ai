# Retiring a full operation journal into a successor

The [GitHub](github-journal.md) and [Jira](jira-journal.md) operation journals admit at
most 1,000 records and delete nothing, so both have ended at the same dead end: usage
reports no remaining records and staging is refused. Until now the documentation could
only warn against the obvious next move, because that move is the dangerous one. A
hand-made replacement journal loses the consumed-attempt evidence. The same task is
staged again in the new file, reserved again, and dispatched a second time for work whose
first attempt may already have reached the remote.

Retirement records the link in both directions and closes the old journal to new
admissions. The successor then refuses any task or operation identifier its chain already
retains, which is the property a replacement journal cannot have.

```text
python -m orch github-journal-backup --journal JOURNAL --destination .runtime/github-final
python -m orch github-journal-retire --journal JOURNAL --destination .runtime/github-final \
    --expected-sha256 BUNDLE_SHA256 --successor .runtime/github-journal-002.sqlite \
    --reason "Journal full"
python -m orch github-journal-chain --journal .runtime/github-journal-002.sqlite
```

The four commands exist for both families: `github-journal-retire`, `github-journal-chain`,
`jira-journal-retire` and `jira-journal-chain`. They share one implementation, so neither
journal can quietly diverge from the other.

## What retirement requires

- A [verified snapshot](github-journal.md) whose comparison with the journal is `matches`.
  The retired evidence is therefore both retained in place and captured in a bundle the
  record names by digest. A snapshot taken before later activity is stale, and retirement
  refuses it.
- An audited `--reason`, validated exactly like a cancellation reason: one bounded,
  printable, non-secret-shaped line.
- A successor path that does not exist, is not the journal itself, does not share a name
  with another journal's storage (a successor called `journal.sqlite-wal` is refused), and
  whose parent directory already exists.

The successor is created and records its predecessor **before** the old journal closes, so
an interruption can leave an unreferenced new journal, which admits nothing until its
predecessor names it, but never a retired journal with nowhere to continue.

## What retirement changes, and what it does not

It adds one record to each journal. It deletes nothing and changes no record's state,
reservation time or scope, so `github-journal-audit` and `jira-journal-audit` produce the
**same report digest** before and after. That equality is asserted in the tests and in the
acceptance walkthrough, because it is the whole claim.

A retired journal refuses **only new admission**. Inspection, usage, audit, snapshots,
reservation, recovery plans and reconciliation all keep working, and restaging an existing
operation stays idempotent. That is deliberate: an attempt slot can only be consumed where
its evidence lives, so a prepared record in a retired journal is still reserved and
reconciled there.

Unsettled records therefore do not block retirement, which is where this differs from
[pilot journal succession](pilot-journal-succession.md). A pilot reaches a settled state,
so retirement can insist on one. An operation record has only `prepared` and `uncertain`;
reconciliation produces a report and never resolves the record, so requiring settlement
would mean a full journal could never retire. Retirement records the counts instead, and
reports them again in the chain walk.

## The successor refuses a second attempt

Staging a new record walks the chain and refuses any task or operation identifier a
predecessor already retains, naming the journal and the state that record is in. This is
enforced for every link, not just the immediate predecessor.

It **fails closed**. If a predecessor is missing, unreadable, or does not name this journal
as its successor, new admission is refused rather than allowed: a chain that cannot be
checked is not evidence that an identifier is new. A journal that has been moved therefore
stops accepting new work until it is put back, which is the conservative direction.

The check cannot be outraced, because predecessors admit nothing: once retired, the only
change a record can still undergo is `prepared` to `uncertain`, which introduces no new
identifiers.

## The chain

`github-journal-chain` and `jira-journal-chain` walk from a journal back through its
predecessors, read-only, and report each one's status, record count, prepared and uncertain
counts, both neighbours and the retirement reason, plus the totals across the chain. Every
link is verified in both directions: a predecessor that does not name this journal as its
successor, one missing from disk, a cycle, or a chain longer than its inspection bound of
16 are refused rather than reported as facts.

`github-journal-usage` and `jira-journal-usage` also report `journal_status`, `successor`
and `predecessor` for the journal they were given.

## Snapshots notice retirement

Retirement changes no record, so a bundle taken before it still matches row for row.
Backup comparison therefore compares succession as well and reports `succession_changed`
with both sides, so a stale bundle cannot claim to describe a journal that has since closed
to new admissions. A bundle taken after retirement compares as `matches` again.

## Storage

Succession lives in a `journal_succession` table inside the journal itself, protected by
the same kind of triggers as the records: no update, no delete. Its presence is bound to
schema version 2, and the schema check accepts exactly two shapes — a journal without the
table at version 1, or a journal with the table, both triggers and version 2. The table
without its version, the version without the table, or a missing trigger all make the
journal refuse audits and writes until it is put back.

A journal is migrated only by retiring it or by becoming a successor. Opening one never
rewrites its schema, so existing journals stay exactly as they are and a backup copy stays
byte-comparable with its source.

Each record is bound by digest to the journal it was written in, so a record copied from
another journal, or written to describe one, is refused wherever it would have an effect.

## Boundaries

Succession is not archive, compaction, deletion or restore. The retained records still
occupy the old journal, and the 1,000-record cap applies to each journal separately, so a
chain grows by files rather than by shrinking anything. Nothing here merges journals, moves
a record between them, resolves an uncertain reservation, or authorizes dispatch or retry:
every report still states `retry_allowed: false` and `live_authorized: false`.

A predecessor's path is recorded as it was at retirement, and the chain is walked by path.
These operations do not protect against a privileged actor with direct SQL access. Such an
actor can forge a retirement record and deny a journal new admissions, but cannot use one
to obtain a second attempt: a forged `follows` link is checked against the predecessor it
names, and a record naming another journal is refused. Usage reports the recorded
succession state without that binding check, so treat `*-journal-chain` as the checked walk.

`scripts/journal_succession_acceptance.py` runs the whole walkthrough for both journals
through the real CLI, and the offline release lane runs it as the
`journal_succession_acceptance` stage.
