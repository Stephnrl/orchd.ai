"""The one outbound path, exercised over a real socket against a server that checks it.\n\nThese tests do not mock the send. A local origin receives the request, asserts what arrived\n— including that the Authorization header the broker added is there and that the body is the\napproved one — and the refusals are measured by what the server never received at all.\n"""
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import sys
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

import secrets as randomness

from orch.__main__ import main
from orch import dispatch
from orch.broker import ServiceClient
from orch.broker_service import BrokerService
from orch.contracts import Rejected, canonical, digest, uid

TOKEN = "ghp_exampleexampleexampleexample1234"


class Origin:
    """A loopback server that records what it was asked to do and answers as told."""

    def __init__(self):
        self.seen = []
        self.reply = (201, {"number": 42, "html_url": "https://example.invalid/issues/42"})
        self.location = None
        self.padding = 0
        self.reads = {}          # path prefix -> (status, body)
        self.read_status = 200
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.seen.append({"path": self.path, "method": self.command,
                                   "authorization": self.headers.get("Authorization"),
                                   "accept": self.headers.get("Accept"),
                                   "user_agent": self.headers.get("User-Agent"),
                                   "encoding": self.headers.get("Accept-Encoding"), "body": ""})
                body = {"unconfigured": True}
                for prefix, value in outer.reads.items():
                    if self.path.startswith(prefix):
                        body = value
                        break
                raw = json.dumps(body).encode()
                if outer.padding:
                    raw = json.dumps({"filler": "x" * outer.padding}).encode()
                self.send_response(outer.read_status)
                if outer.location:
                    self.send_header("Location", outer.location)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                outer.seen.append({"path": self.path, "method": self.command,
                                   "authorization": self.headers.get("Authorization"),
                                   "accept": self.headers.get("Accept"),
                                   "user_agent": self.headers.get("User-Agent"),
                                   "encoding": self.headers.get("Accept-Encoding"),
                                   "body": self.rfile.read(length).decode()})
                status, payload = outer.reply
                raw = json.dumps(payload).encode()
                if outer.padding:
                    raw = json.dumps({"filler": "x" * outer.padding}).encode()
                self.send_response(status)
                if outer.location:
                    self.send_header("Location", outer.location)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def origin(self):
        return "http://127.0.0.1:" + str(self.port)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.origin = Origin()
        self.addCleanup(self.origin.close)
        self.credential = self.write_credential(self.origin.origin, TOKEN)

    def write_credential(self, origin, token, name="credential"):
        path = self.root / name
        path.write_text("origin=" + origin + "\ntoken=" + token + "\n", encoding="ascii")
        os.chmod(path, 0o600)
        return path

    def request(self, **changes):
        base = {"method": "POST", "url": self.origin.origin + "/repos/a/b/issues",
                "headers": {"Accept": "application/vnd.github+json"},
                "body": {"title": "Add rate limiting", "labels": []}}
        base.update(changes)
        return base

    def send(self, request=None, credential=None, sha=None):
        request = request or self.request()
        credential = credential if credential is not None else dispatch.read_credential(self.credential)
        return dispatch.perform("a" * 32, request, sha or digest(request), credential)

    # The credential file

    def test_a_credential_is_an_origin_and_a_token(self):
        loaded = dispatch.read_credential(self.credential)
        self.assertEqual(loaded["token"], TOKEN)
        self.assertEqual(loaded["origin"], "http://127.0.0.1:" + str(self.origin.port))

    def test_a_credential_must_be_private_on_posix(self):
        exposed = self.write_credential(self.origin.origin, TOKEN, "exposed")
        os.chmod(exposed, 0o644)
        if os.name == "nt":
            # Windows has no mode bits to check; the file still loads and the POSIX rule
            # is asserted on the platform that has one. Skipping would spend a Docker skip.
            self.assertEqual(dispatch.read_credential(exposed)["token"], TOKEN)
        else:
            with self.assertRaisesRegex(Rejected, "readable only by its owner"):
                dispatch.read_credential(exposed)

    def test_a_credential_file_must_say_exactly_origin_and_token(self):
        for text, complaint in (("token=" + TOKEN + "\n", "origin= and token="),
                                ("origin=" + self.origin.origin + "\n", "origin= and token="),
                                ("origin=x\norigin=y\ntoken=t\n", "origin= and token="),
                                ("origin=" + self.origin.origin + "\ntoken=\n", "empty or unusable"),
                                ("origin=" + self.origin.origin + "\ntoken=has space\n", "empty or unusable")):
            path = self.root / "broken"
            path.write_text(text, encoding="ascii")
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(Rejected, complaint):
                dispatch.read_credential(path)

    def test_a_token_may_not_travel_in_clear_to_a_real_host(self):
        path = self.write_credential("http://api.github.com", TOKEN, "cleartext")
        with self.assertRaisesRegex(Rejected, "https unless it is loopback"):
            dispatch.read_credential(path)

    def test_a_missing_credential_is_a_refusal_not_an_anonymous_request(self):
        with self.assertRaisesRegex(Rejected, "credential file missing"):
            dispatch.read_credential(self.root / "absent")

    # What may be sent

    def test_only_the_approved_request_is_sent(self):
        request = self.request()
        with self.assertRaisesRegex(Rejected, "not the one that was approved"):
            dispatch.perform("a" * 32, request, "0" * 64, dispatch.read_credential(self.credential))
        self.assertEqual(self.origin.seen, [], "nothing may reach the origin")

    def test_a_credential_is_never_sent_to_an_origin_it_does_not_name(self):
        elsewhere = self.request(url="https://api.github.com/repos/a/b/issues")
        with self.assertRaisesRegex(Rejected, "credential is not for https://api.github.com"):
            self.send(elsewhere)
        self.assertEqual(self.origin.seen, [])

    def test_a_request_may_not_carry_its_own_authentication(self):
        for header in ("Authorization", "Cookie", "Proxy-Authorization"):
            bad = self.request(headers={header: "Bearer smuggled"})
            with self.assertRaisesRegex(Rejected, "may not carry its own authentication"):
                self.send(bad)
        self.assertEqual(self.origin.seen, [])

    def test_a_dispatch_writes_and_does_not_read(self):
        with self.assertRaisesRegex(Rejected, "performs a write, not a GET"):
            self.send(self.request(method="GET"))

    def test_a_request_carrying_a_secret_never_leaves(self):
        bad = self.request(body={"title": "x", "note": "token: ghp_aaaaaaaaaaaaaaaaaaaa"})
        with self.assertRaisesRegex(Rejected, "recognized secret"):
            self.send(bad)
        self.assertEqual(self.origin.seen, [])

    def test_a_url_may_not_smuggle_credentials_or_a_fragment(self):
        for url, complaint in ((self.origin.origin.replace("http://", "http://user:pw@") + "/x", "must not carry credentials"),
                               (self.origin.origin + "/x#frag", "must not carry a fragment"),
                               ("ftp://example.invalid/x", "http or https")):
            with self.assertRaisesRegex(Rejected, complaint):
                self.send(self.request(url=url))

    # What actually happens on the wire

    def test_a_successful_dispatch_sends_the_approved_bytes_with_the_broker_s_token(self):
        request = self.request()
        receipt = self.send(request)
        self.assertEqual(receipt["outcome"], "succeeded")
        self.assertEqual(receipt["response"]["status"], 201)
        self.assertEqual(json.loads(receipt["response"]["body"])["number"], 42)
        self.assertIs(receipt["simulated"], False)
        self.assertEqual(receipt["request_sha256"], digest(request))
        [seen] = self.origin.seen
        self.assertEqual(seen["authorization"], "Bearer " + TOKEN)
        self.assertEqual(seen["accept"], "application/vnd.github+json")
        self.assertEqual(seen["body"], canonical(request["body"]).decode())
        self.assertEqual(seen["path"], "/repos/a/b/issues")

    def test_every_request_identifies_itself_and_refuses_compression(self):
        # GitHub answers a request with no User-Agent with a 403 and an HTML body, which no
        # loopback origin would ever produce; a real call found this and a test keeps it found.
        # Identity encoding keeps a response from arriving compressed and unparseable.
        self.send()
        self.assertEqual(self.origin.seen[0]["user_agent"], "orchd")
        self.assertEqual(self.origin.seen[0]["encoding"], "identity")

    def test_the_token_never_appears_in_the_receipt(self):
        receipt = self.send()
        self.assertNotIn(TOKEN, canonical(receipt).decode())

    def test_a_refused_status_is_recorded_as_failed_not_uncertain(self):
        self.origin.reply = (422, {"message": "Validation failed"})
        receipt = self.send()
        self.assertEqual(receipt["outcome"], "failed")
        self.assertEqual(receipt["error"], "http_422")
        self.assertEqual(receipt["response"]["status"], 422)

    def test_a_redirect_is_recorded_and_never_followed(self):
        self.origin.reply = (302, {"moved": True})
        self.origin.location = "https://elsewhere.invalid/repos/a/b/issues"
        receipt = self.send()
        self.assertEqual(receipt["outcome"], "failed")
        self.assertEqual(receipt["error"], "redirect_not_followed")
        self.assertEqual(receipt["redirect_to"], "https://elsewhere.invalid:443")
        # The proof it was not followed: the origin saw one request, and only one.
        self.assertEqual(len(self.origin.seen), 1)

    def test_an_oversized_response_is_uncertain_rather_than_truncated(self):
        self.origin.padding = dispatch.MAX_RESPONSE + 100
        receipt = self.send()
        self.assertEqual(receipt["outcome"], "uncertain")
        self.assertEqual(receipt["error"], "response_too_large")

    def test_an_unreachable_origin_is_uncertain_and_carries_its_receipt(self):
        # The effect may or may not have happened, which is exactly what reconciliation is
        # for; a refusal before sending would have been safe to retry and this is not.
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()
        credential = dispatch.read_credential(self.write_credential("http://127.0.0.1:" + str(port), TOKEN, "dead"))
        request = self.request(url="http://127.0.0.1:" + str(port) + "/repos/a/b/issues")
        # Returned, never raised: Rejected is a ValueError here, and a caller catching it
        # broadly would drop the one record saying an effect may exist.
        receipt = dispatch.perform("a" * 32, request, digest(request), credential)
        self.assertEqual(receipt["outcome"], "uncertain")
        self.assertEqual(receipt["error"], "connection_failed")
        self.assertIsNone(receipt["response"])

    def test_no_outcome_after_the_bytes_go_is_ever_raised(self):
        self.origin.reply = (500, {"message": "boom"})
        self.assertEqual(self.send()["outcome"], "failed")
        self.origin.padding = dispatch.MAX_RESPONSE + 100
        self.assertEqual(self.send()["outcome"], "uncertain")

    def test_a_refusal_before_sending_says_so_in_its_message(self):
        # "Refused" and "uncertain" are different answers, and the wording is the only thing
        # a caller has to tell them apart before reading the receipt.
        with self.assertRaisesRegex(Rejected, "refused before sending"):
            self.send(self.request(method="DELETE"))


class BrokerDispatchTests(unittest.TestCase):
    """The route as an agent's work would actually reach it: authenticated, through the broker."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.origin = Origin()
        self.addCleanup(self.origin.close)
        self.secret = self.root / "secret"
        self.secret.write_text(randomness.token_hex(32) + "\n", encoding="ascii")
        os.chmod(self.secret, 0o600)
        self.credential = self.root / "credential"
        self.credential.write_text("origin=" + self.origin.origin + "\ntoken=" + TOKEN + "\n", encoding="ascii")
        os.chmod(self.credential, 0o600)

    def serve(self, credential=True):
        workspaces = self.root / "work"
        workspaces.mkdir(exist_ok=True)
        service = BrokerService(self.root / "journal", self.secret, workspaces, "trusted-fixture",
                                None, 0, None, str(self.credential) if credential else None)
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (service.shutdown(), thread.join(5)))
        return service, ServiceClient({"endpoint": "127.0.0.1:" + str(service.port), "secret": str(self.secret)})

    def message(self, request, sha=None):
        return {"schema_version": "1.0.0", "kind": "dispatch", "nonce": uid(),
                "operation_id": "a" * 32, "expected_sha256": sha or digest(request), "request": request}

    def request(self, **changes):
        base = {"method": "POST", "url": self.origin.origin + "/repos/a/b/issues",
                "headers": {"Accept": "application/vnd.github+json"},
                "body": {"title": "Add rate limiting"}}
        base.update(changes)
        return base

    def test_the_broker_dispatches_and_the_caller_never_sees_the_token(self):
        service, client = self.serve()
        request = self.request()
        reply = client.call("dispatch", self.message(request))
        self.assertEqual(reply["receipt"]["outcome"], "succeeded")
        self.assertEqual(reply["receipt"]["request_sha256"], digest(request))
        self.assertIs(reply["receipt"]["simulated"], False)
        # The credential reached the origin; it never reached the caller.
        self.assertEqual(self.origin.seen[0]["authorization"], "Bearer " + TOKEN)
        self.assertNotIn(TOKEN, canonical(reply).decode())

    def test_a_broker_without_a_credential_refuses_rather_than_sending_anonymously(self):
        service, client = self.serve(credential=False)
        self.assertIs(service.profile["dispatch"], False)
        with self.assertRaises(Rejected):
            client.call("dispatch", self.message(self.request()))
        self.assertEqual(self.origin.seen, [])

    def test_the_profile_says_whether_this_broker_can_reach_outside(self):
        service, client = self.serve()
        self.assertIs(service.profile["dispatch"], True)
        self.assertIs(client.call("identity", {"schema_version": "1.0.0", "kind": "identity",
                                               "nonce": uid()})["profile"]["dispatch"], True)

    def test_a_request_that_is_not_the_approved_one_never_reaches_the_origin(self):
        service, client = self.serve()
        with self.assertRaises(Rejected):
            client.call("dispatch", self.message(self.request(), sha="0" * 64))
        self.assertEqual(self.origin.seen, [])

    def test_an_unusable_credential_stops_the_broker_at_startup(self):
        self.credential.write_text("token=only\n", encoding="ascii")
        os.chmod(self.credential, 0o600)
        with self.assertRaisesRegex(Rejected, "origin= and token="):
            self.serve()

    def test_a_withdrawn_credential_stops_dispatch_without_a_restart(self):
        service, client = self.serve()
        self.assertEqual(client.call("dispatch", self.message(self.request()))["receipt"]["outcome"], "succeeded")
        self.credential.unlink()
        with self.assertRaises(Rejected):
            client.call("dispatch", self.message(self.request()))


class FetchTests(unittest.TestCase):
    """Reads, which nothing in this project could perform until now."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.origin = Origin()
        self.addCleanup(self.origin.close)
        path = self.root / "credential"
        path.write_text("origin=" + self.origin.origin + "\ntoken=" + TOKEN + "\n", encoding="ascii")
        os.chmod(path, 0o600)
        self.credential = dispatch.read_credential(path)

    def reads(self, **changes):
        base = {"repository": {"method": "GET", "url": self.origin.origin + "/repos/a/b",
                               "headers": {"Accept": "application/vnd.github+json"}},
                "labels": {"method": "GET", "url": self.origin.origin + "/repos/a/b/labels"}}
        base.update(changes)
        return base

    def test_a_fetch_returns_the_capture_a_codec_expects(self):
        self.origin.reads = {"/repos/a/b/labels": [{"name": "enhancement"}], "/repos/a/b": {"id": 1}}
        capture = dispatch.fetch("a" * 64, self.reads(), self.credential)
        self.assertEqual(capture["plan_sha256"], "a" * 64)
        self.assertEqual(sorted(capture["responses"]), ["labels", "repository"])
        self.assertEqual(capture["responses"]["repository"]["body"], {"id": 1})
        self.assertIs(capture["responses"]["labels"]["redirected"], False)
        self.assertEqual(capture["responses"]["labels"]["status"], 200)

    def test_the_credential_goes_with_every_read(self):
        dispatch.fetch("a" * 64, self.reads(), self.credential)
        self.assertEqual({seen["authorization"] for seen in self.origin.seen}, {"Bearer " + TOKEN})
        self.assertEqual({seen["method"] for seen in self.origin.seen}, {"GET"})

    def test_every_read_identifies_itself_and_refuses_compression(self):
        dispatch.fetch("a" * 64, self.reads(), self.credential)
        self.assertEqual({seen["user_agent"] for seen in self.origin.seen}, {"orchd"})
        self.assertEqual({seen["encoding"] for seen in self.origin.seen}, {"identity"})

    def test_a_fetch_reads_and_does_not_write(self):
        with self.assertRaisesRegex(Rejected, "reads; it does not POST"):
            dispatch.fetch("a" * 64, self.reads(bad={"method": "POST", "url": self.origin.origin + "/x"}),
                           self.credential)
        self.assertEqual(self.origin.seen, [])

    def test_a_read_may_not_leave_the_credential_s_origin(self):
        away = self.reads(other={"method": "GET", "url": "https://api.github.com/repos/a/b"})
        with self.assertRaisesRegex(Rejected, "credential is not for https://api.github.com"):
            dispatch.fetch("a" * 64, away, self.credential)
        self.assertEqual(self.origin.seen, [])

    def test_a_read_may_not_carry_its_own_authentication(self):
        bad = self.reads(other={"method": "GET", "url": self.origin.origin + "/x",
                                "headers": {"Authorization": "Bearer smuggled"}})
        with self.assertRaisesRegex(Rejected, "may not carry its own authentication"):
            dispatch.fetch("a" * 64, bad, self.credential)
        self.assertEqual(self.origin.seen, [])

    def test_a_plan_is_bounded_and_named(self):
        with self.assertRaisesRegex(Rejected, "1 to 8 reads"):
            dispatch.fetch("a" * 64, {}, self.credential)
        many = {("read_%d" % n): {"method": "GET", "url": self.origin.origin + "/x"} for n in range(9)}
        with self.assertRaisesRegex(Rejected, "1 to 8 reads"):
            dispatch.fetch("a" * 64, many, self.credential)
        with self.assertRaisesRegex(Rejected, "sha256 of the plan"):
            dispatch.fetch("not-a-hash", self.reads(), self.credential)

    def test_a_redirect_is_reported_rather_than_followed(self):
        self.origin.read_status = 302
        self.origin.location = "https://elsewhere.invalid/repos/a/b"
        capture = dispatch.fetch("a" * 64, self.reads(), self.credential)
        self.assertIs(capture["responses"]["repository"]["redirected"], True)
        # Two reads were asked for and exactly two requests were made: neither was chased.
        self.assertEqual(len(self.origin.seen), 2)

    def test_an_oversized_or_unparseable_response_is_refused(self):
        self.origin.padding = dispatch.MAX_RESPONSE + 100
        with self.assertRaisesRegex(Rejected, "exceeds the read budget"):
            dispatch.fetch("a" * 64, self.reads(), self.credential)


class WholeLoopTests(unittest.TestCase):
    """Plan, fetch, preview, dispatch: the sequence a project manager's issue would take.\n\nEvery step here is the real one. The reads and the write both cross a socket, the codec\nchecks the capture against the plan it asked for, and the write that goes out is the one\nthe preview produced and nothing else.\n"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.origin = Origin()
        self.addCleanup(self.origin.close)
        path = self.root / "credential"
        path.write_text("origin=" + self.origin.origin + "\ntoken=" + TOKEN + "\n", encoding="ascii")
        os.chmod(path, 0o600)
        self.credential = dispatch.read_credential(path)

    def codec(self):
        """The real codec, aimed at the local origin.\n\n`github_issues` hardcodes api.github.com on purpose — no host override, no inferred\ndestination — so the test substitutes where it points rather than giving the codec a\nway to be pointed elsewhere in production.\n"""
        from orch import github_issues as codec
        patcher = patch.object(codec, "API", self.origin.origin)
        patcher.start()
        self.addCleanup(patcher.stop)
        return codec

    def test_an_issue_goes_from_plan_to_created_without_a_person_pasting_json(self):
        codec = self.codec()

        profile = {"schema_version": "1.0.0", "codec": "github-rest-2026-03-10",
                   "repository": "a/b", "repository_id": 1}
        intent = {"schema_version": "1.0.0", "task_id": "a" * 32, "operation_id": "b" * 32,
                  "repository": "a/b", "title": "Add rate limiting",
                  "body": "Requests from one client should be limited.", "labels": ["enhancement"]}

        # The codec says what must be observed; the broker observes it.
        plan = codec.read_plan(intent, profile)
        self.origin.reads = {"/search/issues": {"total_count": 0, "items": []},
                             "/repos/a/b/labels": [{"name": "enhancement"}],
                             "/repos/a/b": {"id": 1, "full_name": "a/b", "archived": False,
                                            "disabled": False, "has_issues": True}}
        capture = dispatch.fetch(plan["sha256"], plan["binding"]["requests"], self.credential)

        # The codec checks that capture against the plan it asked for, and only then writes.
        preview = codec.preview_issue(intent, profile, capture)
        request = preview["binding"]["request"]

        # The approval is over this hash, and only a request matching it is sent.
        self.origin.reply = (201, {"number": 42, "html_url": "https://example.invalid/issues/42"})
        receipt = dispatch.perform(intent["operation_id"], request, digest(request), self.credential)

        self.assertEqual(receipt["outcome"], "succeeded")
        self.assertEqual(json.loads(receipt["response"]["body"])["number"], 42)
        writes = [seen for seen in self.origin.seen if seen["method"] == "POST"]
        self.assertEqual(len(writes), 1, "exactly one write, and only after the reads")
        self.assertEqual(json.loads(writes[0]["body"])["title"], intent["title"])
        self.assertIn(codec.MARKER % intent["operation_id"], json.loads(writes[0]["body"])["body"])
        self.assertNotIn(TOKEN, canonical(receipt).decode())

    def test_a_capture_from_another_plan_is_refused_before_anything_is_written(self):
        codec = self.codec()

        profile = {"schema_version": "1.0.0", "codec": "github-rest-2026-03-10",
                   "repository": "a/b", "repository_id": 1}
        intent = {"schema_version": "1.0.0", "task_id": "a" * 32, "operation_id": "b" * 32,
                  "repository": "a/b", "title": "Add rate limiting", "body": "", "labels": []}
        plan = codec.read_plan(intent, profile)
        self.origin.reads = {"/search/issues": {"total_count": 0, "items": []},
                             "/repos/a/b/labels": [], "/repos/a/b": {"id": 1, "full_name": "a/b",
                                                                     "archived": False, "disabled": False,
                                                                     "has_issues": True}}
        capture = dispatch.fetch("f" * 64, plan["binding"]["requests"], self.credential)
        with self.assertRaisesRegex(Rejected, "binding mismatch"):
            codec.preview_issue(intent, profile, capture)
        self.assertEqual([seen for seen in self.origin.seen if seen["method"] == "POST"], [])


class BrokerCommandTests(unittest.TestCase):
    """The two outbound commands as the runbook tells an operator to use them."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.origin = Origin()
        self.addCleanup(self.origin.close)
        self.secret = self.root / "secret"
        self.secret.write_text(randomness.token_hex(32) + "\n", encoding="ascii")
        os.chmod(self.secret, 0o600)
        credential = self.root / "credential"
        credential.write_text("origin=" + self.origin.origin + "\ntoken=" + TOKEN + "\n", encoding="ascii")
        os.chmod(credential, 0o600)
        workspaces = self.root / "work"
        workspaces.mkdir()
        self.service = BrokerService(self.root / "journal", self.secret, workspaces, "trusted-fixture",
                                     None, 0, None, str(credential))
        thread = threading.Thread(target=self.service.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.service.shutdown(), thread.join(5)))

    def write(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    def cli(self, *argv, expect=0):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["orch", *argv, "--broker-endpoint",
                                        "127.0.0.1:" + str(self.service.port),
                                        "--broker-secret", str(self.secret)]),                 redirect_stdout(out), redirect_stderr(err):
            try:
                code = 0
                main()
            except SystemExit as exit:
                code = exit.code
        self.assertEqual(code, expect, err.getvalue())
        return json.loads(out.getvalue()) if out.getvalue() else err.getvalue()

    def preview(self, **changes):
        request = {"method": "POST", "url": self.origin.origin + "/repos/a/b/issues",
                   "headers": {"Accept": "application/vnd.github+json"},
                   "body": {"title": "Add rate limiting"}}
        request.update(changes)
        return {"kind": "GitHubIssuePreview",
                "binding": {"operation_id": "a" * 32, "request": request}, "sha256": "x"}

    def test_broker_fetch_performs_a_plan_from_a_file(self):
        self.origin.reads = {"/repos/a/b": {"id": 1}}
        plan = {"sha256": "a" * 64, "binding": {"requests": {
            "repository": {"method": "GET", "url": self.origin.origin + "/repos/a/b"}}}}
        capture = self.cli("broker-fetch", "--plan", self.write("plan.json", plan))
        self.assertEqual(capture["plan_sha256"], "a" * 64)
        self.assertEqual(capture["responses"]["repository"]["body"], {"id": 1})

    def test_broker_dispatch_sends_the_request_the_preview_describes(self):
        receipt = self.cli("broker-dispatch", "--preview", self.write("preview.json", self.preview()))
        self.assertEqual(receipt["outcome"], "succeeded")
        self.assertEqual(self.origin.seen[0]["authorization"], "Bearer " + TOKEN)
        self.assertNotIn(TOKEN, json.dumps(receipt))

    def test_an_unsuccessful_dispatch_exits_non_zero_so_a_script_stops(self):
        # An uncertain outcome especially: the effect may exist, and a script that carried on
        # would either retry it or report a success nobody observed.
        self.origin.reply = (422, {"message": "Validation failed"})
        receipt = self.cli("broker-dispatch", "--preview", self.write("preview.json", self.preview()),
                           expect=2)
        self.assertEqual(receipt["outcome"], "failed")

    def test_both_commands_insist_on_their_inputs(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["orch", "broker-dispatch"]),                 redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(SystemExit):
                main()
        self.assertIn("--preview", err.getvalue())


if __name__ == "__main__":
    unittest.main()
