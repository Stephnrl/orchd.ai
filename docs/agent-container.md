# The loop, and the container that runs it

Everything before this spoke to [the agent API](agent-api.md) as a **scripted sequence** — a
test or an acceptance walkthrough that knew which call came next. The protocol stated rules
that nothing had ever obeyed: wake with no instruction and decide what to do, renew a lease
*while* working, back off when the bound is full, release on shutdown, treat a refusal as
final.

`orch/agent_runner.py` runs that loop, and `containers/agent.Dockerfile` puts it in a
container.

## What the loop does and does not decide

It decides everything about **working**: what to claim, when to renew, when to wait, when to
hand a task back. It decides nothing about **the work**. A `Decider` supplies that:

```python
("draft", {specification fields})   # write the next revision
("ask", [questions])                # stop; a person must answer
("release", None)                   # nothing more to do here
```

Raising `Rejected` from a decider is also an answer: this agent cannot do this task, and the
loop hands it back rather than holding it while failing. A decider that answers anything else
has a bug, and the loop says so rather than quietly treating it as a release.

The default decider does nothing at all. That is deliberate: a container can be started and
watched before there is an intelligence behind it, exercising claiming, renewing and releasing
without writing a specification nobody meant.

## Three things a naive loop would get wrong

**It renews before working, not after.** Work that runs longer than expected must not be the
reason a lease lapses mid-write. The schedule comes from the lease rather than a fixed timer.

**It releases on shutdown.** A container powered off mid-task hands the task back immediately
rather than leaving it stuck for the rest of a lease nobody is using — and the stop signal is
checked between every step, not only between tasks. Being thrown out by an exception releases
it too.

**It tells a refusal from an outage.** Being told no and not knowing are different answers.
An unreachable service means wait and ask again; a refusal means the answer is no.

## The gap writing this found

That last distinction is where the protocol turned out to be wrong.

The published reference said `retry_allowed` was "the authoritative version" of the refusal
table — and that field appeared only on **successful** replies, where it is always false. A
refusal came back as a bare `409 {"error": "agent_refused"}`. So the protocol instructed an
agent to consult an authority that did not exist at the one moment it was needed, and the
first client had to guess.

Refusals now carry it. A `Transient` refusal is one that may stop being a refusal without
anybody doing anything — the task frees, the bound empties — and it is distinct from "your
role may not do this", which will never change. The service reports the difference, the client
raises the difference, and the loop acts on it: a transient refusal on a write keeps the task
and tries the same step again, while a final one hands it back.

It says only that much. A refusal never says *which* agent holds what.

## The container

```sh
docker build -f containers/agent.Dockerfile -t orchd-agent .
docker run --rm --network host \
    -v /etc/orchd/agents/project-manager/secret:/run/secrets/agent:ro \
    -e ORCHD_ENDPOINT=127.0.0.1:PORT -e ORCHD_ROLE=project_manager orchd-agent
```

It is given an endpoint, a role, and its own secret mounted read-only. **It is never given a
store path and never given a credential for anything outside this host** — those live on
[the broker](outbound-dispatch.md), under its own account, because a container holding one can
act whenever it likes while the evidence records a single approved call.

It runs as a non-root user that cannot write to its own image, and it has no `HEALTHCHECK`
that talks to the control plane: a container reporting itself unhealthy because the service
restarted would be restarted in turn, and two things restarting each other is worse than one
of them waiting.

`ORCHD_DECIDER` is a `module:attribute` path. It is checked before the secret file is opened,
so a misspelled import is reported as one rather than as a missing secret.

## What running it actually found

Four things, none of which any offline test would have produced.

**The image did not build.** `pip install --require-hashes=false` is not valid: it is a
boolean flag with no value.

**A bind-mounted secret cannot satisfy the permission check on Docker Desktop.** A file
mounted from Windows or macOS arrives as mode `777` inside the container whatever the host
file says, because those filesystems carry no POSIX modes, and `read_secrets` correctly
refuses it. The check is right — on a Linux host a world-accessible secret genuinely is one.
Where the host cannot provide that property at all, `ORCHD_HOST_CANNOT_PROTECT_SECRETS=1`
copies the secret into a private directory inside the container so the reader is satisfied,
and says out loud that the file on the host is still exposed. It makes the container work; it
does not make the host safe, and the message says so every time.

**A container cannot reach a loopback service on Docker Desktop.** `--network host` gives a
container the network namespace of the Linux VM, not of Windows, so `127.0.0.1:PORT` inside
the container is not the host's. The agent endpoint is loopback-only by design, which leaves
two deployments that work:

| | |
| --- | --- |
| Linux host | `--network host`; the container shares the real loopback |
| Docker Desktop | `--network container:<service>`; the agent joins the service's namespace |

The second is how the loop was proven here. Anything else would mean binding the agent
service somewhere other than loopback, which is a decision about exposure and not one to make
by accident.

**A container that started before the control plane simply died.** It now says so and waits,
because the loop already knows how to wait out an outage and being early is one.

## A caution about proving it yourself

`timeout N docker run ...` kills the *client*, not the container. Every run left behind a
container that carried on holding leases, which is why a later listing showed no work
available. Use `docker run -d` and `docker stop`, which is also the only way to test the
shutdown path, since `docker stop` is what delivers SIGTERM to PID 1.

## What this is not

A fixture decider is not an intelligence. This proves the **loop** is followable, not that an
agent can think — everything downstream of a confirmed specification is still the offline
fixture, and no provider is admitted. Swapping in Copilot or a corporate CLI is a separate
step behind its own gates.

Nothing here changes what an agent may do. The role gate, the holding requirement, the
admission bound and the human boundary are all exactly as they were; this is the first thing
that has had to live within them.
