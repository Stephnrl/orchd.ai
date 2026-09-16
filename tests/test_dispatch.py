"""The one outbound path, exercised over a real socket against a server that checks it.\n\nThese tests do not mock the send. A local origin receives the request, asserts what arrived\n— including that the Authorization header the broker added is there and that the body is the\napproved one — and the refusals are measured by what the server never received at all.\n"""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest

import secrets as randomness

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
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                outer.seen.append({"path": self.path, "method": self.command,
                                   "authorization": self.headers.get("Authorization"),
                                   "accept": self.headers.get("Accept"),
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


if __name__ == "__main__":
    unittest.main()
