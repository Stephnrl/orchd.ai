# Broker service under a separate OS identity

The fixture action broker used to be a subprocess of the orchestrator: same OS account,
same Docker access, and a journal inside the workflow store that the orchestrator could
read and rewrite. This milestone adds `broker-serve`, a loopback HTTP service that the
operator starts under a **different OS account**, and a `--broker-endpoint`/`--broker-secret`
transport that makes the orchestrator dispatch every fixture edit/test through it. The
subprocess transport remains the default and is unchanged.

```text
python -m orch broker-serve --broker-root /var/lib/orchd-broker --broker-secret /etc/orchd/broker.secret --workspaces /var/lib/orchd/store/workspaces --trusted-fixture --port 8091
python -m orch broker-check --broker-endpoint 127.0.0.1:8091 --broker-secret /etc/orchd/broker.secret
python -m orch serve --trusted-fixture --data /var/lib/orchd/store --broker-endpoint 127.0.0.1:8091 --broker-secret /etc/orchd/broker.secret
```

Use `--image IMAGE@sha256:DIGEST` instead of `--trusted-fixture` on both sides for Docker
execution. The image and mode are the **broker's** startup configuration; a request that
names a different profile is refused, so the orchestrator cannot choose the image, mount,
recipe text or a shell string. Only the five routes below exist.

## What the broker owns

- **Its journal directory** (`--broker-root`): per-operation `.json` envelopes, `.lock`
  files and `.retired` tombstones, written atomically under an OS file lock exactly as
  before. The orchestrator never reads this directory; results are served back over the
  authenticated reply and persisted as the existing `execution_provenance` artifact.
- **The execution profile**: `trusted-fixture` or one digest-pinned Docker image.
- **The workspace parent** (`--workspaces`): a request's workspace must resolve to exactly
  `WORKSPACES/<operation_id>`; anything else is an unassigned workspace.
- **Docker authority**: container reconciliation and the durable retire tombstone happen
  inside the service. With the service transport the orchestrator process never calls
  Docker for fixture operations; the tests assert this.

## Routes and authentication

| Route | Effect |
| --- | --- |
| `POST /identity` | Nonce-bound report of the broker account, source digest and profile. No side effect. |
| `POST /execute` | Run the closed request once. An identical request returns the retained envelope; a different request for the same operation, or a retired generation, is refused. |
| `POST /result` | Return the retained envelope for exactly this request or `404`. Never executes. |
| `POST /retire` | Under the operation lock, refuse if a journal exists, else write the tombstone and reconcile the labelled container. |
| `POST /reconcile` | Reconcile the labelled container only. |

Every request body is canonical JSON bound to the [broker wire schema](../contracts/broker-v1.schema.json)
(new `Identity`, `Profile`, `*Request`, `*Reply` and `ExecutionIdentity` definitions) and
carries `X-Orch-Broker-Auth`, an HMAC-SHA256 over the route and exact body bytes keyed by
the shared secret. The service verifies the HMAC **before** parsing JSON, requires exactly
one `Host: 127.0.0.1:PORT`, refuses `Origin`, transfer/content encodings and bodies over
16 KiB, and answers only on 127.0.0.1. Replies are signed under a distinct domain so a
request tag cannot be replayed as a reply, and every reply is bound to its request nonce
or request digest. A tampered or forged reply is `BrokerUncertain` for the orchestrator:
the task blocks and nothing is recorded until operator recovery reads the retained result.

The secret file must hold 64 lowercase hex characters, be a regular unlinked file, and on
POSIX must not be world-accessible. Windows ACLs are not verified by the code; provision
them explicitly. Compromise of the secret lets a local process submit fixed-recipe
executions for assigned workspaces; it does not yield a shell, another image or the
broker journal.

## Identity evidence

Every accepted execution now records a `broker_identities` row (append-only, digest
bound, audited by `integrity_report` and backups) with the transport, the broker
account, the orchestrator account and `separate_identity`. Accounts are stored as
SHA-256 of the account name plus POSIX uid/gid; environment variables are ignored.
The subprocess transport records `separate_identity: false` by construction.

`broker-check` prints a `BrokerIdentityAssessment` and exits `0` only when the broker
answers with the correct secret, reports a different account (and uid, where present)
on the same kind of host, and runs the same broker source digest. Any other outcome
exits `2`. `deployment-check` gains a `broker_identity` gate that stays blocked unless
`--broker-endpoint` and `--broker-secret` are supplied and that assessment is `separate`.
The audit also rejects a stored `separate_identity` claim that does not follow from its
two account reports.

## Provisioning the second account

Linux: create a system user such as `orchd-broker`, add it to `docker` if Docker mode
is used, create the journal directory owned by it, make the secret readable by both
accounts only (for example owner `orchd-broker`, group shared with the orchestrator
account, mode `0640`), and make the orchestrator's `workspaces` directory traversable
and, for `trusted-fixture`, writable by the broker account. Windows: create a separate
local account in `docker-users`, grant it and the operator read access to the secret
with `icacls`, and start `broker-serve` as that account (scheduled task or `runas`).
The orchestrator must not be able to write the broker journal directory.

## Recovery semantics

The workflow semantics are unchanged. `recover` first asks `/result`; an existing
envelope for the exact durable request is accepted as the verified completion without
relaunch. Without one, Docker mode calls `/retire` (tombstone under the broker lock,
then proven container absence) and the attempt is retired as `stopped_without_receipt`;
local fixture mode still cannot prove an unknown process exited and stays blocked. A
retired generation can never be executed late, even with the exact durable request.
A crash between the broker's journal commit and the orchestrator's acceptance is
repaired by re-reading that journal, across broker and orchestrator restarts.

## Verification

`tests/test_broker_service.py` covers the complete workflow through a threaded service,
unauthenticated/tampered/oversized/cross-origin requests, profile and workspace
policy, idempotent redelivery, the read-only result route, crash before the journal
(fixture and simulated Docker), forged replies, secret/endpoint guards, identity
assessment and deployment gating, restart with audit tamper detection, subprocess
identity rows, and the real CLI. `scripts/broker_service_acceptance.py` runs the real
`broker-serve` process, `broker-check`, a completed task, an injected orchestrator crash
after the broker commit, broker and orchestrator restarts and recovery without
relaunch. It runs in the offline and full release lanes.

## Not done

- The acceptance and CI hosts run both processes as **one** account, so their evidence
  says `shared`. A real `separate` assessment requires the provisioning above on a
  deployment host; nothing in this repository can create OS accounts.
- The Git JSON pilot's Docker workers (`pilot-run --pilot-executor docker`) still call
  Docker from the operator's process; routing them through the service is a later step.
- No TLS, per-request authorization beyond fixed recipes, secret rotation, Windows ACL
  verification, service supervision or multiple brokers. One shared secret, one service.
- Live providers, GitHub and Jira remain gated exactly as before; a separate broker
  account is one deployment gate among several, not admission.
