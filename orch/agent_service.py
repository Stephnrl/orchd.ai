"""Loopback agent API so agent containers never touch the store.

[Agent sessions](../docs/agent-sessions.md) made a role-scoped lease possible, but an agent
holding one needed the control plane installed in its image and the store mounted into its
container. A junior container could therefore claim as a lead planner, or skip the check
entirely and write the evidence store directly, so the role gate was cooperative rather
than enforced.

This service closes that. Each agent has its own secret; the agent it *is* comes from the
secret that authenticated the request, never from the request body, and the roles it may
claim come from this service's configuration rather than from the caller. Agents reach it
only through five fixed POST routes on 127.0.0.1, each authenticated with an HMAC, and no
request JSON is parsed before its HMAC has been verified.

The service holds the store; an agent holds nothing but a secret and a socket.
"""
import contextlib
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import re

from jsonschema import Draft202012Validator

from .broker import FORMATS, MAX_MESSAGE, SecretFile, authenticate, identity, sign, source_digest
from .contracts import Rejected, canonical
from .engine import Engine
from .execution import Executor
from .maintenance import real_path
from . import agent_sessions

VERSION = "1.0.0"
ROUTES = {"agent-identity": ("AgentIdentityRequest", "AgentIdentityReply"),
          "agent-claim": ("AgentClaimRequest", "AgentClaimReply"),
          "agent-renew": ("AgentRenewRequest", "AgentRenewReply"),
          "agent-release": ("AgentReleaseRequest", "AgentReleaseReply"),
          "agent-sessions": ("AgentSessionsRequest", "AgentSessionsReply")}
MAX_AGENTS = 32
MAX_ROLES = 10
WIRE_SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "contracts/agent-v1.schema.json").read_text())


def validate_wire(value, kind):
    schema = {**WIRE_SCHEMA, "oneOf": [{"$ref": "#/$defs/" + kind}]}
    if not Draft202012Validator(schema, format_checker=FORMATS).is_valid(value):
        raise Rejected("Invalid agent wire contract")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_number(_):
    raise ValueError("Non-JSON number")


def registry(directory):
    """Every agent this service serves: its secret file and the roles it may claim.

    One directory per agent, holding `secret` and `role`. Roles are read once at startup
    because they are policy, not credentials: changing what an agent may claim is a
    deliberate act that restarts the service, while rotating its secret is not.
    """
    directory = real_path(directory)
    if not directory.is_dir():
        raise Rejected("Agent registry directory does not exist")
    agents = {}
    for entry in sorted(directory.iterdir()):
        if not entry.is_dir():
            continue
        name = agent_sessions.identifier(entry.name)
        role_file, secret_file = entry / "role", entry / "secret"
        if not role_file.is_file() or not secret_file.is_file():
            raise Rejected("Agent " + name + " needs both a role and a secret file")
        roles = [line.strip() for line in role_file.read_text(encoding="ascii").splitlines() if line.strip()]
        if not 1 <= len(roles) <= MAX_ROLES or any(role not in agent_sessions.ROLES for role in roles):
            raise Rejected("Agent " + name + " must list one to %d known roles" % MAX_ROLES)
        if len(agents) >= MAX_AGENTS:
            raise Rejected("This service serves at most %d agents" % MAX_AGENTS)
        agents[name] = {"roles": tuple(roles), "secret": SecretFile(secret_file)}
    if not agents:
        raise Rejected("The agent registry is empty")
    return agents


class AgentService:
    """The store's side of the conversation. It executes nothing and approves nothing."""

    def __init__(self, data, agents, port=0):
        self.agents = registry(agents)
        self.data = real_path(data)
        self.source = source_digest()
        self.server = HTTPServer(("127.0.0.1", port), self._handler(self))
        self.server.timeout = 5

    @contextlib.contextmanager
    def opened(self):
        """The store, for exactly one request.

        A SQLite connection belongs to the thread that made it, and a service has no
        business holding a writable one open between calls anyway: each request opens the
        store, takes the same locks a worker would, and closes it again. The fixture
        executor is never used, because no route here runs anything.
        """
        engine = Engine(self.data, Executor("trusted-fixture"))
        try:
            yield engine
        finally:
            engine.close()

    @property
    def port(self):
        return self.server.server_port

    def close(self):
        self.server.server_close()

    def serve_forever(self):
        try:
            self.server.serve_forever(poll_interval=0.2)
        finally:
            self.close()

    def identify(self, raw, tag, route):
        """Which agent signed this, by trying every agent's accepted secrets.

        Every key is tried and none short-circuits, so a wrong secret costs the same as a
        right one and the reply is signed with whichever key actually matched.
        """
        found = None
        for name, agent in self.agents.items():
            try:
                accepted = agent["secret"].accepted
            except Rejected:
                continue
            key = authenticate(accepted, "request", route, raw, tag)
            if key is not None:
                found = (name, key)
        return found

    def handle(self, route, agent, body):
        """Fixed routes only. The agent name comes from the secret, never from the body."""
        nonce = body["nonce"]
        base = {"schema_version": VERSION, "kind": route, "nonce": nonce,
                "live_authorized": False, "retry_allowed": False}
        if route == "agent-identity":
            return {"schema_version": VERSION, "kind": route, "nonce": nonce, "agent": agent,
                    "roles": list(self.agents[agent]["roles"]), "store_sha256": self.source,
                    "live_authorized": False}
        with self.opened() as engine:
            if route == "agent-sessions":
                listed = agent_sessions.sessions(engine.store)
                return {**base, "sessions": listed["sessions"], "held": listed["held"]}
            task = body["task_id"]
            if route == "agent-claim":
                role = body["role"]
                if role not in self.agents[agent]["roles"]:
                    raise Rejected("Agent " + agent + " may not claim work as " + role)
                report = agent_sessions.claim(engine, task, agent, role, body.get("lease"),
                                              body.get("expected_revision"))
            elif route == "agent-renew":
                report = agent_sessions.renew(engine, task, agent, body.get("lease"))
            else:
                released = agent_sessions.release(engine, task, agent)
                return {**base, "status": "released", "task_id": task, "agent": agent,
                        "released_at": released["released_at"]}
            return {**base, "status": report["status"], "session": self._session(engine, task)}

    @staticmethod
    def _session(engine, task):
        for row in agent_sessions.sessions(engine.store)["sessions"]:
            if row["task_id"] == task:
                return row
        raise Rejected("The session vanished before it could be reported")

    @staticmethod
    def _handler(service):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass  # Never log authentication tags or bodies.

            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def reply(self, status, route, payload, key):
                """Signed with the secret that authenticated the request, so an agent
                mid-rotation can still verify its own reply."""
                raw = canonical(payload)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Orch-Agent-Auth", sign(key, "reply", route, raw))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self.send_error(405)

            def do_POST(self):
                route = self.path[1:]
                if route not in ROUTES:
                    self.send_error(404)
                    return
                expected_host = "127.0.0.1:" + str(self.server.server_port)
                if (len(self.headers.get_all("Host", [])) != 1 or self.headers.get("Host") != expected_host
                        or self.headers.get_all("Origin") or self.headers.get_all("Transfer-Encoding")
                        or self.headers.get_all("Content-Encoding")
                        or len(self.headers.get_all("Content-Type", [])) != 1
                        or self.headers.get("Content-Type") != "application/json"
                        or len(self.headers.get_all("Content-Length", [])) != 1
                        or len(self.headers.get_all("X-Orch-Agent-Auth", [])) != 1):
                    self.send_error(400)
                    return
                length = self.headers.get("Content-Length")
                if not re.fullmatch("[0-9]{1,7}", length) or not 1 <= int(length) <= MAX_MESSAGE:
                    self.send_error(413)
                    return
                raw = self.rfile.read(int(length))
                if len(raw) != int(length):
                    self.send_error(400)
                    return
                found = service.identify(raw, self.headers.get("X-Orch-Agent-Auth"), route)
                if found is None:
                    self.send_error(401)
                    return
                agent, key = found
                try:
                    body = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_invalid_number)
                    if not isinstance(body, dict):
                        raise ValueError("Object required")
                    validate_wire(body, ROUTES[route][0])
                except (ValueError, UnicodeDecodeError, RecursionError, Rejected):
                    self.reply(400, route, {"error": "invalid_agent_message"}, key)
                    return
                try:
                    payload = service.handle(route, agent, body)
                    validate_wire(payload, ROUTES[route][1])
                    status = 200
                except Rejected:
                    status, payload = 409, {"error": "agent_refused"}
                except Exception:  # A crash must answer, not hang an agent that is waiting.
                    status, payload = 500, {"error": "agent_service_failure"}
                self.reply(status, route, payload, key)

        return Handler
