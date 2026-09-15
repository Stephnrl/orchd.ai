# Pilot operator visibility

The [retention milestone](pilot-retention.md) left three operator-facing gaps: the
usage report existed only on the CLI, repository-task recovery diagnostics said
nothing about the retained pilot, and a live reclamation recorded no reason. This
follow-up closes them without adding any mutation to the UI.

## Pilot usage in the local UI and API

The store maintenance panel gains **Check pilot usage** next to the storage and
integrity buttons. It calls the authenticated, read-only `GET /pilot-usage`, which
opens the store's `repository-pilots` journal in SQLite read-only mode and returns the
same report as `pilot-usage` minus the journal's host path. The panel shows journal
rows against the absolute cap, live pilots against the live cap with headroom,
workspace bytes, the number of reclaimable workspaces and the status counts, with the
full per-pilot report (eligibility and exact reasons) in the expandable details.

A store without a pilot journal answers 409 and the panel reports "Report unavailable"
rather than an empty report. Query parameters are rejected. Requests are manual, a
new request clears the previous one, and a lost session disables the button like the
other maintenance reads. There is deliberately no reclaim button: reclamation stays a
reviewed CLI action fenced by scope digest and revision.

## Pilot state in recovery diagnostics

`GET /tasks/TASK_ID/recovery-diagnostics` for a repository JSON task now includes a
`pilot` section read from the retained journal: status, revision, recovery hint,
reclamation state (`null`, `reclaiming` or `reclaimed`), whether the workspace still
matches its receipt, unresolved worker dispatch phases, and `reclaimable` with the
exact blockers from the retention eligibility policy (for example "Bound kernel task
is not terminal"). If the journal is missing or fails integrity checks the section is
`null` and the reasons say so. Nothing probes a container, daemon or broker. The UI
already renders the full diagnostics JSON for blocked tasks.

## Audited reclamation reason

`pilot-reclaim` without `--dry-run` now requires `--reason`, validated exactly like a
cancellation reason: 1–500 printable characters and never secret-shaped. The reason
and the operator principal are recorded inside the durable `reclaiming` record before
any deletion, are digest-bound with it, and survive resumption of an interrupted
reclamation unchanged. The command's result echoes the recorded reason.

## Verification and boundaries

`tests/test_pilot_retention.py` adds reason validation, dry-run indifference and
retention across an injected crash; `tests/test_repository_tasks.py` covers the
diagnostics pilot section before and after recovery, the `/pilot-usage` API
authentication, query and method guards, and a missing journal; the UI script and the
Chromium suite exercise the new button with and without a journal. Both acceptance
scripts pass a reason. No automatic reclamation, UI reclaim control, archive or
restore is added.
