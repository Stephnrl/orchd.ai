import http.client
import json
import os
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import urlsplit

from orch.api import Application
from orch.engine import Engine
from orch.execution import Executor


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = Engine(self.temp.name, Executor("trusted-fixture"))
        self.app = Application(self.engine)
        self.task = self.engine.create_task("<img src=x onerror=alert(1)>")

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def get(self, path):
        return self.app.dispatch("GET", path, {}, self.app.session)

    def test_task_pages_and_closed_queries(self):
        other = self.engine.create_task()
        code, first = self.get("/tasks?limit=1")
        self.assertEqual(code, 200)
        self.assertEqual([x["task_id"] for x in first["items"]], [self.task])
        self.assertEqual(first["items"][0]["title"], "<img src=x onerror=alert(1)>")
        code, second = self.get(f"/tasks?after={first['next']}&limit=1")
        self.assertEqual([x["task_id"] for x in second["items"]], [other])
        self.assertEqual(second["items"][0]["title"], "Greeting fixture")
        self.assertIsNone(second["next"])
        for query in ("limit=101", "after=-1", "limit=0", "after=9223372036854775808", "limit=2&limit=3", "token=secret", "after=", "limit=abc"):
            self.assertEqual(self.get("/tasks?" + query)[0], 409)

    def test_task_titles_require_intact_task_bound_specs(self):
        self.assertEqual(self.app.dispatch("GET", "/tasks", {}, "wrong")[0], 401)
        reference = self.engine.task(self.task)["state"]["spec"]
        spec = self.engine.store.get(reference, self.task)
        spec["title"] = "Tampered title"
        # Simulate offline corruption in this disposable store, beyond its write guard.
        self.engine.store.db.execute("DROP TRIGGER records_update")
        self.engine.store.db.execute("UPDATE records SET payload=? WHERE id=?", (json.dumps(spec), reference["id"]))
        self.assertEqual(self.get("/tasks")[0], 409)

    def test_task_bound_records_artifacts_and_integrity(self):
        other = self.engine.create_task()
        record = self.engine.task(self.task)["state"]["spec"]
        code, spec = self.get(f"/tasks/{self.task}/records/{record['id']}")
        self.assertEqual(code, 200)
        self.assertEqual(spec["title"], "<img src=x onerror=alert(1)>")
        artifact = spec["request"]
        path = f"/tasks/{self.task}/artifacts/{artifact['artifact_id']}"
        self.assertEqual(self.get(path)[0], 200)
        self.assertEqual(self.get(f"/tasks/{other}/records/{record['id']}")[0], 409)
        self.assertEqual(self.get(f"/tasks/{other}/artifacts/{artifact['artifact_id']}")[0], 409)
        self.assertEqual(self.app.dispatch("GET", path, {}, "wrong")[0], 401)
        (self.engine.store.root / "artifacts" / artifact["sha256"]).write_text("tampered")
        self.assertEqual(self.get(path)[0], 409)

    def test_record_pages_and_event_replay_survive_restart(self):
        self.engine.run(self.task)
        expected = self.engine.events(self.task)
        after, actual = 0, []
        while True:
            code, events = self.get(f"/tasks/{self.task}/stream?after={after}&limit=2")
            self.assertEqual(code, 200)
            if not events:
                break
            actual += events
            after = events[-1]["sequence"]
        self.assertEqual(actual, expected)
        self.assertEqual(self.get(f"/tasks/{self.task}/stream?after={after+1}")[0], 409)
        code, records = self.get(f"/tasks/{self.task}/records?limit=1")
        self.assertIsNotNone(records["next"])
        code, next_records = self.get(f"/tasks/{self.task}/records?limit=1&after={records['next']}")
        self.assertNotEqual(records["items"][0]["id"], next_records["items"][0]["id"])
        old_token = self.app.session
        self.engine.close()
        self.engine = Engine(self.temp.name, Executor("trusted-fixture"))
        self.app = Application(self.engine)
        self.assertEqual(self.app.dispatch("GET", "/tasks", {}, old_token)[0], 401)
        self.assertEqual(self.get(f"/tasks/{self.task}/stream?after=0")[1], expected)

    def test_approval_evidence_does_not_approve_and_stale_revision_fails(self):
        self.engine.run(self.task)
        state = self.engine.task(self.task)["state"]
        path = f"/tasks/{self.task}/records/{state['pending_approval']['id']}"
        self.assertEqual(self.get(path)[1]["kind"], "plan")
        self.assertEqual(self.engine.task(self.task)["state"], state)
        request = {"request_id": state["pending_approval"]["id"], "decision": "approve", "expected_revision": state["revision"]-1}
        self.assertEqual(self.app.dispatch("POST", f"/tasks/{self.task}/approvals", request, self.app.session)[0], 409)


class ManagementHTTPTests(unittest.TestCase):
    def test_browser_origin_assets_and_authenticated_sse(self):
        with tempfile.TemporaryDirectory() as data:
            process = subprocess.Popen([sys.executable, "-m", "orch", "serve", "--trusted-fixture", "--data", data, "--port", "0"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                address = process.stdout.readline().strip().split(" ")[-1]
                token = process.stdout.readline().strip().split(" ")[-1]
                parsed = urlsplit(address)
                conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
                def request(method, path, headers=None, body=None):
                    conn.request(method, path, body, headers or {})
                    response = conn.getresponse()
                    return response.status, dict(response.getheaders()), response.read()
                for asset in ("/", "/app.js", "/style.css"):
                    code, headers, body = request("GET", asset)
                    self.assertEqual(code, 200)
                    self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
                    self.assertEqual(headers["Cache-Control"], "no-store")
                    self.assertNotIn(token.encode(), body)
                self.assertEqual(request("GET", "/tasks")[0], 401)
                auth = {"Authorization": "Bearer " + token, "Origin": address, "Content-Type": "application/json"}
                code, _, body = request("POST", "/tasks", auth, '{}')
                self.assertEqual(code, 201)
                task = json.loads(body)["task_id"]
                for changes in ({"Origin": "https://evil.example"}, {"Origin": "null"}, {"Host": "evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
                    self.assertEqual(request("GET", "/tasks", {**auth, **changes})[0], 403)
                self.assertEqual(request("GET", "/tasks", {**auth, "Authorization": token})[0], 401)
                self.assertEqual(request("POST", "/tasks", {**auth, "Content-Type": "text/plain"}, '{}')[0], 400)
                upload = {**auth, 'Content-Type': 'application/octet-stream', 'X-Orch-Expected-SHA256': 'a' * 64}
                self.assertEqual(request('POST', '/evidence-bundles/verify', upload, b'{}')[0], 409)
                for changes in ({'Content-Length': '16777217'}, {'Content-Length': '-1'}, {'Content-Length': '0'},
                                {'Content-Type': 'application/json'}, {'X-Orch-Expected-SHA256': 'bad'},
                                {'Content-Encoding': 'gzip'}, {'Transfer-Encoding': 'chunked'}):
                    self.assertEqual(request('POST', '/evidence-bundles/verify', {**upload, **changes}, b'')[0], 400)
                # No body is sent: authentication must reject before waiting for the declared upload.
                self.assertEqual(request('POST', '/evidence-bundles/verify', {**upload, 'Authorization': 'Bearer invalid', 'Content-Length': '100'}, b'')[0], 401)
                self.assertEqual(request('POST', '/evidence-bundles/verify', {**upload, 'Authorization': 'Bearer \u00e9'}, b'{}')[0], 401)
                self.assertEqual(request('POST', '/evidence-bundles/verify', {**upload, 'Origin': 'https://evil.example'}, b'{}')[0], 403)
                for duplicate in ('Content-Length', 'Content-Type', 'X-Orch-Expected-SHA256', 'Authorization'):
                    conn.putrequest('POST', '/evidence-bundles/verify')
                    for name, value in {**upload, 'Content-Length': '2'}.items():
                        conn.putheader(name, value)
                        if name == duplicate:
                            conn.putheader(name, value)
                    conn.endheaders()  # Reject ambiguous framing/identity before reading any bytes.
                    response = conn.getresponse()
                    self.assertEqual(response.status, 401 if duplicate == 'Authorization' else 400)
                    response.read()
                code, headers, body = request("GET", f"/tasks/{task}/stream", {**auth, "Last-Event-ID": "0"})
                self.assertEqual(code, 200)
                self.assertTrue(headers["Content-Type"].startswith("text/event-stream"))
                events = [json.loads(line[6:]) for line in body.decode().splitlines() if line.startswith("data: ")]
                self.assertEqual([e["sequence"] for e in events], list(range(1, len(events)+1)))
                self.assertEqual(request("GET", f"/tasks/{task}/stream", {**auth, "Last-Event-ID": "invalid"})[0], 400)
                self.assertEqual(request("GET", f"/tasks/{task}/stream", {**auth, "Last-Event-ID": str(events[-1]["sequence"])})[2], b"retry: 2000\n\n")
                self.assertEqual(request("GET", "/../orch.sqlite", auth)[0], 404)
                conn.close()
            finally:
                process.terminate()
                process.communicate(timeout=10)
