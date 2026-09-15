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

## Rotating the shared secret

The secret file may hold **two** secrets, one per line: the current one first and the
previous one below it. Both are accepted, and a reply is signed with whichever secret
authenticated its request, so a process that has not yet re-read the file still
verifies its own answer. Both the service and every client re-read the file whenever
its identity, size or modification time changes, so rotation needs no restart and
drops no operation in flight. A file that becomes unreadable or is withdrawn refuses
the request (`503`) rather than reusing a secret the operator has just taken away.

```text
python -m orch broker-rotate-secret --broker-secret /etc/orchd/broker.secret
python -m orch broker-rotate-secret --broker-secret /etc/orchd/broker.secret --complete
```

The first stage writes a fresh current secret above the previous one; the second drops
the previous one once every process has re-read the file. Each stage rewrites the file
atomically with owner-only permissions and prints a report that never contains a
secret. Beginning a rotation twice, or completing one that has not begun, is refused.
Whoever runs these commands must be able to write the file, which the broker account
owns; the orchestrator account does not need write access.

`broker-check` reports `accepted`, `rotating`, `age_seconds` and `stale` for the
secret, against a documented 30-day maximum age. Age is **reported, never enforced**:
an old secret keeps working, and the deployment gate is what stays blocked. A broker
that is otherwise separate but mid-rotation or past that age reports
`secret_attention` rather than `separate`, and `deployment-check` keeps
`broker_identity` blocked with that reason. Identity separation is still reported
first: a broker sharing the orchestrator's account reports `shared` whatever the
secret's state.

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

## Pilot Docker workers through the broker

Start the broker with `--image IMAGE@sha256:DIGEST --pilot-root JOURNAL` and give the
pilot commands (or the task kernel's `serve`/`repository-task-*` commands) the same
`--broker-endpoint`/`--broker-secret`. The [Git JSON pilot](repository-pilot.md) then
never runs Docker itself: three more routes carry its fixed-recipe workers.

| Route | Effect |
| --- | --- |
| `POST /pilot-profile` | The runtime the approval binds to: daemon, image identity, recipe hash **and the broker account**. |
| `POST /pilot-execute` | Run one edit/test phase for a scope prepared for this broker; a second identical delivery returns the retained observation, a different scope for the same pilot id is a conflict. |
| `POST /pilot-reconcile` | Ownership-checked container removal for one phase. |

A `--pilot-root` naming a [retired journal](pilot-journal-succession.md) is refused at
startup, with the successor named: the broker's root is its own policy boundary and is
never moved by a successor link the orchestrator wrote.
The broker refuses a scope whose journal root is not its configured `--pilot-root`,
whose image is not its own, whose executor names another broker account, or whose
workspace is not `PILOT_ROOT/<pilot id>`. Because the broker identity is part of the
scope's `executor`, a scope prepared through the service cannot be run or reconciled
directly, and a directly prepared scope cannot be run through the service. Every
retained worker observation records `transport` and the broker account, and the
receipt binds that journal. Local-data pilots launch nothing and ignore the service.
The pilot routes accept bodies up to 1 MiB because the scope carries the baseline.

`scripts/pilot_acceptance.py --image IMAGE --broker` runs the real Docker walkthrough
through a real `broker-serve` process and is a step of the Docker CI lane.

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

`tests/test_broker_secret.py` covers the accepted file formats, live re-read, a
withdrawn file, the overlap window, a client pinned to the previous secret verifying
its own reply, a rotation completed during a call, both rotation stages and their
out-of-order refusals, file permissions, and the reported age and deployment gate.
`tests/test_pilot_broker.py` covers pilot workers launched from the service thread with
the broker identity bound into scope and observations, service/direct scope cross-refusal,
retained redelivery and foreign-scope refusal, broker crash after launch with
reconciliation through the broker, unavailable or changed broker before launch, the
task-kernel path and CLI threading.
`tests/test_broker_service.py` covers the complete workflow through a threaded service,
unauthenticated/tampered/oversized/cross-origin requests, profile and workspace
policy, idempotent redelivery, the read-only result route, crash before the journal
(fixture and simulated Docker), forged replies, secret/endpoint guards, identity
assessment and deployment gating, restart with audit tamper detection, subprocess
identity rows, and the real CLI. `scripts/broker_service_acceptance.py` runs the real
`broker-serve` process, `broker-check`, a completed task, a full secret rotation against
the running service, an injected orchestrator crash after the broker commit, broker and
orchestrator restarts and recovery without relaunch. It runs in the offline and full
release lanes.

## Not done

- The acceptance and CI hosts run both processes as **one** account, so their evidence
  says `shared`. A real `separate` assessment requires the provisioning above on a
  deployment host; nothing in this repository can create OS accounts.
- The pilot's `pilot-profile` route calls the Docker daemon for identity on every
  preparation, run and dispatch check, exactly as the direct path did.
- No TLS, per-request authorization beyond fixed recipes, Windows ACL verification,
  service supervision or multiple brokers. One secret file, one service. Rotation is an
  operator action on that file; nothing schedules or enforces it, and a stale secret
  blocks only the deployment gate.
- Live providers, GitHub and Jira remain gated exactly as before; a separate broker
  account is one deployment gate among several, not admission.
