"""The agent's side of the loopback API: a secret, a socket, and nothing else.

An agent container needs this and an endpoint. It never opens the store, never sees another
agent's secret, and never states which agent it is: the service reads that from whichever
secret verified the request, so a container cannot answer for a different agent by asking
nicely.
"""
import http.client
import json
import re

from .agent_service import ROUTES, VERSION, validate_wire
from .broker import MAX_MESSAGE, MAX_REPLY, SecretFile, endpoint, sign, verify
from .contracts import Rejected, Transient, canonical, uid


class AgentClient:
    """One agent talking to one service. Replies are verified before they are read."""

    def __init__(self, service, secret):
        self.host, self.port = endpoint(service)
        self.secret = SecretFile(secret)

    def call(self, route, body=None):
        if route not in ROUTES:
            raise Rejected("Unknown agent route")
        request_kind, reply_kind = ROUTES[route]
        message = {"schema_version": VERSION, "kind": route, "nonce": uid(), **(body or {})}
        validate_wire(message, request_kind)
        raw = canonical(message)
        if len(raw) > MAX_MESSAGE:
            raise Rejected("Agent request too large")
        # Sign with the current secret and verify the reply with the same one, so a
        # rotation between request and reply cannot look like a forged answer.
        key = self.secret.current
        headers = {"Content-Type": "application/json", "X-Orch-Agent-Auth": sign(key, "request", route, raw)}
        connection = http.client.HTTPConnection(self.host, self.port, timeout=45)
        try:
            connection.request("POST", "/" + route, raw, headers)
            response = connection.getresponse()
            length = response.getheader("Content-Length", "")
            if not re.fullmatch("[0-9]{1,7}", length) or int(length) > MAX_REPLY:
                raise Rejected("Agent reply size rejected")
            data = response.read(int(length) + 1)
            if len(data) != int(length):
                raise Rejected("Incomplete agent reply")
            status, tag = response.status, response.getheader("X-Orch-Agent-Auth", "")
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise Rejected("Agent service unavailable") from exc
        finally:
            connection.close()
        if not verify(key, "reply", route, data, tag):
            raise Rejected("Agent reply authentication failed")
        if status != 200:
            # The body was authenticated above, so what it says about retrying can be
            # believed. A refusal that may change on its own is raised as one.
            try:
                refusal = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                refusal = {}
            if isinstance(refusal, dict) and refusal.get("retry_allowed") is True:
                raise Transient("Agent service refused the request for now")
            raise Rejected("Agent service refused the request")
        try:
            payload = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise Rejected("Invalid agent reply") from exc
        validate_wire(payload, reply_kind)
        if payload.get("nonce") != message["nonce"]:
            raise Rejected("Agent reply answered another request")
        return payload

    def whoami(self):
        return self.call("agent-identity")

    def claim(self, task, role, lease=None, expected_revision=None):
        body = {"task_id": task, "role": role}
        if lease is not None:
            body["lease"] = lease
        if expected_revision is not None:
            body["expected_revision"] = expected_revision
        return self.call("agent-claim", body)

    def renew(self, task, lease=None):
        body = {"task_id": task}
        if lease is not None:
            body["lease"] = lease
        return self.call("agent-renew", body)

    def release(self, task):
        return self.call("agent-release", {"task_id": task})

    def sessions(self):
        return self.call("agent-sessions")

    def available(self):
        return self.call("agent-available")

    def specification(self, task):
        return self.call("agent-spec", {"task_id": task})

    def draft(self, task, specification, expected_revision=None):
        body = {"task_id": task, "specification": specification}
        if expected_revision is not None:
            body["expected_revision"] = expected_revision
        return self.call("agent-draft", body)

    def ask(self, task, questions, expected_revision=None):
        body = {"task_id": task, "questions": questions}
        if expected_revision is not None:
            body["expected_revision"] = expected_revision
        return self.call("agent-ask", body)
