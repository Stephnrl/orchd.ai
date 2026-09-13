# Storage usage reports

Operators can inspect artifact quota consumption without changing workflow records
or deleting files:

The connected local UI also offers **Check storage usage** in its store-wide
maintenance panel. It displays artifact quota headroom, orphan/GC counts and missing
or mismatched references, with the report timestamp and expandable JSON details.
Reports are requested manually. A new request clears the previous report; busy or
failed requests show no stale result. Storage reporting is not an integrity verdict.

```sh
python -m orch storage-usage --data .runtime/managed
python -m orch storage-usage --data .runtime/managed --task TASK_ID
```

The CLI requires an existing version-2 database and opens it in SQLite read-only mode.
It does not create or migrate a database, dispatch work, require Docker or invoke a
provider. It takes the usual dispatcher file lock to avoid observing partial workflow
writes; lock-file metadata and SQLite's normal WAL shared-memory handling may still
touch the filesystem. This is read-only with respect to workflow data.

The authenticated API exposes `GET /storage` and `GET /tasks/TASK_ID/storage`. Both
accept no query parameters and return 409 when the dispatcher is busy. Unknown tasks
are rejected. Reports contain counters, limits and fixed database filenames, without
artifact content, hashes, arbitrary filenames or host paths.

| Field | Meaning |
| --- | --- |
| `references`, `logical_bytes` | Artifact references and their cumulative size; duplicates count toward task quotas |
| `task_headroom_bytes` | Remaining ordinary task allowance, clamped at zero; null for a global report |
| `physical_artifact_bytes` | Regular files directly in the artifact directory, counted once each, including staging and unexpected files |
| `disk_headroom_bytes` | Remaining ordinary artifact disk allowance, clamped at zero |
| `audit_reserve_bytes` | Separate trusted audit reserve; not available to ordinary producer output |
| `orphan_files`, `orphan_bytes` | Unreferenced files whose names match the existing GC hash/staging rules |
| `gc_eligible_files`, `gc_eligible_bytes` | Those orphan files at least one day old at scan time |
| `unexpected_entries` | Other names or directories; counted, never traversed or removed |
| `missing_referenced_files`, `size_mismatches` | Missing or incorrectly sized referenced blobs |
| `database_file_bytes` | SQLite database, WAL and shared-memory sizes, outside artifact quotas |

A task report scopes only logical references and task headroom to that task. Physical
storage, cleanup candidates and missing/size-mismatch counts always cover the shared
store. API quotas reflect its Store configuration; the standalone CLI uses the default
64 MiB task and 256 MiB disk limits because custom in-process limits are not persisted.
Both use the same 4 MiB audit reserve constant as artifact ingestion.

Reports are snapshots, not reservations or integrity attestations. The scan checks
sizes but does not hash artifact bytes. Use the existing verified backup/audit path
for content integrity checks. Links and junctions cause rejection. Directories are
not scanned recursively, so the report is not total host-disk usage; workspaces,
broker journals, backups and nested unexpected content are outside these counters.

To remove eligible orphan files, invoke the existing `python -m orch gc --data ...`
command separately. It takes a fresh lock and rechecks references and age. Reporting
does not authorize deletion of referenced evidence or unexpected entries.
Use `python -m orch gc --data ... --dry-run` to preview the same candidates first.

Validated on Windows on September 12, 2026: all 103 Python tests passed with zero
skips, including all five real Docker gates. Six new tests cover duplicate accounting,
quota exhaustion, GC candidate agreement, missing/size-mismatched files, API access,
task scope, database write denial and CLI refusal to create or migrate storage.
Contract validation passed 19 examples and 366 negative cases; Python compilation,
the existing Node UI regression check and whitespace checks also passed.
