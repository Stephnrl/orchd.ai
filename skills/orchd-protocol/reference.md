# orchd protocol reference

Read this when `SKILL.md` was not enough: when you need the exact shape of a reply, when a
refusal was not self-explanatory, or when you are deciding whether a task is yours at all.

## Which role a state belongs to

A task sits in one state, and each state has exactly one role whose turn it is. Claiming
requires that your role is that role. Terminal states belong to nobody and cannot be claimed.

| State | Role whose turn it is |
| --- | --- |
| `DRAFT_SPEC` | `project_manager` |
| `AWAITING_CLARIFICATION` | `project_manager` |
| `SPEC_READY` | `lead_planner` |
| `PLANNING` | `lead_planner` |
| `AWAITING_PLAN_APPROVAL` | `human` |
| `READY_FOR_IMPLEMENTATION` | `junior` |
| `CHANGES_REQUESTED` | `junior` |
| `IMPLEMENTING` | `junior` |
| `TESTING` | `test_runner` |
| `REVIEWING` | `reviewer` |
| `AWAITING_ACTION_APPROVAL` | `human` |
| `PR_CREATED` | `action_broker` |
| `BLOCKED` | `human` |

Three roles appear in no state: `guardian` decides tool requests, `orchestrator` moves work
between steps, and `lead_clarifier` resolves ambiguity in a spec. An agent enrolled only for
those does not claim tasks by state.

A state whose role is `human` is a pause with a person on the other side of it. It is not a
step you may take by claiming a different role, and it is not a state you should wait in a
loop for. Report and end your turn.

## Leases

| | Seconds |
| --- | --- |
| Shortest lease | 60 |
| Default when you ask for none | 900 |
| Longest lease | 3600 |

Ask for roughly the time your next unit of work takes, and renew rather than asking for the
maximum. A long lease is not safer: if your container dies holding one, the task stays
unavailable for the rest of it. A renewal restarts the clock from now; it does not extend
from the old expiry.

A lease is not a lock. It says the work is yours for a while. It does not stop the task from
changing underneath you, which is why a claim can carry `expected_revision` — pass the
revision you last read, and the claim is refused if the task moved since.

## The routes

Every route is a POST to `127.0.0.1` on the endpoint your container was given, authenticated
with an HMAC over the body. The CLI below does all of this for you; the wire detail matters
only if you are implementing a client.

| Route | You send | You get back |
| --- | --- | --- |
| `agent-identity` | nothing | `agent`, `roles`, `store_sha256` |
| `agent-claim` | `task_id`, `role`, optional `lease`, optional `expected_revision` | `status`, `session` |
| `agent-renew` | `task_id`, optional `lease` | `status`, `session` |
| `agent-release` | `task_id` | `status`, `task_id`, `agent`, `released_at` |
| `agent-sessions` | nothing | `sessions`, `held` |
| `agent-spec` | `task_id` | `draft` |
| `agent-draft` | `task_id`, `specification`, optional `expected_revision` | `draft` |
| `agent-ask` | `task_id`, `questions`, optional `expected_revision` | `draft` |

The last three are work rather than bookkeeping, and every one of them requires you to be
**holding** the task. A lease is only granted for the role that task's next step needs, so
holding one is what says the work is yours to do. A `draft` tells you the state, the task
revision, the specification's `spec_sha256` and any `pending_questions`.

Every reply also carries `retry_allowed` and `live_authorized`. A `session` describes one
held task: `task_id`, `agent`, `role`, `kind`, `claimed_at`, `expires_at`, `revision`,
`generation` and `live`. Its `kind` is `agent` when another container holds it and `worker`
when a process on the host does; a worker names no agent and expires at no time, because it
holds a lock rather than a lease and releases it by exiting.

`live_authorized` is always false today. It does not mean your request failed; it means this
control plane has not been cleared to drive a live provider, and you should not read any
reply as authorization to run one.

Start with `agent-identity` when your container starts. It tells you the name you are known
by and the roles you may claim, and it is the cheapest way to confirm your secret works
before there is a task on the line.

## When you are refused

| What it means | Retry helps? |
| --- | --- |
| You asked for a role you are not enrolled for | No. Ask for one of the roles `agent-identity` returned. |
| The task's next step belongs to another role | No. The task is not yours right now. |
| The task is held by someone else, still live | Later. It frees when their lease expires or they release it. |
| Too many tasks are already advancing | Later. A slot frees when any holder finishes. |
| The task moved since the revision you passed | No. Read the task again and decide afresh. |
| The service is unreachable or your secret failed | No. Report it; this is a deployment problem, not a work problem. |

The reply's `retry_allowed` is the authoritative version of this table. Where they disagree,
believe the reply.

"Later" never means immediately. Wait out the lease or the work ahead of you; a tight retry
loop against a live holder accomplishes nothing and hides a real stall from whoever is
watching.

## A worked sequence

```sh
orchd() { python -m orch "$@" --agent-endpoint "$ORCHD_ENDPOINT" --agent-secret "$ORCHD_SECRET"; }

orchd agent-sessions                                        # is this task already held?
orchd agent-claim --task T-7 --role junior --lease 600      # claim it
# ... work, renewing before 600s elapses ...
orchd agent-renew --task T-7 --lease 600
orchd agent-release --task T-7                              # always, success or not
```

Each command prints one JSON object and exits non-zero when refused. Read the object; the
exit code alone does not say which of the refusals above you hit.

## Limits worth knowing

One service serves at most 32 agents, each enrolled for at most 10 roles. Your secret file
must be private to your container — on Linux, mode `600`; a world-readable one is refused at
startup and names the agent it belongs to. Secrets can be rotated while the service runs, so
read the file each time you sign rather than caching its contents for the life of your
process.
