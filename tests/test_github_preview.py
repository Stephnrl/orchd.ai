import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator
from orch.contracts import Rejected, digest
from orch.github_preview import SCHEMA, load_intent, prepare_pull_request


def intent():
    return {"schema_version": "1.0.0", "task_id": "a" * 32, "operation_id": "b" * 32,
            "repository": "example/project", "base": "main", "head": "orchd/" + "a" * 32 + "/" + "b" * 32,
            "expected_base_sha": "1" * 40, "expected_head_sha": "2" * 40,
            "title": "Review greeting", "body": "Evidence summary\nSecond line"}


class GitHubPreviewTests(unittest.TestCase):
    def test_closed_schema_and_deterministic_bound_request(self):
        Draft202012Validator.check_schema(SCHEMA)
        source = intent()
        original = copy.deepcopy(source)
        preview = prepare_pull_request(source, source["repository"])
        self.assertEqual(source, original)
        self.assertEqual(preview, prepare_pull_request(dict(reversed(list(source.items()))), source["repository"]))
        self.assertEqual(preview["sha256"], digest(preview["binding"]))
        self.assertFalse(preview["live_authorized"])
        self.assertFalse(preview["remote_refs_verified"])
        request = preview["binding"]["request"]
        self.assertEqual(request["url"], "https://api.github.com/repos/example/project/pulls")
        self.assertEqual(request["method"], "POST")
        self.assertTrue(request["body"]["draft"])
        self.assertFalse(request["body"]["maintainer_can_modify"])
        self.assertNotIn("Authorization", request["headers"])
        for key, value in (("title", "Changed"), ("body", "Changed"), ("base", "release"), ("expected_head_sha", "3" * 40), ("expected_base_sha", "4" * 40), ("repository", "example/other")):
            changed = {**source, key: value}
            self.assertNotEqual(preview["sha256"], prepare_pull_request(changed, changed["repository"])["sha256"])
        for key in ("task_id", "operation_id"):
            changed = {**source, key: "c" * 32}
            changed["head"] = f"orchd/{changed['task_id']}/{changed['operation_id']}"
            self.assertNotEqual(preview["sha256"], prepare_pull_request(changed, changed["repository"])["sha256"])

    def test_destination_refs_identity_and_unknown_fields_rejected(self):
        source = intent()
        with self.assertRaises(Rejected):
            prepare_pull_request(source, "elsewhere/project")
        cases = [("repository", value) for value in ("https://evil.test/x", "example/project?x", "example/project\n", "example/project.git", "example/project.GIT", "example/../x")]
        cases += [("base", value) for value in ("HEAD", "refs/heads/main", "a..b", "a.lock/x", "a//b", "a/.x", "x@{1}", "x:y", "-main", "a/", "main\n")]
        cases += [("head", "attacker:branch"), ("head", "main"), ("task_id", "a" * 32 + "\n"), ("expected_head_sha", "2" * 40 + "\n"), ("expected_head_sha", "1" * 40), ("draft", False), ("token", "credential")]
        for key, value in cases:
            with self.subTest(key=key, value=value), self.assertRaises(Rejected):
                changed = {**source, key: value}
                prepare_pull_request(changed, changed["repository"])
        for key in source:
            changed = source.copy()
            del changed[key]
            with self.subTest(missing=key), self.assertRaises(Rejected):
                prepare_pull_request(changed, source["repository"])

    def test_text_budgets_controls_and_secrets_rejected(self):
        for key, value in (("title", "   "), ("title", "x\ny"), ("body", "x\x00y"), ("body", "x\ry"), ("body", "é" * 9000), ("body", "token=do-not-publish"), ("title", "ghp_testcredential"), ("title", "\ud800")):
            with self.subTest(key=key), self.assertRaises(Rejected):
                prepare_pull_request({**intent(), key: value}, "example/project")

    def test_bounded_strict_json_and_cli_without_effects(self):
        from orch.__main__ import main
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "intent.json"
            source.write_text(json.dumps(intent()), encoding="utf-8")
            output = io.StringIO()
            with patch.object(sys, "argv", ["orch", "github-preview", "--intent", str(source), "--allow-repository", "example/project"]), redirect_stdout(output), patch("orch.__main__.Engine", side_effect=AssertionError("No store")), patch("subprocess.Popen", side_effect=AssertionError("No subprocess")), patch("socket.socket", side_effect=AssertionError("No network")):
                main()
            self.assertFalse(json.loads(output.getvalue())["live_authorized"])
            self.assertEqual(list(Path(root).iterdir()), [source])
            for text in ('{"title":"one","title":"two"}', '{"n":NaN}', '[', 'x' * 65537):
                source.write_text(text)
                with self.assertRaises(Rejected):
                    load_intent(source)
