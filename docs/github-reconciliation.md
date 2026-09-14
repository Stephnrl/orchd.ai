# Offline reconciliation of an uncertain PR result

An interrupted create request does not establish whether GitHub created a PR. This
milestone adds a comparison codec for saved list-PR observations. It performs no fetch,
POST, retry, credential access or workflow update. Existing platform actions stay simulated.
The separate [operation journal](github-journal.md) can retain staged scopes and consumed
attempt reservations. This classifier cannot update or clear journal state.
For an existing uncertain reservation, `github-reconcile-journal` derives the intent
and repository identity from the validated journal and binds this assessment to the
retained reservation. See the [journal command](github-journal.md#reconcile-observations-against-a-retained-reservation).

```sh
python -m orch github-reconcile --intent intent.json --allow-repository example/project --repository-id 123 --observations observations.json
```

The [intent](github-preview.md) is revalidated and its preview digest recomputed.
The observations envelope has exactly `preview_sha256` and `pages`. Each page has exactly
`page` (integer, starting at 1), `status` (integer HTTP status), `has_next` (JSON boolean)
and `body` (the saved PR-list response). Page numbers must be consecutive; every page
except the last must indicate another page, and the last must indicate none. At most
10 pages, 100 rows per page, and 64 KiB of total JSON are accepted. The strict file loader
rejects duplicate keys, non-JSON numbers and oversized files.

The report includes a proposed GET URL using the [GitHub list-PR endpoint](https://docs.github.com/en/rest/pulls/pulls#list-pull-requests),
`state=all` and the exact owner/operation-head filter. It deliberately omits a base filter
so a PR whose base changed can still be observed. The URL is data only and is never
followed. A future collector must translate trusted pagination metadata into the page
envelopes; arbitrary response-supplied links must not become unrestricted fetch targets.

Every row must match the proposed title/body, open draft state, unmerged status, exact
base/head refs and commit SHAs, numeric repository identity and repository-bound URLs.
PR IDs and numbers must be positive integers, and both API and web URLs must identify
that PR in the allowed repository. Extra response fields are tolerated without echoing
them. Missing fields or unexpected rows cannot be silently treated as a successful match.

Exactly one matching candidate with complete pages yields `candidate_observed` (exit 0).
No candidate, duplicate candidates (including duplicate pages/rows), mismatched scope or
state, failed responses, and incomplete/inconsistent pagination yield `unresolved` (exit 2)
with fixed reason codes and no candidate URL. Invalid input also exits 2.
The binding digest includes the preview, expected repository ID and observation digest.

## No success or retry authority

Every result carries `retry_allowed: false`, `remote_effect_confirmed: false` and
`live_authorized: false`. An empty list is not permission to repeat a POST. An observed
candidate is not proof of authorship, freshness, authenticated origin or successful
dispatch. A caller can fabricate the supplied JSON, and list results can change between
pages. The engine never consumes this assessment as a receipt or approval.

Live reconciliation still needs durable dispatch intent, authenticated bounded collection,
trusted operation provenance, approval binding, branch-race handling, authoritative
response verification and an operator recovery policy. Tests here use synthetic responses
only. Closed, merged, edited or no-longer-draft PRs intentionally remain unresolved for
review rather than being automatically adopted or recreated.

[Paginated recovery transcripts](github-recovery-transcripts.md) now derive exact lookup
requests from an uncertain journal operation, validate captured Link continuations and
response bytes, and recheck the reservation before running this classifier. Truncated
captures remain unresolved and no retry or adoption authority is added.
