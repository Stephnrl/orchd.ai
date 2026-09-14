# Evidence review in the local workspace

The selected-task view now includes an Evidence review panel. It brings the existing
record catalog, explicit selection resolver and claims assessment into the authenticated
local UI. This is a diagnostic workflow: it does not approve, run, recover or advance
a task, and does not call GitHub or a provider.

## Operator flow

1. Select a task and click **Load review choices**. The panel lists retained review and
   policy records by creation time and full record ID, without selecting either one.
2. Choose the intended review and policy. Use **Inspect review** and **Inspect policy**
   to read them in the existing evidence viewer. The policy list can contain tool
   decisions for implementation and tests as well as the action policy; select the
   intended record rather than assuming the first item is suitable.
3. Click **Check selected evidence**. The server resolves the chosen review's patch
   and single test receipt, then runs the same checks as `github-check-record-claims`.
   A consistent result or fixed blockers appear with the assessment time. Full metadata
   and the exact resolved selection are available under diagnostic details.
4. **Save record selection** downloads UTF-8 `orchd-record-selection.json`. It contains
   the task ID and exact patch, test, review and policy references, ready for the
   [portable bundle exporter](github-evidence-bundles.md).

For example, after saving the selection:

```sh
python -m orch github-export-evidence-bundle --data .runtime/phase2 --records orchd-record-selection.json --destination .runtime/evidence.json
```

The saved file is a selection, not a bundle, cached approval or proof of current
readiness. Saving remains available for a completed blocked diagnostic, so the exact
selection can be retained for investigation. Bundle export independently rechecks it
and rejects blocked or expired claims. The UI itself does not create artifacts or
publish bundles into workflow storage.

## Stale and incomplete states

An empty catalog or missing review/policy pair leaves assessment disabled. The operator
can load choices again after the workflow reaches review. The catalog retains its
existing 200-record cap; it does not silently truncate or automatically pick newer
records when the cap is exceeded.

Changing either choice clears the previous diagnostic and download. Refreshing or
switching tasks also clears the choices and result. Obsolete asynchronous responses
cannot repopulate a newer selection or a different task. Requests disable relevant
controls while pending; failures clear saved results and allow an explicit retry.
Background event-driven task refreshes also invalidate the panel.

The displayed assessment is tied to its selected immutable records and check time;
it is not continuously revalidated as a policy ages. `evidence_verified`,
`live_authorized` and `retry_allowed` remain false. Approval controls are separate and
unchanged by running the diagnostic. Record IDs, blockers and metadata render as text.

## Authenticated API

| Route | Request | Result |
| --- | --- | --- |
| `GET /tasks/{task}/evidence-catalog` | No query parameters | Existing `GitHubEvidenceRecordCatalog` |
| `POST /tasks/{task}/assess-evidence` | Exactly `review` and `policy` document references | Resolved `selection` and `assessment` |
| `POST /tasks/{task}/evidence-bundle` | Exactly `patch`, `test`, `review` and `policy` document references | Canonical portable bundle bytes, only after a fresh consistent assessment |

All routes require the existing in-memory operator bearer session. Task identity
comes from the URL, not a request-body override. Each reference contains `id` and
`sha256`; malformed, stale-hash or foreign-task references are rejected. Missing or
corrupt dependencies, exceeded catalog limits and database errors produce a generic
409 error with no partial selection. Missing sessions produce 401.

The assessment route returns both `claims_consistent` and `blocked` as successful
diagnostic responses (HTTP 200).
Clients must inspect `assessment.status`. The API does not accept a proposed decision,
new instructions, a substitute task ID or a request to execute. Selection resolution
and assessment use the existing read transactions; this is not atomic admission or a
claim that the task cannot change afterward.

Validation includes API authentication/ownership and malformed-input tests, no-write
and no-execution assertions, UI stale-response/error tests and a real Chromium fixture
workflow that checks evidence, inspects a policy, downloads the selection and confirms
unchanged task state. Desktop and narrow-screen layouts are visually checked.

## Download a portable bundle

After checking a consistent selection, choose **Download evidence bundle**. The server
reassesses that exact chain and collects its dependencies in one read transaction using
the same builder as the CLI exporter. Expired policy, changed or missing evidence and
size-limit failures reject the download. No file is published into the workflow store.

The response uses a fixed attachment filename, `application/json`, exact Content-Length,
`X-Orch-Bundle-SHA256` over the complete file, and the API's no-store headers. The browser
limits the stream to 16 MiB, checks its declared length and SHA-256, then offers the file
as `orchd-evidence-bundle.json`. Keep the displayed digest for the
[standalone verification command](github-evidence-bundles.md). A hash supplied by this
server detects changed bytes; it does not independently authenticate the server or
grant execution authority. The file includes selected project evidence.

Changing the selection, refreshing or switching tasks cancels an outstanding download
and clears its digest. Late results cannot offer a stale file. A failed download clears
the assessment and requires another check. The displayed success means the bytes were
prepared for the browser download, not that a filesystem save completed. Restart the
local API after upgrading so the new route is available.
