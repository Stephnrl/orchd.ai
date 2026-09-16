# Outbound dispatch

Everything else in this control plane prepares request data and refuses to pretend it sent
anything — [GitHub issues](github-issues.md), [pull requests](github-preview.md), [Jira
actions](jira-integration.md) all stop at "here is exactly what would be sent". This is the
one path that sends it.

It lives on the broker, and that placement is the whole design.

## Why the broker holds the credential and no agent does

The obvious thing is to mount a token into the project manager's container so it can file
its own issues. That would undo everything upstream of it.

An agent container that holds a GitHub token can use it whenever it likes, for anything the
token permits. The evidence would record *"approved: create issue #12"* while the container
could have made fifty other calls that no record mentions. Every approval, every fence,
every receipt above it becomes decorative — not wrong, just no longer load-bearing.

So the boundary is: **an agent holds a secret that proves which agent it is and grants
nothing.** That is what [the agent API](agent-api.md) issues. The broker — already running
under its own OS account for exactly this reason — holds what can change the world.

```text
agent ──asks──> orchd ──human approves──> broker ──sends──> GitHub
 (identity)                (fenced intent)   (credential)
```

For the ordered path from a token to a created issue on a host that can reach these
services, see [the live integration runbook](live-integration-runbook.md).

## The credential file

Two lines, mode `600`, readable only by the broker account:

```text
origin=https://api.github.com
token=<the token>
```

```sh
python -m orch broker-serve --broker-root ... --broker-secret ... --workspaces ... \
    --dispatch-credential /etc/orchd/github.credential
```

**The origin is not decoration.** A profile is operator-supplied and could name any
destination; binding the credential to an origin means a misdirected profile cannot carry a
live token to a host that was never meant to see it. A credential is refused outright if its
origin is plain `http` and not loopback — a token over cleartext to a real host is a token
on the wire.

An unusable credential stops the broker at startup, naming the problem, rather than becoming
a puzzle at the first dispatch. The file is re-read on every request, so rotating a token
needs no restart and withdrawing one takes effect immediately.

## Reads, which nothing could perform before

Every codec here prepares a **read plan** naming exactly what must be observed before acting.
Until now nothing could perform one: a preview needed a person to fetch each URL by hand and
paste the JSON back. The `fetch` route performs them, and returns the capture in exactly the
shape the codec that asked for them expects.

```sh
python -m orch github-issue-read-plan --github-profile profile.json --intent intent.json > plan.json
python -m orch broker-fetch --plan plan.json --broker-endpoint 127.0.0.1:PORT --broker-secret ... > capture.json
python -m orch github-issue-preview --github-profile profile.json --intent intent.json --transcript capture.json
```

A fetch needs no approval, because nothing it does changes anything and nothing it does can
need undoing. Its fence is narrower for the same reason it can be: **only GETs, only to the
credential's origin**, at most eight of them, each bounded and parsed as strict JSON with no
duplicate keys. A write smuggled in among the reads is refused before any of them run.

The capture carries the plan hash it was asked for. The broker cannot check that hash — it
does not hold the plan — but the codec that does refuses a capture whose reads or hash do not
match the plan it asked, so a capture cannot be passed off as answering a different question.
A redirect is reported as `redirected: true` rather than chased, and every codec refuses it.

## What may be sent

```sh
# through the authenticated broker route, after approval
{"kind": "dispatch", "operation_id": "...", "expected_sha256": "...", "request": {...}}
```

Four things are checked before a single byte leaves, and each refusal happens *before*
sending, so nothing happened and preparing it again is safe:

1. **The request hashes to `expected_sha256`** — the hash the approval was granted over. What
   a human approved and what leaves the machine are the same bytes, and the receipt names the
   hash so anyone can check it afterwards.
2. **Its origin is the credential's origin.** Otherwise: *"This credential is not for
   https://elsewhere"*.
3. **It carries no `Authorization`, `Cookie` or `Proxy-Authorization` of its own.** The broker
   adds authentication; a request bringing its own is either confused about where credentials
   come from or trying to smuggle one past this boundary.
4. **It is a write** — `POST`, `PATCH` or `PUT` — and contains no recognised secret.

## Redirects are never followed

A 3xx is recorded with `error: "redirect_not_followed"`, the origin it pointed at, and
nothing more. Following one would hand the `Authorization` header to whatever the redirect
named, which is the classic way a scoped token stops being scoped.

## Refused, failed, and uncertain are three different answers

| Outcome | Meaning |
| --- | --- |
| *(raises)* | Refused before sending. Nothing happened; preparing it again is safe |
| `failed` | It was sent and the service said no — a `422`, a `403`, a redirect |
| `uncertain` | The bytes went and the answer did not come back |

`uncertain` is the one that matters. A timeout, a reset connection, or a response too large
to read all mean the effect **may exist**, and retrying blindly is how duplicates are made.
That is what reconciliation is for, and for GitHub issues it already works: every issue
carries an [operation marker](github-issues.md#the-operation-marker), so
`github-issue-reconcile` can find the one that operation created.

Every outcome after the bytes go is **returned as a receipt, never raised**. `Rejected` is a
`ValueError` in this codebase, and a caller catching it broadly would throw away the only
record saying an effect may exist.

## The receipt

A `DispatchReceipt` names the operation, the request hash, the origin, method and URL, the
outcome, the response status and body (bounded and redacted), and the times either side. It
says `simulated: false` — the only receipt in this control plane that may. The token appears
nowhere in it, and there is a test asserting exactly that.

## The gate

`deployment-check` reports an `outbound_dispatch` gate, which passes only when **both** hold:
the broker was given a dispatch credential, and it runs under a separate OS account. The
second is not a formality. A credential file on the orchestrator's own account is readable by
everything the orchestrator runs, which puts it back within reach of the processes this
design exists to keep it away from.

`dispatch.py` is also part of the broker's source digest, so a broker whose outbound path
changed no longer reports as the same broker.

## The loop, end to end

```text
codec ──read plan──> broker fetch ──capture──> codec preview ──approval──> broker dispatch
        (what to        (GETs, one              (checks the                 (the approved
         observe)        credential)             capture)                    hash only)
```

`tests/test_dispatch.py` walks exactly that against a local origin, with both the reads and
the write crossing a real socket, and asserts that exactly one write happens and only after
the reads.

## What this is not

This performs a request. It does not decide that the request should be made: approvals,
role gates, the guardian and every fence on scope and revision are unchanged and all sit
above it. Nothing here grants an agent anything.

It is also not yet wired into the task workflow. `engine.py` still requires
`ExternalActionReceipt.simulated` to be true for a task to reach `PR_CREATED`, and admitting
a real receipt there is a deliberate separate step. Today the route exists, is authenticated,
is fenced, and is reachable by an operator who has configured a credential — which is what
the `outbound_dispatch` gate reports.
