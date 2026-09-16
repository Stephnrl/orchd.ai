"""The container's entry point: configuration from the environment, then the loop.

An agent container is given three things and no more — where the control plane is, which
role it works as, and the path to its own secret. It is never given a store path, never given
a credential for anything outside this host, and never told which task to work on. Finding
work is [its own job](../docs/agent-queue.md); reaching anything outside is [the broker's](../docs/outbound-dispatch.md).

The decider is named rather than built in, because deciding what a specification should say is
the agent's own business and this package has no opinion about it. `ORCHD_DECIDER` is an
`module:attribute` path to a `Decider`; the default does nothing at all, so a container can be
started and watched before there is an intelligence behind it.

Shutdown is a signal, and the loop treats it as one: a container powered off mid-task hands
the task back rather than leaving it held for the rest of a lease nobody is using.
"""
import importlib
import os
import pathlib
import shutil
import signal
import sys
import tempfile

from .agent_client import AgentClient
from .agent_runner import DEFAULT_LEASE, Decider, Runner
from .contracts import Rejected


class Idle(Decider):
    """A decider with nothing to say, so the loop can be run before an agent can think.

    It takes work, looks at it, and hands it straight back. That is deliberately useless and
    deliberately harmless: it exercises claiming, renewing and releasing without writing a
    specification nobody meant.
    """

    def consider(self, draft):
        return ("release", None)


def load(path):
    """A `module:attribute` decider, refused clearly rather than imported hopefully."""
    if not isinstance(path, str) or path.count(":") != 1:
        raise Rejected("ORCHD_DECIDER is module:attribute, for example orch.agent_main:Idle")
    module, _, name = path.partition(":")
    try:
        found = getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError, ValueError) as exc:
        raise Rejected("Could not load the decider " + path) from exc
    decider = found() if isinstance(found, type) else found
    if not isinstance(decider, Decider):
        raise Rejected("The decider " + path + " is not a Decider")
    return decider


def readable(path, host_cannot_protect):
    """The secret file, or a private copy of it when the host cannot make one private.

    A bind mount from Docker Desktop on Windows or macOS arrives as mode 777 whatever the
    host file says, because those filesystems carry no POSIX modes. The reader is right to
    refuse it: on a Linux host a world-accessible secret means any account can read it.

    Where the host genuinely cannot provide the property, an operator may say so, and this
    copies the secret into a private directory of its own so the reader is satisfied. That
    copy is honest about what it is: it makes the file private *inside the container*, and it
    does nothing whatever about the host, which is why it is opt-in and says so out loud.
    """
    exposed = os.name != "nt" and os.path.isfile(path) and os.stat(path).st_mode & 0o007
    if not exposed or not host_cannot_protect:
        return path
    private = pathlib.Path(tempfile.mkdtemp(prefix="orchd-agent-"))   # 0700 by construction
    copy = private / "secret"
    shutil.copyfile(path, copy)
    os.chmod(copy, 0o600)
    print("orchd agent: " + str(path) + " is world-accessible and the host was declared unable "
          "to protect it; using a private copy inside this container. The file on the host is "
          "still exposed to every account on it.", file=sys.stderr, flush=True)
    return str(copy)


def configured(environment=None):
    """Everything this container runs on, with nothing inferred and nothing defaulted away."""
    env = environment if environment is not None else os.environ
    endpoint, secret, role = env.get("ORCHD_ENDPOINT"), env.get("ORCHD_SECRET_FILE"), env.get("ORCHD_ROLE")
    if not endpoint or not secret or not role:
        raise Rejected("An agent container needs ORCHD_ENDPOINT, ORCHD_SECRET_FILE and ORCHD_ROLE")
    lease = env.get("ORCHD_LEASE") or str(DEFAULT_LEASE)
    if not lease.isdigit():
        raise Rejected("ORCHD_LEASE is a whole number of seconds")
    # The decider first: it is pure configuration, and checking it before opening a secret
    # file means a misspelled import path is reported as one rather than as a missing secret.
    decider = load(env.get("ORCHD_DECIDER") or __name__ + ":Idle")
    secret = readable(secret, env.get("ORCHD_HOST_CANNOT_PROTECT_SECRETS") == "1")
    try:
        client = AgentClient(endpoint, secret)
    except Rejected as exc:
        # The underlying reader speaks of broker secrets; in a container this is the agent's.
        raise Rejected("ORCHD_SECRET_FILE is unusable: " + str(exc)) from exc
    return Runner(client, decider, role, lease=int(lease))


def main(environment=None):
    try:
        runner = configured(environment)
    except (Rejected, OSError) as exc:
        # The message names the setting, never its value: a secret path is configuration and
        # a secret is not, and nothing here should teach anyone to print one.
        print("orchd agent: " + str(exc), file=sys.stderr)
        return 2
    for received in (signal.SIGTERM, signal.SIGINT):
        signal.signal(received, lambda *_: runner.stop())
    try:
        # Saying who we are is a courtesy, not a precondition: a container started before the
        # control plane is up should wait for it, which is what the loop already does on an
        # outage, rather than dying because the first call happened to be too early.
        name = runner.client.whoami()["agent"]
    except Rejected:
        name = "(not yet known)"
        print("orchd agent: the control plane is not answering yet; waiting", file=sys.stderr, flush=True)
    print("orchd agent " + name + " working as " + runner.role, flush=True)
    try:
        runner.run()
    except Rejected as exc:
        # A refusal that reached here is a configuration or protocol problem, not a moment to
        # retry. Report it as a line, not as a traceback a container log will bury.
        print("orchd agent: " + str(exc), file=sys.stderr, flush=True)
        return 1
    print("orchd agent " + name + " stopped; " + str(runner.worked) + " steps", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
