# Retiring a pilot journal and starting its successor

A pilot journal refuses a new pilot once it holds 1024 rows, and rows are never deleted,
so [retention](pilot-retention.md) has always ended at "this requires a new journal" with
nothing to carry the relationship. Starting one by hand loses the link: the old evidence
sits on disk with nothing recording where work continued, and nothing stops an operator
preparing into a journal that is meant to be finished.

Retirement closes that gap **without deleting a row**. It records the link in both
directions, closes the old journal to new preparations, and leaves every retained pilot
exactly where it was.

```text
python -m orch pilot-journal-backup --data JOURNAL --destination .runtime/pilot-final
python -m orch pilot-journal-retire --data JOURNAL --destination .runtime/pilot-final \
    --expected-sha256 BACKUP_SHA256 --successor .runtime/pilot-journal-002 --reason "Journal full"
python -m orch pilot-journal-chain --data .runtime/pilot-journal-002
```

## What retirement requires

- A [verified snapshot](pilot-journal-backups.md) whose comparison with the journal is
  `matches`. The retired evidence is therefore both retained in place and captured in a
  bundle that the record names by digest. A snapshot taken before later activity is
  stale, and retirement refuses it.
- **Every pilot settled**: `passed`, `failed`, `abandoned` or `interrupted`. A `prepared`
  or `reserved` pilot could still run, so retirement names it and refuses. Abandon or
  reconcile it first.
- An audited `--reason`, validated exactly like a cancellation reason.
- A successor directory that does not already exist, is not the journal itself, and
  neither contains nor sits inside it.

The successor is created and records its predecessor **before** the old journal closes,
so an interruption can leave an unreferenced new journal, which is harmless, but never a
retired journal with nowhere to continue.

## What retirement changes, and what it does not

It adds one record to each journal. It deletes nothing, reclaims nothing, and changes no
pilot's status or revision, so every retained row keeps verifying and
`pilot-journal-audit` reports exactly what it did before. The report says `rows_deleted:
0` because that is the point.

A retired journal refuses **only preparation**. Inspection, usage, audit, snapshots,
reconciliation and [reclamation](pilot-retention.md) all keep working, because an
operator may still need to settle and clean up what is already there.

## The chain

`pilot-journal-chain` walks from a journal back through its predecessors, read-only,
and reports each one's status, retained rows and live pilots, plus the totals across the
chain. It verifies every link in both directions: a predecessor that does not name this
journal as its successor, one that is missing from disk, a cycle, or a chain longer than
its inspection bound are all refused rather than reported as facts.

`pilot-usage` also reports `journal_status`, `successor` and `predecessor` for the
journal it was given.

## Boundaries

Succession is not archive, compaction or deletion. The retained rows still occupy the old
journal, and the cap still applies to each journal separately, so the chain grows by
directories rather than by shrinking anything. Nothing here merges journals, moves a
pilot between them, or restores one. A predecessor's path is recorded as it was at
retirement; moving that directory afterwards breaks the chain walk, which is then
refused rather than silently skipped. `pilot_acceptance` retires a settled journal,
proves the retired one refuses a new pilot, prepares in the successor and walks the
chain, so the offline release lane covers it.
