# Running the agents from the window

The window could see agents and not run them. You enrolled one, then went to a terminal and
typed a `docker run` line with four flags you had to get right, and if you got the network
mode wrong the container sat there unable to reach anything. The window is supposed to be
where a day starts — click an agent, click Run — so this is the part that was missing.

It is also the part that could have been a hole, and the design is mostly about that.

## The one thing a request decides

A window that composes a command from what a browser sends is a window that runs whatever a
browser sends. So nothing here takes a command, an image, a path, an environment variable or a
flag from a request. **A request contributes exactly one thing: which enrolled agent to act
on.** Everything else comes from a file on the host.

```
POST /agents/lead-devops/start     ← the name, and nothing else. No payload is accepted.
POST /agents/lead-devops/stop
GET  /agents                       ← what is declared, and what Docker says is running
```

`tests/test_agent_processes.py` asserts the route refuses *any* payload — `{"image": ...}`,
`{"command": ...}`, `{"network": ...}` — and that a name which is not an agent name never
reaches the daemon at all.

## What an agent may run is declared on the host

Beside its role and its secret: `agents/<name>/container.json`, written by the operator with
`agent-declare` or by hand.

```json
{
  "image": "sha256:47198c3aeea221f22bbd1857af57e8ffc059ca1dbf9ff610fe550fc5f6ef5132",
  "endpoint": "127.0.0.1:8721",
  "role": "lead_planner",
  "decider": "orch.agent_main:Idle",
  "lease": 900,
  "network": "host",
  "memory": "512m",
  "host_cannot_protect_secrets": false,
  "resolved_from": "orchd-agent:latest"
}
```

Every field has a grammar: a digest-pinned image or a local image id, a loopback endpoint, a
role that agent is **enrolled for**, a `module:attribute` decider, a lease inside the same
bounds a claim uses, one of the two network modes that actually work, and a memory ceiling.

**The set of fields is closed.** A declaration carrying something this does not know about is
refused rather than ignored, because a field nobody reads is how a setting quietly stops
taking effect — and because `"command"`, `"privileged"` and `"volumes"` are exactly the fields
somebody would add later if ignoring them were free.

Two things are deliberately **not** declarable:

- **The secret to mount.** It is derived from the registry — `agents/<name>/secret` — because
  a declaration that can name any host file is a way to read any host file through a container.
- **The command.** The image's entry point is the agent loop, and nothing follows the image in
  the argv. A declaration that could replace it is a declaration that could run anything.

## Pinning an image you built yourself

`agent-declare --image orchd-agent:latest` records the **bytes** that tag pointed at when you
declared it, not the tag:

```sh
python -m orch agent-declare --agents agents --agent lead-devops \
    --image orchd-agent:latest --agent-endpoint 127.0.0.1:8721 --role lead_planner
```

A tag moves the next time you build. Resolving it once, at declaration time, means the
container that starts in three weeks is the one you meant, and `resolved_from` records which
tag it came from so the file still says something human in six months.

Both ways of naming exact bytes are accepted: `name@sha256:...` for an image from a registry,
and a bare `sha256:...` image id for one built here — an image built locally has no registry
digest until it is pushed, and requiring one would mean this could not be used with the image
in this repository. A plain tag in the file is refused; it is only accepted by the command
that pins it.

## The argv

A fixed template, the same shape the [task executor](../orch/execution.py) uses — no shell, no
interpolation into a string:

```sh
docker run --detach --pull=never \
  --name orchd-agent-lead-devops --label ai.orchd.agent=lead-devops --restart=no \
  --read-only --cap-drop=ALL --security-opt=no-new-privileges \
  --pids-limit=256 --memory=512m --tmpfs=/tmp:rw,noexec,nosuid,size=16m \
  --log-opt=max-size=10m --log-opt=max-file=3 --network=host \
  --mount type=bind,source=<registry>/lead-devops/secret,target=/run/secrets/agent,readonly \
  --env ORCHD_ENDPOINT=... --env ORCHD_ROLE=... --env ORCHD_DECIDER=... \
  --env ORCHD_LEASE=... --env ORCHD_SECRET_FILE=/run/secrets/agent \
  sha256:47198c...
```

`--pull=never`: nothing is pulled for you. An image that is not on the machine is a refusal
that says so, not a download that happens while you watch a spinner.

`--restart=no`, explicitly: an agent that stopped stays stopped until somebody starts it. A
control plane that resurrects containers behind the operator is one that disagrees with the
window about what is running.

Logs are bounded rather than disabled. `--log-driver=none` would make `docker logs` useless
for the one question an operator actually asks — *what did it say before it stopped?* — and no
limit at all is a laptop disk that fills overnight.

The image's own non-root user is not overridden. The image is the thing that knows which user
it built for; the task executor pins `--user` because it runs a stock Python image with a
script, which is a different situation.

## Docker is asked, not told

There is no table of running agents in the store. The container name is derived
(`orchd-agent-<name>`) and every container is labelled, so `docker ps --filter label=...`
answers exactly. A container started by a window that has since restarted is still running and
the daemon knows it; a second copy in the store would drift the first time anything happened
out of band.

For the same reason the listing is its own route rather than part of [`/status`](status.md).
The status view's promise is that it answers while everything else is busy; shelling out to a
daemon on every poll would trade that away for one panel.

## What the window shows that presence alone could not

Presence says whether an agent is **calling in**. The container says whether it is **there**.
Apart they are two half-answers, and one pair of them is the one that matters:

| Container | Calling in | What the roster says |
| --- | --- | --- |
| running | yes | Working, or idle waiting for work |
| running | no | **Container running, but not calling in** — marked, like a task waiting on you |
| stopped or absent | no | Not started |
| none declared | no | No container declared |

The middle row is the whole point. Before this, an agent whose container was up but whose
secret was wrong, or whose endpoint was unreachable, looked exactly like one nobody had
started yet.

Run and Stop are offered only when the window knows what the container is doing. If Docker
cannot be asked, the summary says why and the buttons are absent: a button that is certain to
be refused looks like the thing that is broken.

## Refusals are reported, not raised

A start that cannot happen comes back as a report with a status and a reason code — the way
[`doctor`](deployment-readiness.md) does — because "it did not start, and here is why" is what
an operator needs to see, not a 409 saying a request was rejected.

| Reason | What to do |
| --- | --- |
| `not_enrolled` | That name is not an agent here. `agent-enrol` first. |
| `not_declared` | No `container.json`. `agent-declare` on the host. |
| `already_running` / `not_running` | Nothing to do; the window is behind, refresh it. |
| `docker_cli_missing` / `daemon_unavailable` | Docker is not there or not answering. |
| `image_not_present` | Build or pull it. Nothing is pulled for you. |
| `previous_container_not_removed` | `docker ps -a`; something else holds the name. |
| `start_failed` / `stop_failed` | `docker logs orchd-agent-<name>`. |

The codes are a closed set and **never carry the daemon's own output**, which contains host
paths. The container's log is where the detail lives, and it survives: a container is removed
at the *next* start rather than with `--rm`, so the last run is still readable.

## Stopping hands work back

`docker stop --time 30` sends SIGTERM, which [the loop](agent-container.md) already handles by
releasing the task it holds. Thirty seconds is generous rather than tight: releasing is one
call, and a slow release should still hand the task back instead of letting the lease lapse.

Measured on a real container here: stop to exit took **0.66 seconds**, and the log ended with
the loop's own line rather than a kill.

## Using it

```sh
python -m orch agent-enrol   --agents agents --agent lead-devops --role lead_planner
python -m orch agent-declare --agents agents --agent lead-devops \
    --image orchd-agent:latest --agent-endpoint 127.0.0.1:8721 --role lead_planner
python -m orch agent-start   --agents agents --agent lead-devops
python -m orch agent-processes --agents agents
python -m orch agent-stop    --agents agents --agent lead-devops
```

A refusal exits non-zero, so a script can tell, and prints the same report the window gets.

On Docker Desktop add `--network container:<the service's container>` and
`--host-cannot-protect-secrets`; see [the container](agent-container.md) for why both are
needed there and what the second one does and does not make safe.

## What this is not

It does not schedule, restart, scale or supervise. It starts what you declared, stops what is
running, and says which is which. A supervisor that restarts agents is a thing that disagrees
with the window about what is running, and this deliberately does not have one.

It is not remote. Docker is asked on the machine the window runs on, as the operator, the same
way the task executor is.

It grants an agent nothing. The role gate, the lease, the admission bound, the human
approvals and the broker's monopoly on credentials are all exactly as they were; this only
decides whether the container that lives inside them is running.
