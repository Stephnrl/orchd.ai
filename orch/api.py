"""Small localhost HTTP boundary. Session identity never comes from request JSON."""
import hmac
import json
import secrets
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit, parse_qs

from .contracts import Rejected, ref


def page(query):
    if set(query) - {"after", "limit"} or any(len(v) != 1 for v in query.values()):
        raise Rejected("Invalid pagination")
    after, limit = query.get("after", ["0"])[0], query.get("limit", ["50"])[0]
    if not after.isascii() or not after.isdecimal() or not limit.isascii() or not limit.isdecimal():
        raise Rejected("Invalid pagination")
    after, limit = int(after), int(limit)
    if not 0 <= after <= 9223372036854775807 or not 1 <= limit <= 100:
        raise Rejected("Invalid pagination")
    return after, limit


class Application:
    def __init__(self, engine):
        self.engine = engine
        self.session = secrets.token_urlsafe(32)  # In-memory local session; never persisted.

    def dispatch(self, method, path, payload, token):
        if not isinstance(token, str) or not hmac.compare_digest(token, self.session):
            return 401, {"error": "Operator session required"}
        try:
            parsed = urlsplit(path)
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=10)
            parts = parsed.path.strip("/").split("/")
            if method == "GET" and parts == ["tasks"]:
                after, limit = page(query)
                rows = self.engine.store.db.execute("SELECT rowid,id,state FROM tasks WHERE rowid>? ORDER BY rowid LIMIT ?", (after, limit + 1)).fetchall()
                return 200, {"items": [{"task_id": r["id"], "state": json.loads(r["state"])} for r in rows[:limit]],
                             "next": rows[limit-1]["rowid"] if len(rows) > limit else None}
            if query and not (method == "GET" and len(parts) == 3 and parts[2] in ("records", "stream")):
                raise Rejected("Unexpected query")
            if method == "POST" and parts == ["tasks"]:
                if set(payload) - {"title"}:
                    raise Rejected("Unknown create fields")
                return 201, {"task_id": self.engine.create_task(payload.get("title", "Greeting fixture"))}
            if len(parts) < 2 or parts[0] != "tasks":
                return 404, {"error": "Unknown route"}
            task = parts[1]
            if method == "GET" and parts[2:] == ["records"]:
                self.engine.task(task)
                after, limit = page(query)
                rows = self.engine.store.db.execute("SELECT rowid,id,kind FROM records WHERE task_id=? AND rowid>? ORDER BY rowid LIMIT ?", (task, after, limit + 1)).fetchall()
                return 200, {"items": [{"id": r["id"], "kind": r["kind"]} for r in rows[:limit]],
                             "next": rows[limit-1]["rowid"] if len(rows) > limit else None}
            if method == "GET" and len(parts) == 4 and parts[2] in ("records", "artifacts"):
                table = parts[2]
                row = self.engine.store.db.execute(f"SELECT payload FROM {table} WHERE task_id=? AND id=?", (task, parts[3])).fetchone()
                if not row:
                    raise Rejected("Missing or cross-task evidence")
                item = json.loads(row[0])
                if table == "records":
                    return 200, self.engine.store.get(ref(item), task)
                return 200, {"reference": item, "content": self.engine.store.read_artifact(item, task)}
            if method == "GET" and parts[2:] == ["stream"]:
                state = self.engine.task(task)["state"]
                after, limit = page(query)
                if after > state["last_event_sequence"]:
                    raise Rejected("Event cursor is ahead of task")
                rows = self.engine.store.db.execute("SELECT payload FROM events WHERE task_id=? AND sequence>? ORDER BY sequence LIMIT ?", (task, after, limit)).fetchall()
                return 200, [json.loads(r[0]) for r in rows]
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
        except (Rejected, KeyError, TypeError, ValueError, OSError):
            return 409, {"error": "Request rejected; inspect current task and approval"}


def serve(engine, port=8080):
    app = Application(engine)

    class Handler(BaseHTTPRequestHandler):
        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            super().end_headers()

        def respond(self, code, encoded, content_type):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *_):
            pass  # Do not log authorization headers or query strings.

        def handle_request(self):
            expected_host = "127.0.0.1:" + str(self.server.server_port)
            if (self.headers.get("Host") != expected_host
                    or self.headers.get("Origin") not in (None, "http://" + expected_host)
                    or self.headers.get("Sec-Fetch-Site") == "cross-site"):
                self.send_error(403)
                return
            assets = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
            if self.command == "GET" and self.path in assets:
                name, mime = assets[self.path]
                self.respond(200, (Path(__file__).with_name("ui") / name).read_bytes(), mime)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 4096:
                    raise ValueError()
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError()
                if self.command == "POST" and self.headers.get_content_type() != "application/json":
                    raise ValueError()
                body = json.loads(self.rfile.read(length)) if length else {}
                if not isinstance(body, dict):
                    raise ValueError()
            except (ValueError, UnicodeDecodeError):
                self.send_error(400)
                return
            authorization = self.headers.get("Authorization", "")
            token = authorization[7:] if authorization.startswith("Bearer ") else ""
            path = self.path
            streaming = self.command == "GET" and urlsplit(path).path.endswith("/stream")
            if streaming and self.headers.get("Last-Event-ID") is not None:
                if urlsplit(path).query:
                    self.send_error(400)
                    return
                cursor = self.headers["Last-Event-ID"]
                if not cursor.isascii() or not cursor.isdecimal() or len(cursor) > 19:
                    self.send_error(400)
                    return
                path += "?after=" + cursor + "&limit=100"
            code, output = app.dispatch(self.command, path, body, token)
            if streaming and code == 200:
                encoded = ("retry: 2000\n\n" + "".join(f"id: {e['sequence']}\nevent: task_event\ndata: {json.dumps(e)}\n\n" for e in output)).encode()
                self.respond(code, encoded, "text/event-stream; charset=utf-8")
                return
            encoded = json.dumps(output).encode()
            self.respond(code, encoded, "application/json")

        do_GET = handle_request
        do_POST = handle_request

    server = HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 5
    print(f"Local API: http://127.0.0.1:{server.server_port}")
    print(f"Operator session (this process only): {app.session}", flush=True)
    server.serve_forever()
