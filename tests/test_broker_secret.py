"""Broker shared-secret rotation: an overlap window, live re-read and freshness reporting."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import test_broker_service as service_tests
from orch.broker import (SECRET_MAX_AGE_SECONDS, SecretFile, ServiceClient, VERSION, assess_identity,
                         authenticate, load_secret, read_secrets, rotate_secret, sign)
from orch.contracts import Rejected, canonical, uid
from orch.deployment import deployment_check

ROOT = Path(__file__).resolve().parents[1]


def write(path, *values):
    path.write_text("\n".join(values) + "\n", encoding="ascii")
    os.chmod(path, 0o600)
    return path


class SecretFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = write(self.root / "secret", secrets.token_hex(32))

    def test_one_or_two_different_secrets_are_accepted_and_nothing_else(self):
        current = read_secrets(self.path)[0]
        self.assertEqual(len(current), 1)
        previous = secrets.token_hex(32)
        write(self.path, current[0].hex(), previous)
        keys, _ = read_secrets(self.path)
        self.assertEqual([k.hex() for k in keys], [current[0].hex(), previous])
        self.assertEqual(load_secret(self.path), current[0])  # The first line is the current secret.
        for bad in ([], [secrets.token_hex(32)] * 2, [secrets.token_hex(32)] * 3,
                    ["short"], ["g" * 64], [secrets.token_hex(32).upper()],
                    [secrets.token_hex(32), "not hex"], [secrets.token_hex(32) + "extra"]):
            with self.subTest(bad=[v[:12] for v in bad]):
                write(self.path, *bad) if bad else self.path.write_text("", encoding="ascii")
                os.chmod(self.path, 0o600)
                with self.assertRaises(Rejected):
                    read_secrets(self.path)
        write(self.path, *([secrets.token_hex(32)] * 2))  # Two identical secrets are not an overlap.
        with self.assertRaisesRegex(Rejected, "two different secrets"):
            read_secrets(self.path)

    def test_changes_are_picked_up_and_a_withdrawn_file_refuses(self):
        secret = SecretFile(self.path)
        first = secret.current
        self.assertEqual(secret.accepted, (first,))
        replacement = secrets.token_hex(32)
        write(self.path, replacement, first.hex())
        self.assertEqual([k.hex() for k in secret.accepted], [replacement, first.hex()])
        self.assertEqual(secret.current.hex(), replacement)
        self.assertTrue(secret.state()["rotating"])
        write(self.path, replacement)
        self.assertEqual(secret.accepted, (bytes.fromhex(replacement),))
        self.assertFalse(secret.state()["rotating"])
        # A file that becomes unreadable refuses rather than reusing a withdrawn secret.
        self.path.unlink()
        with self.assertRaises(Rejected):
            secret.accepted
        with self.assertRaises(Rejected):
            SecretFile(self.root / "missing")

    def test_age_is_reported_against_the_documented_maximum(self):
        secret = SecretFile(self.path)
        state = secret.state()
        self.assertEqual((state["accepted"], state["rotating"], state["stale"]), (1, False, False))
        self.assertLess(state["age_seconds"], 60)
        stamp = time.time() - SECRET_MAX_AGE_SECONDS - 60
        os.utime(self.path, (stamp, stamp))
        self.assertTrue(SecretFile(self.path).state()["stale"])
        # A secret is never refused for age; staleness is reported, not enforced.
        self.assertEqual(len(SecretFile(self.path).accepted), 1)

    def test_authenticate_returns_the_matching_secret_only(self):
        first, second = secrets.token_bytes(32), secrets.token_bytes(32)
        body = b'{"kind":"identity"}'
        tag = sign(second, "request", "identity", body)
        self.assertEqual(authenticate((first, second), "request", "identity", body, tag), second)
        self.assertIsNone(authenticate((first,), "request", "identity", body, tag))
        self.assertIsNone(authenticate((first, second), "reply", "identity", body, tag))
        self.assertIsNone(authenticate((first, second), "request", "execute", body, tag))
        self.assertIsNone(authenticate((first, second), "request", "identity", body + b" ", tag))
        for invalid in (None, "", "zz" * 32, tag[:-1]):
            self.assertIsNone(authenticate((first, second), "request", "identity", body, invalid))


class RotateSecretTests(unittest.TestCase):
    setUp = SecretFileTests.setUp

    def test_begin_keeps_the_previous_secret_and_complete_drops_it(self):
        original = load_secret(self.path)
        begun = rotate_secret(self.path)
        self.assertEqual((begun["stage"], begun["accepted"], begun["rotating"]), ("overlap", 2, True))
        keys, _ = read_secrets(self.path)
        self.assertEqual(len(keys), 2)
        self.assertEqual(keys[1], original)
        self.assertNotEqual(keys[0], original)
        self.assertNotIn(keys[0].hex(), canonical(begun).decode())  # A report never carries a secret.
        with self.assertRaisesRegex(Rejected, "already in progress"):
            rotate_secret(self.path)
        finished = rotate_secret(self.path, complete=True)
        self.assertEqual((finished["stage"], finished["accepted"], finished["rotating"]), ("completed", 1, False))
        self.assertEqual(read_secrets(self.path)[0], [keys[0]])
        with self.assertRaisesRegex(Rejected, "No rotation is in progress"):
            rotate_secret(self.path, complete=True)

    def test_the_rewritten_file_stays_private_and_unlinked(self):
        rotate_secret(self.path)
        info = self.path.stat()
        self.assertEqual(info.st_nlink, 1)
        if os.name != "nt":
            self.assertEqual(info.st_mode & 0o077, 0)
        self.assertEqual(len(list(self.path.parent.iterdir())), 1)  # No temporary file is left behind.
        write(self.path, "not a secret")
        with self.assertRaises(Rejected):
            rotate_secret(self.path)
        self.assertEqual(self.path.read_text(encoding="ascii").strip(), "not a secret")

    def test_cli_stages_refuse_out_of_order_use(self):
        env = {**os.environ, "PYTHONPATH": str(ROOT)}

        def cli(*extra, expect=0):
            result = subprocess.run([sys.executable, "-m", "orch", "broker-rotate-secret", "--broker-secret", str(self.path), *extra],
                                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, expect, result.stderr[-400:])
            return json.loads(result.stdout) if result.returncode == 0 else result

        self.assertEqual(cli()["stage"], "overlap")
        cli(expect=2)
        self.assertEqual(len(read_secrets(self.path)[0]), 2)
        self.assertEqual(cli("--complete")["stage"], "completed")
        cli("--complete", expect=2)
        self.assertEqual(len(read_secrets(self.path)[0]), 1)
        missing = subprocess.run([sys.executable, "-m", "orch", "broker-rotate-secret"], cwd=ROOT, env=env,
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual(missing.returncode, 2)


class RotatingServiceTests(unittest.TestCase):
    """Rotation while the service runs: no restart, no dropped operation, no forged reply."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.harness = service_tests.Harness(self.root / "workspaces")
        self.addCleanup(self.harness.close)
        self.secret = Path(self.harness.config["secret"])

    def identity(self, config=None):
        return assess_identity(config or self.harness.config)

    def test_overlap_accepts_both_secrets_and_completion_refuses_the_previous(self):
        previous = load_secret(self.secret)
        stale_client = ServiceClient(self.harness.config)
        self.assertEqual(stale_client.secret.current, previous)
        rotate_secret(self.secret)
        current = load_secret(self.secret)
        # The service re-reads the file without restarting, and both secrets work.
        self.assertTrue(self.identity()["secret"]["rotating"])
        self.assertEqual(self.identity()["status"], "shared")  # Identity separation is reported before secret state.
        for key in (previous, current):
            with self.subTest(key=key.hex()[:8]):
                body = canonical({"schema_version": VERSION, "kind": "identity", "nonce": uid()})
                status, raw = self.harness.raw("identity", body, tag=sign(key, "request", "identity", body))
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(raw)["kind"], "identity")
        rotate_secret(self.secret, complete=True)
        body = canonical({"schema_version": VERSION, "kind": "identity", "nonce": uid()})
        self.assertEqual(self.harness.raw("identity", body, tag=sign(previous, "request", "identity", body))[0], 401)
        self.assertEqual(self.harness.raw("identity", body, tag=sign(current, "request", "identity", body))[0], 200)
        self.assertFalse(self.identity()["secret"]["rotating"])  # Settled again.

    def test_a_client_holding_the_previous_secret_verifies_its_own_reply(self):
        client = ServiceClient(self.harness.config)
        previous = client.secret.current
        rotate_secret(self.secret)
        self.assertNotEqual(load_secret(self.secret), previous)
        # Pin only this client to the previous secret, as a process that has not re-read the file.
        with patch.object(client.secret, "refresh", lambda: (previous,)):
            reply = client.call("identity", {"schema_version": VERSION, "kind": "identity", "nonce": uid()})
        self.assertEqual(reply["identity"], self.harness.service.identity)
        self.assertEqual(reply["secret"]["accepted"], 2)

    def test_rotation_completed_during_a_call_still_returns_a_verifiable_reply(self):
        client = ServiceClient(self.harness.config)
        original = self.harness.service.handle

        def rotate_then_handle(route, body):
            rotate_secret(self.secret)
            rotate_secret(self.secret, complete=True)  # The signing secret is withdrawn mid-call.
            return original(route, body)

        with patch.object(self.harness.service, "handle", rotate_then_handle):
            reply = client.call("identity", {"schema_version": VERSION, "kind": "identity", "nonce": uid()})
        self.assertEqual(reply["secret"]["accepted"], 1)
        # The next call uses the secret the operator left in place.
        self.assertEqual(client.call("identity", {"schema_version": VERSION, "kind": "identity", "nonce": uid()})["kind"], "identity")

    def test_a_withdrawn_secret_file_refuses_every_route_without_executing(self):
        client = ServiceClient(self.harness.config)
        body = canonical({"schema_version": VERSION, "kind": "identity", "nonce": uid()})
        tag = sign(client.secret.current, "request", "identity", body)
        self.secret.unlink()
        with patch.object(self.harness.service.executor, "run_direct", side_effect=AssertionError("No execution")):
            self.assertEqual(self.harness.raw("identity", body, tag=tag)[0], 503)
            with self.assertRaises(Rejected):
                client.call("identity", {"schema_version": VERSION, "kind": "identity", "nonce": uid()})
        write(self.secret, tag[:0] or secrets.token_hex(32))
        self.assertEqual(self.harness.raw("identity", body, tag=tag)[0], 401)  # The old tag is not accepted back.

    def test_identity_and_deployment_gate_report_rotation_and_age(self):
        report = self.identity()
        self.assertEqual(report["secret"], {"accepted": 1, "rotating": False, "age_seconds": report["secret"]["age_seconds"], "stale": False})
        self.assertEqual(report["secret_max_age_seconds"], SECRET_MAX_AGE_SECONDS)
        self.assertFalse(report["live_authorized"])
        base = self.harness.service.identity
        other = {**base, "account_sha256": "f" * 64, "uid": None if base["uid"] is None else base["uid"] + 1}
        with patch.object(self.harness.service, "identity", other):
            self.assertEqual(self.identity()["status"], "separate")
            stamp = time.time() - SECRET_MAX_AGE_SECONDS - 60
            os.utime(self.secret, (stamp, stamp))
            stale = self.identity()
            self.assertTrue(stale["secret"]["stale"])
            self.assertEqual(stale["status"], "secret_attention")
            assessment = deployment_check("github_copilot_cli", broker=self.harness.config)
            self.assertEqual(assessment["broker_identity"]["status"], "secret_attention")
            self.assertIn("mid-rotation", assessment["broker_identity"]["reason"])
            self.assertIn("broker_identity", assessment["blocking_gates"])
            self.assertFalse(assessment["provider_authorized"])
            os.utime(self.secret, None)
            self.assertEqual(deployment_check("github_copilot_cli", broker=self.harness.config)["broker_identity"]["status"], "separate")

    def test_cli_check_reports_the_secret_state(self):
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        result = subprocess.run([sys.executable, "-m", "orch", "broker-check", "--broker-endpoint", self.harness.config["endpoint"],
                                 "--broker-secret", self.harness.config["secret"]], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 2)  # Shared identity on this host.
        report = json.loads(result.stdout)
        self.assertEqual(report["secret"]["accepted"], 1)
        self.assertNotIn(load_secret(self.secret).hex(), result.stdout)


if __name__ == "__main__":
    unittest.main()
