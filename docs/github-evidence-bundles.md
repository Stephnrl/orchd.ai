# Portable offline GitHub evidence bundles

An operator can now package a selected fixture evidence chain into one file and check
it on another machine without the original workflow directory. This is a review handoff,
not a workflow backup, restore, approval decision or dispatch input. No provider, Docker
process or network action runs during either command.

## Export a selected chain

Use the [record catalog and selection resolver](github-approval.md) to select the exact
patch, test, review and policy records. Save the resolver's JSON as UTF-8 `records.json`.
The export command takes that same closed selection object; it never selects a newer
attempt or substitutes another review or policy.

```sh
python -m orch github-export-evidence-bundle --data .runtime/phase2 --records records.json --destination .runtime/evidence.json
```

The source store opens read-only. The exporter assesses the records and collects their
dependencies within one SQLite read transaction. It requires `claims_consistent`; an
expired policy, unsuccessful test or other blocker prevents publication. Missing,
corrupt or foreign-task dependencies also fail without a report or bundle.

The canonical JSON bundle contains:

- The exact four-record selection and twelve required contract records: patch, test,
  review, policy, test request, work order, review request, tool request, specification,
  implementation plan, plan approval request and plan approval decision.
- Two implementation/test operations with their current-generation broker requests
  and envelope provenance, plus the plan approval registry binding.
- Referenced artifacts, including the original request, snapshot, diff, command outputs,
  broker envelopes and tool payload when present. Bytes use canonical base64 encoding;
  each artifact retains its original task-owned metadata, size and digest.

Unselected records, unrelated artifacts, task context, events and other tasks are not
included. This is the dependency set used by the current fixture assessment, not every
transitive dependency or evidence of actor authenticity. In particular, it does not
resolve every provider invocation or establish policy/approver trust.

The destination must be a new file outside the source workflow directory, with an
existing parent directory and no links in its path. Publication writes and flushes a
temporary sibling, then exclusively creates a hard link at the destination. Existing
files, including files created concurrently, are never replaced. Filesystems without
hard-link support fail closed. Handled failures remove the temporary file; a process
crash can leave an `.orch-evidence-*` sibling. Publication does not claim power-loss
durability for directory entries or protection against a hostile process replacing
ancestor directories during filesystem operations.

## Verify without the source workflow

The export report's `binding.bundle_sha256` is the SHA-256 of the complete file. Retain
that digest with the review handoff and pass it explicitly:

```sh
python -m orch github-verify-evidence-bundle --bundle .runtime/evidence.json --expected-sha256 EXPECTED_BUNDLE_SHA256
```

Verification needs no `--data`. It checks the expected file digest, canonical JSON,
format version and internal payload digest before constructing disposable local storage.
It validates the contracts and artifacts, reruns the evidence assessment, recollects the
selected dependency set and requires an exact match. Missing, duplicate, extra or
inconsistent dependencies are rejected, even if the bundle's digests were recomputed.
Scratch storage is removed on success and handled failure; it is never adopted as an
active workflow. A process crash can leave an `orch-evidence-verify-*` temporary directory.

Policy expiry and chronology are assessed using the verifier's current clock. The
bundle contains no trusted cached assessment. A structurally intact but expired bundle
returns `blocked`; missing or corrupted evidence fails without a report. Both commands
return a metadata assessment, not artifact contents:

| Result | Exit code | Meaning |
| --- | --- | --- |
| `claims_consistent` | 0 | Current local claims agree under the fixture checks |
| `blocked` (verification) | 2 | Intact evidence has current consistency/expiry blockers |
| Error without JSON stdout | 2 | Invalid input, limits, corruption or publication failure |

`evidence_verified`, `live_authorized` and `retry_allowed` remain false. An expected digest
detects changes relative to that supplied value; it does not authenticate whoever
supplied it. A coordinated forgery of internally consistent evidence is not ruled out.
This bundle and its report cannot replace `--evidence` in combined approval assessment
or authorize a GitHub action.

For journal-bound review, use the dedicated [bundle approval commands](github-bundle-approval.md).
They derive evidence hashes from the verified selection, bind the exact bundle file in
a saved preview and recheck its tool payload against the prepared journal scope.

## Bounds and handling

The format allows at most 16 MiB per file, 4 MiB of contract records, forty artifact
references and 8 MiB of artifact bytes. Individual contracts and artifacts remain
limited to 1 MiB. Only twelve contract records, two operations, two broker requests,
two provenance entries and one approval binding are accepted. Unknown fields and
versions are rejected. Files are read with a byte cap; artifact filenames in disposable
storage are validated content hashes, never bundle-supplied filesystem paths.

The exported file contains request text, code, logs and other retained evidence. Base64
is encoding, not encryption. Existing artifact redaction metadata is preserved; export
does not rewrite or newly sanitize the evidence. Handle the bundle as project data.
The report printed to the terminal contains metadata and fixed blockers only.

SQLite records are collected from one read snapshot. Artifact files are separately
checked for task ownership, regular-file status, hash and size as they are read; this
does not create an atomic snapshot of the whole filesystem or authenticate execution.

Tests cover standalone and real-workflow round trips, read-only source access,
unselected-data exclusion, altered bytes and recomputed digests, missing/extra/duplicate
dependencies, cross-task references, bounds, expiry, overwrite races, interrupted
publication, scratch cleanup and CLI success/error behavior.
