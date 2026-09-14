# Offline GitHub read plans and response transcripts

This is the response-ingestion contract for a future reviewed GitHub transport. It
derives three exact GET requests from an expected prepared journal operation, validates
saved response exchanges, and emits the existing scope-bound observation snapshot for
[combined preflight](github-preflight.md). It does not fetch URLs, acquire credentials,
follow redirects, retry requests, reserve the journal or enable live execution.

## Derive the read plan

```sh
python -m orch github-ref-read-plan --journal .runtime/github.sqlite --operation-id OPERATION_ID --expected-sha256 JOURNAL_SCOPE_SHA256
```

Save stdout as UTF-8 `read-plan.json`. `GitHubRefReadPlan` binds the operation, journal
scope digest, request-preview digest, numeric repository ID, three logical requests and
limits. `sha256` hashes its binding. The journal opens read-only and must still be
prepared with the expected scope.

The roles are `repository`, `base` and `head`. All requests use `https://api.github.com`
and the exact journal repository. Ref reads use the single-reference endpoint. Requests
include the preview's versioned GitHub Accept/API-version headers and
`Accept-Encoding: identity`; no authorization header is included. The endpoints follow
GitHub's [Get a repository](https://docs.github.com/en/rest/repos/repos#get-a-repository)
and [Get a reference](https://docs.github.com/en/rest/git/refs#get-a-reference) contracts.
The single-reference endpoint distinguishes an exact ref from prefix matching.

This plan is data, not a command to execute. A future credential-aware transport must
keep credentials out of the saved logical request and independently enforce origin,
TLS, deadline, redirect, read-size and credential-containment rules. The transcript
cannot establish that any of those transport actions actually happened.

## Transcript format

A UTF-8 JSON transcript has exactly these fields:

| Field | Value |
| --- | --- |
| `kind` | `GitHubRefReadTranscript` |
| `schema_version` | `1.0.0` |
| `plan_sha256` | The read plan's binding digest |
| `responses` | Exactly `repository`, `base` and `head`, each an exchange below |

Each exchange contains exactly `request`, `error` and `response`. `request` must equal
that role's complete logical request from the plan. Changing the method, URL or headers,
adding credentials or a request body, or substituting another role's request rejects the
whole transcript. There is no retry/attempt list.

For a completed HTTP response, `error` is null and `response` contains exactly:

| Field | Value |
| --- | --- |
| `url` | The original planned URL, without redirect or origin substitution |
| `redirected` | JSON false |
| `status` | Integer 200–599; booleans are invalid |
| `headers` | Array of `[name, value]` string pairs; duplicate names are rejected case-insensitively |
| `body_base64` | Canonical base64 of the complete response body bytes |

Body bytes here are the entity body after HTTP transfer framing, without content
decompression. Bodies are limited to 12 KiB each. Headers are limited to 32 pairs and
8 KiB of canonical JSON; names must be HTTP tokens and values printable ASCII.
Content-Encoding must be absent or identity. Content-Length, when present, must be a
decimal integer equal to the decoded body length. Unknown valid headers are discarded.

A 200 response requires `application/json` or `application/vnd.github+json`, optionally
with UTF-8 charset, and no Location header. Its body must be strict UTF-8 JSON without
duplicate keys, non-finite numbers, unsupported numeric ranges or malformed Unicode.
Repository and ref identity/content are then checked by the existing snapshot/preflight
assessment. An intact but mismatching body produces blockers, not a matching result.

Non-200 responses preserve their HTTP status but normalize their body to null. Their
bodies still obey encoding and byte limits, but HTML/error details are not retained in
the snapshot. An unfollowed 3xx is unavailable; a response marked redirected or reporting
a changed URL rejects the transcript. No redirects are followed by this implementation.

For a transport failure, `error` is exactly `timeout`, `connection_failed` or `tls_failed`,
and `response` is null. It normalizes to status 0/body null, which the existing ref
assessment treats as unavailable. Free-form exception messages are not accepted.

## Produce a preflight snapshot

```sh
python -m orch github-ref-transcript-snapshot --journal .runtime/github.sqlite --operation-id OPERATION_ID --expected-sha256 JOURNAL_SCOPE_SHA256 --transcript transcript.json --observed-at 2026-01-01T00:01:00Z
```

Save stdout as UTF-8 `ref-snapshot.json` and use its `sha256` as the expected observation
digest for `github-preflight`. The command reconstructs the plan from the journal,
checks the transcript and then rechecks the prepared scope in a fresh read transaction
before emitting a `GitHubRefObservationSnapshot`. A reservation during decoding prevents
snapshot creation. No database writes occur.

The entire input transcript, including base64 and headers, is limited to 64 KiB. Output
uses canonical JSON and the existing 64 KiB snapshot budget. Invalid shapes, duplicate
input keys, excessive sizes, unexpected requests, invalid timestamps or journal changes
exit 2 without partial stdout. Successful packaging exits 0 even if a captured response
is unavailable or mismatching; run preflight to assess it. The plan and snapshot both
retain `remote_refs_verified: false` and `live_authorized: false`.

The supplied observation time remains unauthenticated. Preflight applies its existing
freshness, policy, scope and final-journal checks. A fully fabricated transcript can
still match; this codec adds structural validation, not trusted collection or approval.
Retain the original plan/transcript separately if needed: the normalized snapshot does
not preserve response headers, error text or a digest of the complete transcript. Valid
200 response bodies remain project data; this is not a new redaction mechanism.

Tests cover exact request binding, encoding and size limits, duplicate headers/JSON,
redirects, unavailable statuses and transport errors, journal reservation during parsing,
CLI round trips, and the complete transcript-to-bundle-preflight path with moved refs
and rate-limit/timeout failures. These use synthetic captures, not live GitHub traffic.

For uncertain operations, [recovery transcripts](github-recovery-transcripts.md) reuse
the strict exchange decoder with bounded pagination and retained-reservation checks.
