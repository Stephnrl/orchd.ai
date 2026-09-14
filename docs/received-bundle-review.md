# Review a received evidence bundle

The local workspace can verify a portable evidence file without its original task
store. The reviewer can start with an empty workspace. This uses the same standalone
verifier as `github-verify-evidence-bundle`; it does not import a task, adopt a database,
grant approval or contact a provider or GitHub.

## Review flow

1. Connect using the session printed by your local API.
2. Under **Verify a received bundle**, select the handoff's JSON file (1 byte to 16 MiB).
3. Enter the expected whole-file SHA-256: exactly 64 lowercase hexadecimal characters.
   Obtain this digest through your trusted handoff channel. The UI deliberately does
   not populate the expected value from the file being checked.
4. Choose **Verify received bundle**. The browser hashes the bytes before upload and
   rejects a mismatch locally. The server independently checks the expected digest,
   reconstructs disposable storage and reassesses the retained chain.
5. Inspect **Bundle diagnostic details**, including the selected task/record references,
   artifact counts, check time and fixed blockers. **Save assessment** downloads
   `orchd-bundle-assessment.json` for review.

An intact bundle can produce `claims_consistent` or `blocked`; for example, policy may
have expired since export. Both produce a diagnostic report. Changed bytes, malformed
bundles, incomplete dependencies and exceeded budgets reject verification without a
partial report. A matching digest identifies bytes; it does not authenticate their
author or make their claims execution authority. The saved report is a point-in-time
assessment, not an approval or an input that enables execution.

Changing the file or expected digest clears the prior result and cancels the browser
request. **Clear or cancel review** also clears both inputs. Late responses cannot
restore a cleared result. Cancellation suppresses the browser result; server-side work
already started may finish its bounded verification and cleanup. Task selection is
independent of this review. Disconnecting reloads the page and clears its memory.

## Upload boundary and lifecycle

`POST /evidence-bundles/verify` accepts raw bytes with `Content-Type:
application/octet-stream`, an exact positive `Content-Length` no greater than
16,777,216, and `X-Orch-Expected-SHA256`. The existing operator bearer session and
loopback Host/Origin rules apply. Authentication and upload framing are checked before
reading the body. Duplicate authorization, length, type or digest headers, transfer
encoding, content encoding, missing/invalid digests and invalid lengths are rejected.
The existing five-second socket inactivity timeout applies to body reads.

The route accepts no task ID, destination path, JSON wrapper or query parameters.
Verification checks the digest before allocating scratch storage. The upload uses a
fixed filename inside a unique temporary directory; the existing verifier creates its
own bounded, query-only reconstruction. Both directories are removed after success or
exception. They are separate from the operator's workflow store. The single-request
local HTTP server serializes verification; a large review can delay other local API
requests. A process crash can leave OS temporary files, so this is not secure erasure
or a guarantee of cleanup after process termination.

Success and blocked diagnostics return HTTP 200 with `GitHubEvidenceBundleAssessment`.
Rejected bundles return a generic 409 JSON error; invalid framing returns 400 and a
missing/invalid session returns 401. Responses retain no-store and the existing browser
security headers. No artifact bodies are returned in the diagnostic report.

Validation covers verification without a source store, expired claims, scratch cleanup,
pre-allocation rejection, authentication before body reads, duplicate framing headers,
size/digest guards, stale and cancelled browser requests, and a real browser handoff.
Restart the local API after upgrading to load the new endpoint.
