# Paginated GitHub recovery transcripts

An uncertain journal operation can now produce an exact PR lookup plan and assess a
saved paginated capture against its retained reservation. This extends the
[read transcript boundary](github-read-transcripts.md) to uncertain-result recovery.
It does not send requests, reset the journal, adopt a PR or authorize a retry.

## Plan and assess

```sh
python -m orch github-recovery-read-plan --journal .runtime/github.sqlite --operation-id OPERATION_ID --expected-sha256 JOURNAL_SCOPE_SHA256
python -m orch github-reconcile-transcript --journal .runtime/github.sqlite --operation-id OPERATION_ID --expected-sha256 JOURNAL_SCOPE_SHA256 --transcript recovery-capture.json
```

Both commands require the expected retained scope in state `uncertain`. They open the
journal read-only; intent and repository overrides are rejected. Planning returns a
`GitHubRecoveryReadPlan` whose binding includes the operation, scope, reservation time,
repository ID, preview digest, first GET request and limits. Its `sha256` hashes that
binding. The lookup uses `state=all`, the exact owner/head branch, `per_page=100` and
`page=1`. Headers are the pinned preview headers plus `Accept-Encoding: identity`;
credentials are not part of the logical request.

The query follows GitHub's [List pull requests](https://docs.github.com/en/rest/pulls/pulls#list-pull-requests)
contract. Continuation metadata follows its documented
[Link pagination](https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api).
The implementation consumes saved exchanges only; these links are never fetched.

## Capture format and limits

The transcript is a UTF-8 JSON object with exactly `kind: GitHubRecoveryReadTranscript`,
`schema_version: 1.0.0`, `plan_sha256` and `pages`. `pages` is an ordered array of one to
ten exchanges using the same `request`, `error`, `response` shape as the ref transcript.
Each request must match the derived GET for its one-based page index, including headers
and canonical query encoding. Skipped, repeated and out-of-order requests are rejected.
Captures do not supply their own `has_next` flag.

The existing strict decoder enforces a 12 KiB body limit per page, canonical base64,
declared content length, response origin and redirect restrictions, duplicate-header
rejection and strict UTF-8 JSON for 200 responses. The entire transcript remains bounded
to 64 KiB, including base64 and headers; ten pages is a maximum, not a promise that ten
full pages fit. Non-200/error bodies are not retained in the assessment. Timeouts and
other allowed transport failures remain unavailable observations.

## Pagination validation

The optional Link header accepts up to four comma-separated entries with quoted
`next`, `prev`, `first` or `last` relations. Duplicate/unknown relations, extra link
parameters and malformed entries are rejected. Next must be exactly the following
page, previous the preceding page, first page 1, and last no earlier than the current
page. A last-page hint beyond the current page requires a next link.

Every link must use HTTPS on `api.github.com`, without fragments, and preserve the
exact state/head/page-size query values. Duplicate or extra query parameters are
rejected. Query ordering and percent encoding may vary in links, but not in recorded
logical requests. Paths may use the original owner/name endpoint or
`/repositories/{bound numeric ID}/pulls`; a different repository ID is rejected. Later
logical requests are always derived from retained scope, never copied from link URLs.

Reading another page after a response declared no continuation rejects the capture.
Stopping while a next link remains, including at the ten-page cap, produces an unresolved
assessment. A later response cannot erase an earlier last-page hint: if a capture ends
before a previously advertised page, it remains incomplete. An omitted Link header is
treated as a captured end-of-pagination claim, not independently verified completeness.
Remote lists may change between pages; this importer cannot establish a stable snapshot.

## Result and retained-state checks

After decoding, a fresh transaction rechecks the journal schema, uncertain state, scope
and reservation identity, then runs the existing journal reconciliation classifier.
The result `GitHubRecoveryTranscriptAssessment` binds the plan digest, canonical transcript
digest, checked page count and full journal reconciliation report. Raw response bodies
and headers are omitted. No journal mutation occurs.

Exactly one matching candidate across a complete capture can produce
`candidate_observed`/exit 0. Missing, edited, closed, duplicate or conflicting candidates,
unavailable pages and incomplete pagination produce `unresolved`/exit 2 with a report.
Malformed captures, exceeded budgets and changed/invalid reservations exit 2 with no
partial stdout. Planning succeeds with exit 0; neither command creates a reservation.

Every result retains `retry_allowed: false`, `remote_effect_confirmed: false` and
`live_authorized: false`. Captures remain unauthenticated and may be fabricated or stale.
A candidate observation does not prove that this operation created the PR. Trusted
collection, operation provenance and an admitted operator recovery policy remain
necessary before live reconciliation. Tests use synthetic responses, not live traffic.
