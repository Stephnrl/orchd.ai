# Local management UI

Start the server from the repository root and open the exact address it prints:

```sh
python -m orch serve --fixture-provider cli --image python@sha256:APPROVED_DIGEST --data .runtime/managed --port 8080
```

For a fixed local worker demonstration, replace `--image ...` with
`--trusted-fixture`. This mode is not a sandbox. The server always binds 127.0.0.1;
the hostname `localhost` is intentionally not accepted by its Host check.

Paste the process's operator session into the connection form. It is held only in
page memory, never placed in a URL, cookie or web storage. Disconnect/reload clears
the page; restarting the server rotates the session. Disconnect does not revoke the
server's session for other clients; stop the server to revoke it everywhere.

Create a task to confirm the existing fixed greeting specification. Select a task,
then **Run to next pause**. At plan or simulated-action approval, inspect the request
and its referenced evidence, check the acknowledgment, and choose Approve or Reject.
Approval never automatically runs the next stage. Each decision submits the displayed
request ID and task revision; the engine rejects stale, expired or mismatched decisions.
Reading evidence never changes the task. UI task creation uses the default fixture
scenario; the CLI demo still supplies its separate NEEDS_CHANGES scenario.

The UI now supports [operator cancellation](operator-cancellation.md) between
operations. Expand **Cancel this task** and provide a reason; cancellation preserves
evidence and permanently stops further task advancement. Unresolved execution must be
reconciled first.

Records link to their referenced contracts and artifacts. Event buttons open the
persisted payload, including state/context and evidence references. Artifact reads
check task ownership, metadata, length and SHA-256 through Store.read_artifact.
Content is returned inside JSON and rendered using textContent; it is never treated
as HTML, executed, or opened as a user-controlled URL. The UI displays trust/producer
metadata with artifact content. No arbitrary filesystem browsing endpoint is added.

New authenticated read routes:

| Route | Result |
| --- | --- |
| `GET /tasks?after=0&limit=50` | Task states in stable insertion order |
| `GET /tasks/ID/records?after=0&limit=50` | Record IDs and contract types |
| `GET /tasks/ID/records/RECORD_ID` | Validated task-owned contract |
| `GET /tasks/ID/artifacts/ARTIFACT_ID` | Verified metadata and inert text content |
| `GET /tasks/ID/stream` | Finite SSE replay batch after Last-Event-ID |

Task/record pages return an opaque numeric `next` cursor or null. Page sizes are 1–100.
The stream accepts a decimal Last-Event-ID (or `after`/`limit` query), returns up to
100 events with stable per-task sequence IDs, then closes. The browser reconnects
using authenticated fetch every two seconds and appends only subsequent events.
This avoids putting credentials in native EventSource URLs. Replay survives server
restart after reconnecting with its new session and reselecting the persisted task.
Existing JSON events/history routes remain available.

The HTTP server remains synchronous and serves one local operator. A running stage
delays other requests; event updates appear after the stage returns. This is bounded
replay, not a concurrent streaming dispatcher. Recovery remains an explicit CLI/API
operation; the UI does not guess whether an uncertain worker can be retried.

Static UI assets contain no session or task data and can be loaded without a session.
Every data/mutation route requires an explicit Bearer header. Exact same-origin
browser requests are accepted; foreign/null origins, foreign Host values and
cross-site fetch metadata are rejected. POST bodies require application/json.
No CORS grant or cookie authentication is added. Responses set no-store, nosniff,
no-referrer, frame denial and a Content Security Policy allowing only local scripts,
styles and connections. Like the previous API, this is not remote or multi-user
identity provisioning and does not defend against a compromised local account.

The server and UI remain Python plus dependency-free browser JavaScript/CSS. Live
provider enablement still requires the review described in provider-adapter.md;
external GitHub/Jira actions remain simulated or unavailable.

Validation on 2026-09-12: all 65 tests passed with no skips on Windows Docker Desktop.
Browser checks covered login, inert HTML in a title, linked plan/action evidence,
both explicit approvals, COMPLETED with 41 replayed events, and page reload/reconnect.
The browser used the CLI provider fixture and real Docker workers. Automated checks
cover hostile origins/Host, missing sessions, SSE cursors, stale approval revisions,
cross-task evidence, artifact corruption, pagination and server-restart replay/session
rotation. Contract validation passed 19 examples and 366 negative cases; Python
compilation, JavaScript syntax and whitespace checks passed.

Runtime verification `ead3cb12523d4d2c883cc32828af504c` passed with source digest
`4c5262518d3d25c3d140ce68fef9991dfb53d062c9e1b76f63db8512ba68fb17`; UI assets are
now included in that digest. The local report remains under ignored `.runtime/` and
still explicitly reports `provider_authorized: false`.
