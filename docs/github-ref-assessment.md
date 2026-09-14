# Offline GitHub repository and ref assessment

The next broker foundation step compares saved GitHub-shaped responses against an
[intent preview](github-preview.md) and an explicit numeric repository ID. It performs
no network calls and cannot verify who collected the responses or when they were read.
The [combined preflight](github-preflight.md) can now bind these supplied responses to
a journal scope and timestamp, assess their age and combine them with portable evidence
review. The timestamp remains an unauthenticated caller claim.

```sh
python -m orch github-check-refs --intent intent.json --allow-repository example/project --repository-id 123 --observations observations.json
```

The intent is revalidated and its preview digest recomputed. The observations file
must be UTF-8 JSON, at most 64 KiB, without duplicate keys or non-JSON numbers. Its
top-level keys are exactly:

| Key | Value |
| --- | --- |
| `preview_sha256` | Digest from the preview being assessed |
| `repository` | Object with exactly `status` (integer HTTP status) and `body` (saved repository response) |
| `base` | Same response envelope, for the base branch's single-ref endpoint |
| `head` | Same response envelope, for the operation branch's single-ref endpoint |

All three statuses must be 200. The repository body must match the operator-supplied
numeric ID, exact full name and expected API URL; `archived` and `disabled` must both
be JSON false. Each ref must have the exact `refs/heads/...` name and repository-bound
URL, and point to a commit with the expected SHA and commit URL. Missing required
fields, non-object bodies, redirects, failures, renamed/replaced repositories, tags,
cross-repository response URLs and moved tips cannot produce a matching result.
Additional GitHub response-body fields are tolerated but never copied into the output.

The formats follow GitHub's official [repository endpoint](https://docs.github.com/en/rest/repos/repos#get-a-repository)
and [single-reference endpoint](https://docs.github.com/en/rest/git/refs#get-a-reference).
The report includes the corresponding GET request URLs as data only. Ref lookup uses
the single-ref endpoint, not prefix matching. Exact case/URL matching is intentionally
conservative, consistent with the intent allowlist policy.

The result is `status: matches` (exit 0) or `status: blocked` (exit 2), with stable
blocker codes. Invalid envelopes or oversized/invalid files also exit 2. A digest binds
the recomputed preview digest, expected numeric repository ID and supplied observation
digest. The raw responses are not echoed. Neither the CLI nor report opens workflow
storage, creates processes, acquires credentials or publishes anything.

## What matching does not establish

Every result retains `live_authorized: false` and `remote_refs_verified: false`.
Input is untrusted offline evidence: a caller can fabricate matching responses, and
old responses can match old expectations. This is a comparison codec, not an authenticated
fetcher, freshness check, approval decision, signed attestation or dispatch gate.
The engine does not consume it for live execution. Synthetic tests prove comparison
behavior only; they are not captured GitHub responses or live-service validation.

A future trusted transport must validate response origin/status/redirect handling,
apply bounded authenticated reads under an independent credential policy, and bind
fresh observations to the approved operation. Numeric repository identity must also
be bound into the eventual human approval. Branch changes between reads or before PR
creation and uncertain dispatch reconciliation remain unresolved live-broker work.

[Read plans and transcript ingestion](github-read-transcripts.md) now provide the
offline contract for that boundary: exact planned GETs, bounded response bytes, strict
JSON and header checks, failure normalization and a fresh journal check before snapshot
creation. It still does not implement or authenticate a transport.
