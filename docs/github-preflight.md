# Combined offline GitHub preflight

The offline preflight combines a [bundle-bound approval review](github-bundle-approval.md)
with supplied repository and branch observations. It checks local evidence, intended
destination, commit tips, observation age and final journal state in one diagnostic
report. It does not fetch from GitHub, authenticate observations, grant approval,
reserve an attempt or dispatch a request.

## Package observations for an exact scope

Supply the [existing repository/ref response envelope](github-ref-assessment.md):
`preview_sha256`, `repository`, `base` and `head`. The preview hash here is the GitHub
request preview inside the journal scope, not the outer bundle approval preview.
Responses contain integer `status` and a saved response `body`. Save the envelope as
UTF-8 `raw-observations.json`.

```sh
python -m orch github-ref-observation-snapshot --journal .runtime/github.sqlite --operation-id OPERATION_ID --expected-sha256 JOURNAL_SCOPE_SHA256 --observations raw-observations.json --observed-at 2026-01-01T00:01:00Z
```

Save stdout as UTF-8 `ref-snapshot.json`. The command reads the expected prepared journal
scope and validates the observation envelope and size limits. It emits canonical JSON
for a `GitHubRefObservationSnapshot`, binding schema version, journal scope digest,
supplied observation time and responses. Its `sha256` is the digest of that binding.
Unknown snapshot fields and versions are rejected when assessed.

Packaging preserves well-formed failures and mismatches for review; producing a snapshot
does not mean the repository or refs match. The timestamp is supplied by the caller and
is not an attestation. UTC `Z` notation is required, with seconds and optional one to six
fractional digits. Historical or future observations can be packaged but cannot pass
preflight unless they satisfy the checks below.

The entire saved snapshot file is limited to 64 KiB, including its envelope. The CLI
prints canonical JSON to keep its output within that cap. Large raw envelopes can fit
the input limit but exceed the snapshot limit after metadata is added; those are
rejected. Reformatting a large snapshot with added whitespace can also exceed the file
limit. Raw responses are retained in the snapshot, not echoed in assessment reports.

## Run the complete preflight

```sh
python -m orch github-preflight --journal .runtime/github.sqlite --approval-preview bundle-preview.json --expected-sha256 BUNDLE_PREVIEW_SHA256 --bundle .runtime/evidence.json --expected-bundle-sha256 BUNDLE_FILE_SHA256 --observations ref-snapshot.json --expected-observations-sha256 SNAPSHOT_BINDING_SHA256
```

The three expected digests have different roles:

| Option | Identifies |
| --- | --- |
| `--expected-sha256` | The saved outer bundle approval preview |
| `--expected-bundle-sha256` | The complete portable evidence file |
| `--expected-observations-sha256` | The saved observation snapshot's binding |

The command first performs the complete bundle-bound review. If it is already blocked,
preflight returns those blockers without reading the observation file. Otherwise it
validates the snapshot and compares observations against the exact scope in the saved
approval, including the numeric repository identity, active status, full name, API URLs,
base/head ref names, commit object types and both expected commit SHAs.

Snapshots can also be produced from [strict response transcripts](github-read-transcripts.md).
That path validates the saved request/response boundary before normalization and rechecks
the journal after decoding. It does not change preflight's trust or freshness rules.

After that work, preflight rechecks the approval against the current journal. Policy
validity and observation freshness are evaluated at that final check time. Observations
must be at or after the nested approval preview's issue time, not in the future, and
strictly less than five minutes old. Equality at the five-minute expiry is stale.
Fractional timestamps are compared as instants. The short window is fixed, not a
caller-controlled override.

Consequently, a moved head, renamed or replaced repository, unavailable response,
wrong scope/preview digest, stale observations, expiry during assessment or a reservation
observed by the final journal check cannot produce `current`. The report remains a
point-in-time diagnostic; state can change after the final check.

## Results

Snapshot preparation returns JSON with exit 0, or an error with exit 2 and no JSON.
Preflight returns `GitHubOfflinePreflight` with `current`/exit 0 or `blocked`/exit 2.
Malformed files, missing required inputs, exceeded budgets or invalid expected digests
fail with exit 2 and no partial JSON report.

The report binds the complete local review, observation assessment and final approval
check. Fixed blockers are grouped by `approval:`, `evidence:`, `tool:` and `refs:`.
`bundle_bytes_verified` records whether the bundle was checked, and
`observations_checked` records whether observations were assessed. These can differ
when local review blocks after bundle verification. An observation assessment includes
its recorded time, expiry, final check time and the derived GET request plan as data;
no requests are sent. Raw response bodies and bundle artifact contents are omitted.

`remote_refs_verified`, `evidence_verified`, `live_authorized` and `retry_allowed`
remain false. The caller could fabricate matching responses and timestamps. A trusted
fetcher, authenticated approver, credential boundary, atomic admission and uncertain
result reconciliation are still required before live use. The engine does not consume
this report to execute actions, and simulated GitHub tool payloads remain rejected by
the underlying bundle-bound review.

The journal is opened read-only. Bundle verification uses disposable, query-only
storage as before. The snapshot contains supplied response data and should be handled
as project data; packaging does not newly sanitize or encrypt it.

Tests exercise the full CLI flow, no-process/no-network behavior, repository and tip
mismatches, preserved failure responses, exact scope/hash binding, timestamp syntax and
range errors, freshness boundaries, final expiry checks, reservation races, skipped
observation I/O and size limits.
