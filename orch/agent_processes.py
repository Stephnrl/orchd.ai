"""Starting and stopping the agent containers an operator declared, and nothing else.

Until now the window could see agents and not run them: you enrolled one, then went to a
terminal and typed a `docker run` line with four flags you had to get right. The window is
supposed to be where a day starts, so this is the part that was missing — click an agent,
click Run.

It is also the part that could have been a hole. A window that composes a command from what
a browser sends is a window that runs whatever a browser sends, and this control plane exists
to make that impossible. So nothing here takes a command, a path, an image or a flag from a
request. **The only thing a request contributes is which declared agent to act on**, and the
name has to already be enrolled.

What an agent may run is a declaration written beside its role and its secret, by the
operator, on the host: `agents/<name>/container.json`. Every field in it is a value with a
grammar — a digest-pinned image, a loopback endpoint, one of the two network modes that
actually work, a role that agent is enrolled for — and a field that is not in the closed set
is refused rather than passed through. The argv is a fixed template with those values
substituted, exactly as the [executor](execution.py) renders a task container.

Two things are deliberately *not* declarable. The secret to mount is derived from the
registry, because a declaration that can name any host file is a way to read any host file
through a container. The command is fixed, because the image's entry point is the agent loop
and a declaration that can replace it is a declaration that can run anything.

Docker is asked what is running rather than the store being told. A container that was
started by a window that has since restarted is still running, and the daemon knows it; a
table in the store would be a second copy that drifts the first time anything happens out of
band. The name is derived (`orchd-agent-<name>`) and every container is labelled, so finding
them is exact rather than a guess about what a name means.

Refusals here are **reported rather than raised**: a start that cannot happen comes back as a
report with a status and a reason code, the way [`doctor`](readiness.py) does, because "it did
not start" is a thing the operator needs to see in the window rather than a 409 that says a
request was rejected. The codes are a closed set and never carry the daemon's own output.
"""
import json
import os
import re
import shutil

from .agent_sessions import AGENT_ROLES, DEFAULT_LEASE, MAX_LEASE, MIN_LEASE, identifier
from .broker import atomic_json, endpoint as loopback
from .contracts import Rejected, now
from .maintenance import real_path
from .process import capture

DECLARATION = "container.json"
LABEL = "ai.orchd.agent"
PREFIX = "orchd-agent-"
# How long `docker stop` waits for the loop to release what it holds before the kill. The
# runner releases on SIGTERM, and releasing is one call; this is generous rather than tight
# so that a slow release still hands the task back instead of lapsing.
STOP_SECONDS = 30
MAX_DECLARATION_BYTES = 8192
MAX_LISTED = 64
DEFAULT_MEMORY = "512m"
DEFAULT_DECIDER = "orch.agent_main:Idle"
# Exact bytes, in either of the two ways Docker names them: a registry digest, or the local
# image id. An image built on this machine has no registry digest until it is pushed, and
# requiring one would mean the feature could not be used with the image in the repository.
IMAGE = re.compile(r"(?:[A-Za-z0-9./:_-]{1,200}@)?sha256:[a-f0-9]{64}")
TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9./_-]{0,199}(:[A-Za-z0-9][A-Za-z0-9._-]{0,127})?")
DECIDER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*")
CONTAINER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
MEMORY = re.compile(r"[0-9]{1,6}[mg]")
FIELDS = {"image", "endpoint", "role", "decider", "lease", "network", "memory",
          "host_cannot_protect_secrets", "resolved_from"}
REQUIRED = {"image", "endpoint", "role"}


def folder(directory, name):
    """The agent's own directory, refused if it is not one this registry holds."""
    name = identifier(name)
    place = real_path(directory) / name
    if not place.is_dir() or not (place / "role").is_file() or not (place / "secret").is_file():
        raise Rejected("Agent " + name + " is not enrolled here")
    return name, place


def roles(place):
    return [line.strip() for line in (place / "role").read_text(encoding="ascii").splitlines() if line.strip()]


def declaration(directory, name):
    """What this agent runs as, read and checked field by field.

    Every value has a grammar and the set of fields is closed. A declaration that carries
    something this does not know about is refused rather than ignored: a field nobody reads
    is how a setting quietly stops taking effect.
    """
    name, place = folder(directory, name)
    path = place / DECLARATION
    if path.is_symlink() or not path.is_file():
        raise Rejected("Agent " + name + " has no container declaration")
    if path.stat().st_size > MAX_DECLARATION_BYTES:
        raise Rejected("A container declaration is a small file")
    try:
        declared = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise Rejected("Container declaration is not readable JSON") from exc
    if not isinstance(declared, dict) or set(declared) - FIELDS or REQUIRED - set(declared):
        raise Rejected("A declaration states image, endpoint and role, and only known fields")
    if not isinstance(declared["image"], str) or not IMAGE.fullmatch(declared["image"]):
        raise Rejected("An image is digest-pinned, as name@sha256:...")
    loopback(declared["endpoint"])    # 127.0.0.1:PORT, or refused by name
    if declared["role"] not in AGENT_ROLES or declared["role"] not in roles(place):
        raise Rejected("A container runs as one role its agent is enrolled for")
    if "resolved_from" in declared and (not isinstance(declared["resolved_from"], str)
                                        or not TAG.fullmatch(declared["resolved_from"])):
        raise Rejected("resolved_from records the tag this image was pinned from")
    result = {"agent": name, "image": declared["image"], "endpoint": declared["endpoint"],
              "role": declared["role"], "decider": declared.get("decider", DEFAULT_DECIDER),
              "lease": declared.get("lease", DEFAULT_LEASE),
              "network": declared.get("network", "host"),
              "memory": declared.get("memory", DEFAULT_MEMORY),
              "host_cannot_protect_secrets": declared.get("host_cannot_protect_secrets", False)}
    if not isinstance(result["decider"], str) or not DECIDER.fullmatch(result["decider"]):
        raise Rejected("A decider is module:attribute")
    if type(result["lease"]) is not int or not MIN_LEASE <= result["lease"] <= MAX_LEASE:
        raise Rejected("A lease is %d to %d seconds" % (MIN_LEASE, MAX_LEASE))
    if not isinstance(result["memory"], str) or not MEMORY.fullmatch(result["memory"]):
        raise Rejected("Memory is a number followed by m or g")
    if not isinstance(result["host_cannot_protect_secrets"], bool):
        raise Rejected("host_cannot_protect_secrets is true or false")
    network(result["network"])
    return result


def network(value):
    """One of the two deployments that actually work, and nothing else.

    `--network host` shares the host's loopback, which is where the agent service listens.
    On Docker Desktop that is the Linux VM's loopback and not the host's, so there the
    container joins the service's own namespace instead. Anything else would mean exposing
    the agent service beyond loopback, which is a decision to make deliberately rather than
    by writing a word in a file. See docs/agent-container.md.
    """
    if value == "host":
        return ["--network=host"]
    if isinstance(value, str) and value.startswith("container:") and CONTAINER.fullmatch(value[10:]):
        return ["--network=" + value]
    raise Rejected("A network is host, or container:NAME to join a running container")


def pinned(image):
    """Exact bytes, from an already-pinned reference or by resolving a tag once, now.

    A tag is what an operator has in their hand after `docker build`, and it moves the next
    time they build. Resolving it here means the declaration records the image that tag meant
    on the day it was written, rather than whatever it points at the morning something breaks.
    """
    if isinstance(image, str) and IMAGE.fullmatch(image):
        return image, None
    if not isinstance(image, str) or not TAG.fullmatch(image):
        raise Rejected("An image is a tag to pin now, or already pinned as sha256:... ")
    result = docker(["docker", "image", "inspect", image, "--format", "{{json .Id}}"], timeout=20)
    if result["code"] != 0:
        raise Rejected("No local image called " + image + "; build or pull it first")
    try:
        identity = json.loads(result["stdout"].strip())
    except ValueError as exc:
        raise Rejected("Could not read that image's identity") from exc
    if not isinstance(identity, str) or not IMAGE.fullmatch(identity):
        raise Rejected("Could not read that image's identity")
    return identity, image


def declare(directory, name, image, service, role, decider=None, lease=None,
            network_mode=None, memory=None, host_cannot_protect_secrets=False):
    """Write the declaration, having checked every value the way starting will."""
    name, place = folder(directory, name)
    path = place / DECLARATION
    if path.exists():
        raise Rejected("Agent " + name + " is already declared; remove " + DECLARATION + " to redeclare")
    image, from_tag = pinned(image)
    declared = {"image": image, "endpoint": service, "role": role,
                "decider": decider or DEFAULT_DECIDER, "lease": lease or DEFAULT_LEASE,
                "network": network_mode or "host", "memory": memory or DEFAULT_MEMORY,
                "host_cannot_protect_secrets": bool(host_cannot_protect_secrets)}
    if from_tag:
        declared["resolved_from"] = from_tag
    atomic_json(path, declared)
    try:
        checked = declaration(directory, name)
    except Rejected:
        # A file that would be refused at start time is worse than no file: it looks
        # declared in the window and fails when the operator most wants it to work.
        path.unlink(missing_ok=True)
        raise
    return {"kind": "AgentDeclared", "declaration": str(path), **checked, "live_authorized": False}


def command_for(directory, name, declared):
    """The fixed argv for one declared agent. Nothing in it comes from a request.

    The shape mirrors the task executor's: no shell, no interpolation into a string, an
    explicit name and label so the container can be found again, and no image pull.
    """
    _, place = folder(directory, name)
    secret = str(real_path(place / "secret"))
    if "," in secret:
        # `--mount` separates its own fields with commas, so a path containing one would
        # become two options. Refusing beats guessing what the operator meant.
        raise Rejected("A registry directory cannot contain a comma in its path")
    argv = ["docker", "run", "--detach", "--pull=never",
            "--name", PREFIX + name, "--label", LABEL + "=" + name,
            # Never resurrected behind the operator's back: an agent that stopped stays
            # stopped until somebody starts it, and the window says which are not running.
            "--restart=no",
            "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--pids-limit=256", "--memory=" + declared["memory"],
            # The copy a host that cannot keep a mount private needs, and nothing else, is
            # written here; the image runs as its own non-root user, which is not overridden
            # because the image is the thing that knows which user it built for.
            "--tmpfs=/tmp:rw,noexec,nosuid,size=16m",
            # Bounded logs rather than none: the operator needs to read what an agent did,
            # and an unbounded log on a laptop is a disk that fills overnight.
            "--log-opt=max-size=10m", "--log-opt=max-file=3",
            *network(declared["network"]),
            "--mount", "type=bind,source=" + secret + ",target=/run/secrets/agent,readonly",
            "--env", "ORCHD_ENDPOINT=" + declared["endpoint"],
            "--env", "ORCHD_ROLE=" + declared["role"],
            "--env", "ORCHD_DECIDER=" + declared["decider"],
            "--env", "ORCHD_LEASE=" + str(declared["lease"]),
            "--env", "ORCHD_SECRET_FILE=/run/secrets/agent"]
    if declared["host_cannot_protect_secrets"]:
        argv += ["--env", "ORCHD_HOST_CANNOT_PROTECT_SECRETS=1"]
    # The image last and no command after it: the entry point is the agent loop, and a
    # declaration that could replace it would be a declaration that could run anything.
    return argv + [declared["image"]]


def docker(argv, timeout=30):
    """Run one docker command with the same restricted environment the executor uses."""
    executable = shutil.which("docker")
    if not executable:
        raise Rejected("docker_cli_missing")
    env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP")
           if key in os.environ}
    result = capture([executable, *argv[1:]], timeout=timeout, limit=65536, env=env)
    if result["failure"]:
        raise Rejected("daemon_unavailable")
    return result


def listed(directory):
    """What Docker says about the containers this registry's agents run in.

    One call for all of them, filtered by label, so the answer is about orchd's containers
    and nothing else on the machine.
    """
    result = docker(["docker", "ps", "--all", "--no-trunc", "--filter", "label=" + LABEL,
                     "--format", "{{json .}}"], timeout=20)
    if result["code"] != 0:
        raise Rejected("daemon_unavailable")
    found = {}
    for line in result["stdout"].splitlines()[:MAX_LISTED]:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        name = str(row.get("Names", "")).split(",")[0]
        if not name.startswith(PREFIX):
            continue
        found[name[len(PREFIX):]] = {
            "container": str(row.get("ID", ""))[:12], "state": str(row.get("State", "")),
            "status": str(row.get("Status", ""))[:120], "image": str(row.get("Image", ""))[:200],
            "started": str(row.get("RunningFor", ""))[:60]}
    return found


def report(kind, name, status, reason=None, detail=None, **extra):
    return {"kind": kind, "agent": name, "status": status, "reason": reason,
            "detail": detail[:200] if detail else None, "checked_at": now(),
            "live_authorized": False, **extra}


def start(directory, name):
    """Start one declared agent. The name is the only thing a caller decides."""
    try:
        name, _ = folder(directory, name)
    except Rejected as exc:
        return report("AgentStart", str(name), "refused", "not_enrolled", str(exc))
    try:
        declared = declaration(directory, name)
    except Rejected as exc:
        return report("AgentStart", name, "refused", "not_declared", str(exc))
    try:
        state = listed(directory).get(name)
    except Rejected as exc:
        return report("AgentStart", name, "refused", str(exc))
    if state and state["state"] == "running":
        # Two containers for one agent would both hold leases as the same name, and the
        # second would look like the first coming back.
        return report("AgentStart", name, "refused", "already_running", container=state["container"])
    if state:
        # The previous run's container still holds the name. Removing it here rather than
        # running with `--rm` is what keeps its logs readable until the next start.
        removal = docker(["docker", "rm", PREFIX + name], timeout=20)
        if removal["code"] != 0:
            return report("AgentStart", name, "refused", "previous_container_not_removed")
    try:
        result = docker(command_for(directory, name, declared), timeout=60)
    except Rejected as exc:
        return report("AgentStart", name, "refused", str(exc))
    if result["code"] != 0:
        # The daemon's own words are not reported: they carry host paths, and the operator
        # can read them with `docker logs`. The reason names the common cause.
        reason = "image_not_present" if "pull" in result["stderr"] or "No such image" in result["stderr"] else "start_failed"
        return report("AgentStart", name, "refused", reason)
    return report("AgentStart", name, "started", container=result["stdout"].strip()[:12],
                  role=declared["role"], image=declared["image"])


def stop(directory, name):
    """Stop one agent, giving its loop time to hand back what it holds."""
    try:
        name, _ = folder(directory, name)
    except Rejected as exc:
        return report("AgentStop", str(name), "refused", "not_enrolled", str(exc))
    try:
        state = listed(directory).get(name)
    except Rejected as exc:
        return report("AgentStop", name, "refused", str(exc))
    if not state or state["state"] != "running":
        return report("AgentStop", name, "refused", "not_running")
    result = docker(["docker", "stop", "--time", str(STOP_SECONDS), PREFIX + name],
                    timeout=STOP_SECONDS + 30)
    if result["code"] != 0:
        return report("AgentStop", name, "refused", "stop_failed", container=state["container"])
    # The container is left in place, stopped, so its log survives until the next start.
    return report("AgentStop", name, "stopped", container=state["container"])


def processes(directory):
    """Every enrolled agent: whether it is declared, and what its container is doing.

    Reported rather than raised when Docker cannot be reached, for the same reason the status
    view reports an unopenable journal: a panel that dies because one thing it describes is
    broken is the opposite of useful.
    """
    from .agent_service import registry
    result = {"kind": "AgentProcesses", "agents": [], "docker": "available", "reason": None,
              "checked_at": now(), "live_authorized": False}
    try:
        enrolled = sorted(registry(directory))
    except (Rejected, OSError) as exc:
        return {**result, "docker": "unknown", "reason": "registry_unreadable",
                "detail": str(exc)[:200]}
    try:
        containers = listed(directory)
    except Rejected as exc:
        containers, result["docker"], result["reason"] = {}, "unavailable", str(exc)
    for name in enrolled:
        entry = {"agent": name, "declared": False, "role": None, "image": None,
                 "state": "no container", "container": None, "status": None, "reason": None}
        try:
            declared = declaration(directory, name)
            entry.update(declared=True, role=declared["role"], image=declared["image"])
        except Rejected as exc:
            entry["reason"] = str(exc)[:200]
        found = containers.get(name)
        if found:
            entry.update(state=found["state"], container=found["container"], status=found["status"])
        result["agents"].append(entry)
    return result
