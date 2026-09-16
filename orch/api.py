"""Small localhost HTTP boundary. Session identity never comes from request JSON."""
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit, parse_qs

from .contracts import Rejected, ref
from .operator_session import OperatorSession


@dataclass(frozen=True)
class BundleDownload:
    content: bytes
    sha256: str


@dataclass(frozen=True)
class BundleUpload:
    content: bytes
    sha256: str


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
    def __init__(self, engine, sessions=None):
        self.engine = engine
        self.sessions = sessions if sessions is not None else OperatorSession()

    @property
    def session(self):
        return self.sessions.token

    def dispatch(self, method, path, payload, token):
        if not self.sessions.authenticate(token, touch=path != '/session'):
            return 401, {"error": "Operator session required"}
        try:
            parsed = urlsplit(path)
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=10)
            parts = parsed.path.strip("/").split("/")
            if parts[0] == 'batches':
                from .batches import Batches
                batches = Batches(self.engine)
                if method == 'GET' and parts == ['batches']:
                    if payload or set(query) - {'after'} or ('after' in query and len(query['after']) != 1):
                        raise Rejected('Invalid batch list fields')
                    return 200, batches.list(query['after'][0] if 'after' in query else None)
                if query: raise Rejected('Unexpected batch query')
                if method == 'POST' and parts == ['batches']:
                    return 201, batches.create(payload)
                if method == 'GET' and len(parts) == 2:
                    if payload: raise Rejected('Batch inspection accepts no payload')
                    return 200, batches.inspect(parts[1])
                if method == 'POST' and len(parts) == 3 and parts[2] in ('run', 'abandon'):
                    required = {'scope_sha256', 'expected_revision'}
                    if parts[2] == 'abandon': required |= {'task_id', 'expected_snapshot_sha256'}
                    if not isinstance(payload, dict) or set(payload) != required:
                        raise Rejected('Explicit batch scope required')
                    if parts[2] == 'run':
                        return 200, batches.run(parts[1], payload['scope_sha256'], payload['expected_revision'])
                    return 200, batches.abandon(parts[1], payload['scope_sha256'], payload['expected_revision'],
                                                payload['task_id'], payload['expected_snapshot_sha256'])
                return 405, {'error': 'Unsupported batch route or method'}
            if method == 'POST' and parts == ['repository-tasks']:
                if query or not isinstance(payload, dict) or set(payload) != {'title', 'intent'}:
                    raise Rejected('Explicit repository task title and intent required')
                task = self.engine.create_repository_task(payload['intent'], payload['title'])
                return 201, self.engine.task(task)
            if method == "GET" and parts == ["tasks"]:
                after, limit = page(query)
                rows = self.engine.store.db.execute("SELECT rowid,id,state,context FROM tasks WHERE rowid>? ORDER BY rowid LIMIT ?", (after, limit + 1)).fetchall()
                items = []
                for row in rows[:limit]:
                    state = json.loads(row["state"])
                    # A task still being drafted has no specification yet, so its title is the
                    # one it was opened with. See docs/draft-spec.md.
                    spec = self.engine.store.get(state["spec"], row["id"], "TaskSpec") if state["spec"] else None
                    title = spec["title"] if spec else json.loads(row["context"]).get("draft_title")
                    items.append({"task_id": row["id"], "title": title, "state": state})
                return 200, {"items": items,
                             "next": rows[limit-1]["rowid"] if len(rows) > limit else None}
            if query and not (method == "GET" and len(parts) == 3 and parts[2] in ("records", "stream")):
                raise Rejected("Unexpected query")
            if parts in (['session'], ['session', 'rotate'], ['session', 'revoke']):
                if payload:
                    raise Rejected('Session operations accept no payload fields')
                if method == 'GET' and parts == ['session']:
                    return 200, self.sessions.status(token)
                if method == 'POST' and parts == ['session', 'rotate']:
                    return 200, self.sessions.rotate(token)
                if method == 'POST' and parts == ['session', 'revoke']:
                    return 200, self.sessions.revoke(token)
                return 405, {'error': 'Unsupported session method'}
            if method == 'POST' and parsed.path == '/evidence-bundles/verify':
                from .github_bundle import verify_bundle_bytes
                if not isinstance(payload, BundleUpload):
                    raise Rejected('Binary bundle upload required')
                return 200, verify_bundle_bytes(payload.content, payload.sha256)
            if method == "GET" and parts == ["storage"]:
                from .maintenance import storage_usage
                return 200, storage_usage(self.engine.store)
            if method == "GET" and parts == ["pilot-usage"]:
                from .pilot import Pilot
                from .pilot_retention import usage
                root = self.engine.store.root / "repository-pilots"
                if not (root / "pilot.sqlite").is_file():
                    raise Rejected("Repository pilot journal is missing")
                pilot = Pilot(root, read_only=True)
                try:
                    report = usage(pilot)
                finally:
                    pilot.close()
                return 200, {key: value for key, value in report.items() if key != "journal_root"}  # No host paths over HTTP.
            if method == "GET" and parts == ["integrity-report"]:
                from .maintenance import integrity_report
                return 200, integrity_report(self.engine.store)
            if method == "POST" and parts == ["tasks"]:
                if set(payload) - {"title"}:
                    raise Rejected("Unknown create fields")
                return 201, {"task_id": self.engine.create_task(payload.get("title", "Greeting fixture"))}
            if len(parts) < 2 or parts[0] != "tasks":
                return 404, {"error": "Unknown route"}
            task = parts[1]
            if method == 'POST' and parts[2:] == ['evidence-bundle']:
                from .github_bundle import build_bundle
                if not isinstance(payload, dict) or set(payload) != {'patch', 'test', 'review', 'policy'}:
                    raise Rejected('Explicit evidence selection required')
                raw, report = build_bundle(self.engine.store, {'task_id': task, **payload})
                return 200, BundleDownload(raw, report['binding']['bundle_sha256'])
            if method == 'GET' and parts[2:] == ['evidence-catalog']:
                from .github_evidence import list_evidence_records
                return 200, list_evidence_records(self.engine.store, task)
            if method == 'POST' and parts[2:] == ['assess-evidence']:
                from .github_evidence import resolve_record_selection, check_record_claims
                if not isinstance(payload, dict) or set(payload) != {'review', 'policy'}:
                    raise Rejected('Explicit review and policy references required')
                selection = resolve_record_selection(self.engine.store, {'task_id': task, **payload})
                assessment = check_record_claims(self.engine.store, selection)
                return 200, {'selection': selection, 'assessment': assessment}
            if method == "GET" and parts[2:] == ["recovery-diagnostics"]:
                return 200, self.engine.recovery_diagnostics(task)
            if method == "GET" and parts[2:] == ["storage"]:
                from .maintenance import storage_usage
                return 200, storage_usage(self.engine.store, task)
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
            if method == "POST" and parts[2:] == ["renew-approval"]:
                if set(payload) != {"request_id", "expected_revision"}:
                    raise Rejected("Invalid renewal fields")
                return 200, self.engine.renew_approval(task, **payload)
            if method == "POST" and parts[2:] == ["recover"]:
                if set(payload) != {"expected_revision"} or type(payload["expected_revision"]) is not int:
                    raise Rejected("Invalid recovery fields")
                return 200, self.engine.recover(task, payload["expected_revision"])
            if method == "POST" and parts[2:] == ["retry-cleanup"]:
                if set(payload) != {"operation_id", "expected_revision"} or not isinstance(payload["operation_id"], str):
                    raise Rejected("Invalid cleanup fields")
                return 200, self.engine.retry_cleanup(task, **payload)
            if method == "POST" and parts[2:] == ["cancel"]:
                if set(payload) != {"expected_revision", "reason"}:
                    raise Rejected("Invalid cancellation fields")
                return 200, self.engine.cancel(task, payload["expected_revision"], payload["reason"])
            return 404, {"error": "Unknown route"}
        except (Rejected, KeyError, TypeError, ValueError, OSError, sqlite3.Error):
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
            if (len(self.headers.get_all('Host', [])) != 1 or len(self.headers.get_all('Origin', [])) > 1
                    or len(self.headers.get_all('Sec-Fetch-Site', [])) > 1
                    or self.headers.get("Host") != expected_host
                    or self.headers.get("Origin") not in (None, "http://" + expected_host)
                    or self.headers.get("Sec-Fetch-Site") == "cross-site"):
                self.send_error(403)
                return
            assets = {"/batches.js": ("batches.js", "text/javascript; charset=utf-8"), "/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
            if self.command == "GET" and self.path in assets:
                name, mime = assets[self.path]
                self.respond(200, (Path(__file__).with_name("ui") / name).read_bytes(), mime)
                return
            authorization = self.headers.get('Authorization', '')
            token = authorization[7:] if authorization.startswith('Bearer ') else ''
            if len(self.headers.get_all('Authorization', [])) != 1 or not app.sessions.authenticate(token, touch=False):
                self.respond(401, b'{"error":"Operator session required; reconnect or restart the server"}', 'application/json')
                return
            if (len(self.headers.get_all('Content-Length', [])) > 1
                    or len(self.headers.get_all('Content-Type', [])) > 1
                    or len(self.headers.get_all('Last-Event-ID', [])) > 1
                    or self.headers.get_all('Transfer-Encoding') or self.headers.get_all('Content-Encoding')):
                self.send_error(400)
                return
            if self.command == 'POST' and self.path == '/evidence-bundles/verify':
                from .github_bundle import MAX_BUNDLE_BYTES
                length = self.headers.get('Content-Length', '')
                expected = self.headers.get('X-Orch-Expected-SHA256', '')
                if (len(self.headers.get_all('Content-Length', [])) != 1
                        or len(self.headers.get_all('Content-Type', [])) != 1
                        or len(self.headers.get_all('X-Orch-Expected-SHA256', [])) != 1
                        or not re.fullmatch('[0-9]{1,8}', length)
                        or not 0 < int(length) <= MAX_BUNDLE_BYTES
                        or self.headers.get('Content-Type') != 'application/octet-stream'
                        or self.headers.get_all('Transfer-Encoding')
                        or self.headers.get_all('Content-Encoding')
                        or not re.fullmatch('[a-f0-9]{64}', expected)):
                    self.send_error(400)
                    return
                try:
                    raw = self.rfile.read(int(length))
                    if len(raw) != int(length):
                        raise ValueError('Incomplete upload')
                except (OSError, ValueError):
                    self.send_error(400)
                    return
                code, output = app.dispatch('POST', self.path, BundleUpload(raw, expected), token)
                self.respond(code, json.dumps(output).encode(), 'application/json')
                return
            try:
                raw_length = self.headers.get('Content-Length', '0')
                if not re.fullmatch('[0-9]{1,8}', raw_length): raise ValueError()
                length = int(raw_length)
                if not 0 <= length <= 4096:
                    raise ValueError()
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError()
                if self.command == "POST" and self.headers.get_content_type() != "application/json":
                    raise ValueError()
                raw = self.rfile.read(length)
                if len(raw) != length: raise ValueError()
                def unique(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result: raise ValueError('Duplicate JSON key')
                        result[key] = value
                    return result
                def invalid_number(_):
                    raise ValueError('Non-JSON number')
                body = json.loads(raw.decode('utf-8'), object_pairs_hook=unique, parse_constant=invalid_number) if length else {}
                if not isinstance(body, dict):
                    raise ValueError()
                if self.command == 'GET' and body: raise ValueError()
            except (ValueError, UnicodeDecodeError, RecursionError, OSError):
                self.send_error(400)
                return
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
            if isinstance(output, BundleDownload):
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(output.content)))
                self.send_header('Content-Disposition', 'attachment; filename="orchd-evidence-bundle.json"')
                self.send_header('X-Orch-Bundle-SHA256', output.sha256)
                self.end_headers()
                self.wfile.write(output.content)
                return
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
