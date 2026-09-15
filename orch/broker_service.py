"""Loopback broker service so the host action broker can run under its own OS account.

The service owns its journal directory, its execution profile (mode and image) and
the Docker calls. The orchestrator reaches it only through five fixed POST routes on
127.0.0.1, each authenticated with an HMAC over a shared secret that both accounts
can read and nobody else can. No request JSON is parsed before its HMAC has been
verified, and no route accepts a shell string, mount, recipe text or image choice.
"""
import json
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .broker import MAX_MESSAGE, ROUTES, VERSION, atomic_json, file_lock, identity, load_secret, safe_id, sign, source_digest, validate_wire, verify
from .broker_process import check_request, journal, perform
from .contracts import Rejected, canonical
from .execution import Executor


class Missing(Rejected):
    pass


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_number(_):
    raise ValueError("Non-JSON number")


class BrokerService:
    def __init__(self, root, secret, workspaces, mode, image=None, port=0):
        self.root, self.workspaces = Path(root).absolute(), Path(workspaces).absolute()
        for path in (self.root, self.workspaces):
            for component in (path, *path.parents):
                if component.is_symlink() or (hasattr(component, "is_junction") and component.is_junction()):
                    raise Rejected("Broker service paths must not contain links")
        if self.root == self.workspaces or self.root in self.workspaces.parents or self.workspaces in self.root.parents:
            raise Rejected("Broker journal and workspaces must be separate directories")
        if type(port) is not int or not 0 <= port <= 65535:
            raise Rejected("Invalid broker port")
        self.key = load_secret(secret)
        self.executor = Executor(mode, image)
        self.profile = {"mode": mode, "image": image}
        self.identity = identity()
        self.root.mkdir(parents=True, exist_ok=True)
        self.server = HTTPServer(("127.0.0.1", port), self._handler())
        self.server.timeout = 5

    @property
    def port(self):
        return self.server.server_port

    def serve_forever(self):
        self.server.serve_forever()

    def shutdown(self):
        self.server.shutdown()
        self.server.server_close()

    def handle(self, route, body):
        if body["kind"] != route:
            raise Rejected("Broker message kind does not match its route")
        if route == "identity":
            return {"schema_version": VERSION, "kind": "identity", "nonce": body["nonce"], "identity": self.identity,
                    "source_digest": source_digest(), "profile": self.profile}
        if route in ("execute", "result"):
            request = body["request"]
            operation = check_request(request, self.workspaces, self.profile)
            if route == "execute":
                envelope = perform(self.root, request, self.executor)
            else:
                with file_lock(self.root / (operation + ".lock")):
                    envelope = journal(self.root, operation, request)
                if envelope is None:
                    raise Missing("No retained broker result")
            return {"schema_version": VERSION, "kind": route, "envelope": envelope, "identity": self.identity}
        operation = safe_id(body["operation_id"])
        if route == "retire":
            with file_lock(self.root / (operation + ".lock")):
                if (self.root / (operation + ".json")).exists():
                    raise Rejected("Invalid journal must be investigated, not discarded")
                atomic_json(self.root / (operation + ".retired"), {"generation": body["generation"]})
                absent = self.executor.reconcile(operation)
            return {"schema_version": VERSION, "kind": "retire", "nonce": body["nonce"], "operation_id": operation,
                    "generation": body["generation"], "container_absent": absent}
        return {"schema_version": VERSION, "kind": "reconcile", "nonce": body["nonce"], "operation_id": operation,
                "container_absent": self.executor.reconcile(operation)}

    def _handler(service):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass  # Never log authentication tags or bodies.

            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def reply(self, status, route, payload):
                raw = canonical(payload)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Orch-Broker-Auth", sign(service.key, "reply", route, raw))
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
                        or self.headers.get_all("Origin") or self.headers.get_all("Transfer-Encoding") or self.headers.get_all("Content-Encoding")
                        or len(self.headers.get_all("Content-Type", [])) != 1 or self.headers.get("Content-Type") != "application/json"
                        or len(self.headers.get_all("Content-Length", [])) != 1 or len(self.headers.get_all("X-Orch-Broker-Auth", [])) != 1):
                    self.send_error(400)
                    return
                length = self.headers.get("Content-Length")
                if not re.fullmatch("[0-9]{1,6}", length) or not 1 <= int(length) <= MAX_MESSAGE:
                    self.send_error(413)
                    return
                raw = self.rfile.read(int(length))
                if len(raw) != int(length):
                    self.send_error(400)
                    return
                if not verify(service.key, "request", route, raw, self.headers.get("X-Orch-Broker-Auth")):
                    self.send_error(401)
                    return
                try:
                    body = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_invalid_number)
                    if not isinstance(body, dict):
                        raise ValueError("Object required")
                    validate_wire(body, ROUTES[route][0])
                except (ValueError, UnicodeDecodeError, RecursionError, Rejected):
                    self.reply(400, route, {"error": "invalid_broker_message"})
                    return
                try:
                    payload = service.handle(route, body)
                    validate_wire(payload, ROUTES[route][1])
                    status = 200
                except Missing:
                    status, payload = 404, {"error": "no_retained_result"}
                except Rejected:
                    status, payload = 409, {"error": "broker_refused"}
                except Exception:  # A broker crash must answer, not hang the fenced caller.
                    status, payload = 500, {"error": "broker_internal_failure"}
                self.reply(status, route, payload)

        return Handler
