"""Real loopback transport smoke test; never connects to an external service."""
import http.client
import json
import os
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import urlsplit


class HTTPTests(unittest.TestCase):
    def test_operator_http_boundary(self):
        with tempfile.TemporaryDirectory() as data:
            command = [sys.executable, "-m", "orch", "serve", "--trusted-fixture", "--data", data, "--port", "0"]
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                address = process.stdout.readline().strip().split(" ")[-1]
                token = process.stdout.readline().strip().split(" ")[-1]
                parsed = urlsplit(address)
                self.assertEqual(parsed.hostname, "127.0.0.1")
                connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)

                def request(method, path, payload=None, authorized=True, origin=None):
                    headers = {"Content-Type": "application/json"}
                    if authorized:
                        headers["Authorization"] = "Bearer " + token
                    if origin:
                        headers["Origin"] = origin
                    connection.request(method, path, json.dumps(payload or {}), headers)
                    response = connection.getresponse()
                    raw = response.read()
                    return response.status, raw

                self.assertEqual(request("POST", "/tasks", authorized=False)[0], 401)
                self.assertEqual(request("POST", "/tasks", origin="https://untrusted.example")[0], 403)
                code, raw = request("POST", "/tasks")
                self.assertEqual(code, 201)
                task = json.loads(raw)["task_id"]
                code, raw = request("POST", f"/tasks/{task}/run")
                self.assertEqual(code, 200)
                state = json.loads(raw)["state"]
                self.assertEqual(state["state"], "AWAITING_PLAN_APPROVAL")
                code, _ = request("POST", f"/tasks/{task}/approvals", {"request_id": state["pending_approval"]["id"], "decision": "approve", "expected_revision": state["revision"]})
                self.assertEqual(code, 200)
                code, raw = request("POST", f"/tasks/{task}/run")
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(raw)["state"]["state"], "AWAITING_ACTION_APPROVAL")
                self.assertEqual(request("GET", f"/tasks/{task}/history")[0], 200)
                connection.close()
            finally:
                process.terminate()
                process.communicate(timeout=10)
