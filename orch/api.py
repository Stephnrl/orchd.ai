"""Small localhost HTTP boundary. Session identity never comes from request JSON."""
import hmac
import json
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit

from .contracts import Rejected


class Application:
    def __init__(self, engine):
        self.engine = engine
        self.session = secrets.token_urlsafe(32)  # In-memory local session; never persisted.

    def dispatch(self, method, path, payload, token):
        if not isinstance(token, str) or not hmac.compare_digest(token, self.session):
            return 401, {"error": "Operator session required"}
        parts = path.strip("/").split("/")
        try:
            if method == "POST" and parts == ["tasks"]:
                if set(payload) - {"title"}:
                    raise Rejected("Unknown create fields")
                return 201, {"task_id": self.engine.create_task(payload.get("title", "Greeting fixture"))}
            if len(parts) < 2 or parts[0] != "tasks":
                return 404, {"error": "Unknown route"}
            task = parts[1]
            if method == "GET" and len(parts) == 2:
                return 200, self.engine.task(task)
            if method == "GET" and parts[2:] == ["events"]:
                return 200, self.engine.events(task)
            if method == "GET" and parts[2:] == ["history"]:
                return 200, [{"event": e, "payload": json.loads(self.engine.store.read_artifact(e["payload"], task))} for e in self.engine.events(task)]
            if method == "POST" and parts[2:] in (["run"], ["advance"]):
                if payload:
                    raise Rejected("Run accepts no agent instructions")
                return 200, getattr(self.engine, parts[2])(task)
            if method == "POST" and parts[2:] == ["approvals"]:
                if set(payload) != {"request_id", "decision", "expected_revision"} or type(payload["expected_revision"]) is not int:
                    raise Rejected("Invalid approval fields")
                return 200, self.engine.approve(task, **payload)
            if method == "POST" and parts[2:] == ["recover"]:
                if set(payload) != {"expected_revision"} or type(payload["expected_revision"]) is not int:
                    raise Rejected("Invalid recovery fields")
                return 200, self.engine.recover(task, payload["expected_revision"])
            return 404, {"error": "Unknown route"}
        except (Rejected, KeyError, TypeError):
            return 409, {"error": "Request rejected; inspect current task and approval"}


def serve(engine, port=8080):
    app = Application(engine)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *_):
            pass  # Do not log authorization headers or query strings.

        def handle_request(self):
            expected_host = "127.0.0.1:" + str(self.server.server_port)
            if self.headers.get("Host") != expected_host or self.headers.get("Origin") is not None:
                self.send_error(403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 4096:
                    raise ValueError()
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError()
                body = json.loads(self.rfile.read(length)) if length else {}
                if not isinstance(body, dict):
                    raise ValueError()
            except (ValueError, UnicodeDecodeError):
                self.send_error(400)
                return
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            code, output = app.dispatch(self.command, urlsplit(self.path).path, body, token)
            encoded = json.dumps(output).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        do_GET = handle_request
        do_POST = handle_request

    server = HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 5
    print(f"Local API: http://127.0.0.1:{server.server_port}")
    print(f"Operator session (this process only): {app.session}", flush=True)
    server.serve_forever()
