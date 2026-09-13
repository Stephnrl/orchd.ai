# On-demand integrity audit

Run the existing backup integrity checks without creating a snapshot:

```sh
python -m orch audit --data .runtime/managed
```

The CLI opens an existing version-2 database read-only and takes the dispatcher lock.
It does not create or migrate a database, repair data, dispatch work, require Docker
or invoke providers. As with storage reporting, lock metadata and SQLite shared-memory
handling may touch the filesystem; workflow records and artifacts are not written.
`--task` is rejected because this check covers the complete store.

The authenticated API route is `GET /integrity-report`. It accepts no query parameters
or repair options. A completed audit returns HTTP 200 with an explicit `status` of
`passed` or `failed`. Inspect that field rather than treating HTTP 200 as an integrity
verdict. Missing authentication returns 401; a busy dispatcher rejects the request
with 409 and provides no verdict.

The report checks SQLite integrity and foreign keys, validates stored contract shapes,
verifies referenced artifact sizes and SHA-256 hashes, and compares each task projection
with its event replay. This detects changed bytes even when file sizes are unchanged.
Persisted broker requests are checked against their operation IDs, owning tasks and
generations. Every stored execution-provenance row must resolve to its durable request
and a task-authorized artifact produced by the action broker as a trusted receipt.
The envelope must satisfy the broker wire schema and match its saved digest, request
hash, nonce, generation, task, operation and source digest. Recomputing an artifact
hash alone cannot conceal a broken request/envelope binding.

These same checks run before backup publication and during staged restore validation.
An interrupted request may legitimately have no committed provenance yet. Recovery
may also advance an operation generation beyond its historical evidence; that older
evidence remains valid. The audit compares persisted source digests with each other,
not with currently installed code, so upgrading the runtime does not invalidate history.
It does not contact Docker, read broker journals, or persist new evidence.

On success it reports task, record and artifact-reference counts. On failure counts
remain null and the reason is `database_artifact_or_replay_integrity_failure`.
Artifact content, arbitrary paths, hashes and raw exception messages are excluded.

CLI exit status is zero for a passed report and two for a failed report. Invalid CLI
arguments or missing data are rejected before opening storage. Failure to open an
invalid/unsupported database, or failure to acquire its lock, is a command error rather
than a completed integrity report. No report is persisted automatically.

The result is a snapshot of local consistency, not an authenticity signature or a
complete semantic/security review of every contract relationship. Someone able to
rewrite the database and evidence may forge a consistent history. Unreferenced files,
broker journals and runtime process state are outside this audit. Use storage reports
for orphan accounting and recovery diagnostics for blocked-task preconditions.

Auditing holds the dispatcher lock for the duration of the full scan and can take
longer as history grows. The loopback API is synchronous, so other requests wait during
an audit; operators can use the CLI during a suitable maintenance window instead.

Local validation: 16 focused integrity, storage-reporting and backup-publication tests
passed. Five new tests cover unchanged data, same-size corruption, projection drift,
missing blobs, API boundaries, CLI exit codes and refusal to create missing storage.
The PR's CI provides the full Windows/Linux and separate Docker results.
