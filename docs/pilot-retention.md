# Repository pilot retention and reviewed workspace reclamation

The pilot journal previously refused a 129th pilot and offered no way to release
capacity or disk. This milestone adds a read-only usage report and an explicitly
reviewed reclamation command. Reclamation deletes only a pilot's disposable
workspace directory. Journal rows, scopes, receipts, worker journals and any
kernel task records remain retained and continue to verify.

```text
python -m orch pilot-usage --data JOURNAL
python -m orch pilot-reclaim --data JOURNAL --pilot-id ID --expected-sha256 SCOPE_DIGEST --expected-revision REVISION --dry-run
python -m orch pilot-reclaim --data JOURNAL --pilot-id ID --expected-sha256 SCOPE_DIGEST --expected-revision REVISION --reason "Evidence archived"
```

A live reclamation requires one audited `--reason` (1–500 printable characters,
never secret-shaped), recorded with the durable `reclaiming` intent alongside the
operator principal, exactly like a cancellation reason. A dry run needs none. Resuming
an interrupted reclamation keeps the reason recorded with its original intent. The
[operator visibility follow-up](pilot-operator-visibility.md) also exposes the usage
report in the local UI and the pilot's state in repository-task recovery diagnostics.

For task-bound pilots the journal is `STORE/repository-pilots`. Neither command
requires the pilot's Docker daemon or image; reclamation never runs a worker.

## Usage report

`pilot-usage` opens the journal read-only and lists every pilot with its status,
workspace bytes and file count, retained reclamation status, and the exact reasons
it is or is not reclaimable. It reports `journal_rows` against the absolute journal
cap (1024) and `live_pilots` against the live cap (128). Only pilots with a
completed reclamation record leave the live count. A pilot whose retained scope,
receipt or reclamation record fails integrity checks is listed as ineligible with
that reason; the report never repairs or hides it.

## Eligibility

Reclamation is refused unless all of the following hold at review time:

- The pilot is `passed`, `failed`, `abandoned` or `interrupted`. A `reserved` pilot
  must be reconciled first. A standalone `prepared` pilot may still run and must be
  abandoned explicitly first.
- A `prepared` pilot bound to a kernel task whose state is `COMPLETED`, `FAILED` or
  `CANCELLED` can never gain admission, so reclamation abandons it in the same
  fenced transaction. The task kernel does not otherwise inform the journal of
  rejection or cancellation.
- No Docker worker phase is still `dispatched`.
- A bound kernel task is terminal, its retained plan references this exact pilot
  and scope digest, and its store is readable. An unavailable or mismatched store
  blocks reclamation; it does not default to permit.
- The workspace contains only expected replacement paths as regular, unlinked,
  size-bounded files. Unexpected entries, links or special files are evidence of
  tampering and keep the workspace retained for investigation.

`--expected-sha256` and `--expected-revision` must match the current journal row
exactly, as for every other pilot mutation, and the worker lock is held so no
pilot can be running concurrently. Reclamation neither bumps the pilot revision
nor changes its status, except for the bound-abandonment case above.

## Durable two-phase record

`--dry-run` returns the inventory that would be deleted and writes nothing. A live
reclamation first commits a `reclaiming` record with the hashed file inventory,
bound task state and timestamp, then deletes files and directories bottom-up, then
marks the record `reclaimed`. A crash between those commits leaves a visible
`reclaiming` record; rerunning the same reviewed command resumes it only if every
remaining file is in the recorded inventory with its recorded hash. Any change
after the intent was recorded is refused and retained.

`pilot-inspect` shows `reclamation` as `null`, `reclaiming` or `reclaimed`. After
reclamation, `observed_files` is empty and `matches_receipt` is false because the
workspace no longer exists; the receipt itself is unchanged and still verifies
against the retained scope. Legacy journals without the reclamation table open
read-only unchanged and gain the table on their next writable open.

## Boundaries

Reclamation is not archive, compaction or backup. Journal rows are never deleted,
so the absolute journal cap is reached eventually and requires a new journal.
Standard workflow backups continue to exclude the live pilot journal, and a
reclaimed workspace cannot be restored; task-bound results are served from the
retained kernel snapshot artifacts, which reclamation does not touch. No automatic
or scheduled reclamation exists.

`python scripts/pilot_acceptance.py` and `python scripts/repository_task_acceptance.py`
exercise usage, dry-run, reclamation, restart inspection and kernel snapshot
retention; both run in the offline release lane, and the Docker lane runs the
worker variants.
