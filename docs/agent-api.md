# The loopback agent API

[Agent sessions](agent-sessions.md) gave an agent a role-scoped lease, but an agent holding
one needed the control plane installed in its image and the store mounted into its
container. A junior container could therefore claim as a lead planner, or skip the check
altogether and write the evidence store directly. The role gate was a convention between
cooperating processes, which is not what a gate is for.

This service makes it enforced. **The agent a request comes from is the secret that signed
it**, never a field in the body, and the roles that agent may claim come from this service's
configuration rather than from the caller. The service holds the store; an agent holds a
secret and a socket.

```text
python -m orch agent-serve --data STORE --agents REGISTRY --port 0
python -m orch agent-claim --agent-endpoint 127.0.0.1:PORT --agent-secret SECRET \
    --task TASK --role lead_planner --lease 900
```

Note what the second command does not take: a store path, an agent name, or any way to say
who it is. A container needs the endpoint and its own secret file.

## The registry

One directory per agent, holding two files:

```text
agents/
  lead-devops/   role   -> "lead_planner"
                 secret -> 64 hex characters, or two lines mid-rotation
  junior-devops/ role   -> "junior"
                 secret -> ...
```

Each secret must be a small, unlinked, **private** file: on POSIX the service refuses one
that is world-accessible, and says which agent it belongs to rather than failing later at a
request. Create them with `chmod 600`.

Roles are read once at startup because they are policy: changing what an agent may claim is
a deliberate act that restarts the service. Secrets are re-read live, so
[rotation](broker-service.md) works exactly as it does for the broker — write the new secret
above the old one, let every container pick it up, then drop the old one, with no restart
and no dropped request.

An agent may be enrolled for more than one role, which is how a lead that also reviews is
expressed. At most 32 agents and 10 roles each.

## Two gates, not one

A claim passes only if **both** hold:

1. The authenticated agent is enrolled for the role it asks for — configuration, checked here.
2. That role is the one the task's next step belongs to — the [session rule](agent-sessions.md),
   checked in the kernel.

So a junior container cannot ask for `lead_planner` (gate 1), and cannot take planner work by
asking as `junior` either (gate 2). Neither gate is expressible in a request body, which is
the point.

## The wire

Twelve fixed POST routes on 127.0.0.1, each with a request and reply contract in
`contracts/agent-v1.schema.json`:

| Group | Routes |
| --- | --- |
| Who am I | `agent-identity` |
| Holding a task | `agent-claim`, `agent-renew`, `agent-release`, `agent-sessions` |
| Finding work | `agent-available` |
| Doing the work | `agent-spec`, `agent-draft`, `agent-ask` |
| [Questions](consultations.md) | `agent-consultations`, `agent-consult-take`, `agent-consult-answer` |

The work routes require a live lease on the task named, and the question routes require
nothing but a role: a consultation is not a task, holds nothing, and changes nothing. The service checks the Host header, refuses `Origin`,
`Transfer-Encoding` and `Content-Encoding`, bounds the body, and **verifies the HMAC before
parsing any JSON**. Replies are signed with the secret that authenticated the request, so an
agent mid-rotation can still verify its own answer, and each reply carries the nonce of the
request it answers and a `kind` naming its own route, so no reply can be replayed as another.

Every agent's accepted secrets are tried without short-circuiting, so a wrong secret costs
what a right one does.

Each request opens the store, takes the same locks a worker would, and closes it again. The
service holds no connection between calls: a SQLite connection belongs to the thread that
made it, and a network service has no business keeping a writable one open anyway.

## What it still is not

The service authenticates **which agent** is calling and enforces **which roles** it may
claim. It does not decide what that agent may then do: approvals still gate effects, the
guardian still decides tool requests, the broker still records what really ran, and every
fence on scope, revision and receipts is unchanged. Nothing here dispatches work, executes
anything, or authorizes a live action.

Secrets are files on one host, readable by the account that runs each container. That is the
same trust model the [broker](broker-service.md) uses, and it is not multi-user
authentication, corporate identity or a credential vault. An operator working directly on the
machine still opens the store with the CLI, as they always could — enforcement here is about
what an *agent container* can do, not about locking the operator out of their own store.
