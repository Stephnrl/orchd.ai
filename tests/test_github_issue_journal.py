"""An issue intent held the way a pull request is: once, fenced, approved, reserved once.

The dispatcher checks that a request matches an approved hash, but until now nothing recorded
*which* hash a human approved. These tests fix the lifecycle that closes it — staged in the
journal, previewed for approval against evidence that suits what is being approved, reserved
exactly once, and reconciled afterwards by the operation marker the issue carries.
"""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from orch.__main__ import main

from orch import github_issues as codec
from orch.contracts import Rejected, canonical, digest
from orch.github_approval import check_approval, prepare_approval
from orch.github_journal import GitHubJournal, issue_family, scope_intent

PROFILE = {"schema_version": "1.0.0", "codec": "github-rest-2026-03-10",
           "repository": "a/b", "repository_id": 1}
INTENT = {"schema_version": "1.0.0", "task_id": "a" * 32, "operation_id": "b" * 32,
          "repository": "a/b", "title": "Add rate limiting",
          "body": "Requests from one client should be limited.", "labels": []}
REPOSITORY = {"id": 1, "full_name": "a/b", "archived": False, "disabled": False, "has_issues": True}
EVIDENCE = {"task_id": INTENT["task_id"], "spec_sha256": "c" * 64,
            "duplicates_sha256": "d" * 64, "policy_sha256": "e" * 64}
PULL_EVIDENCE = {"task_id": INTENT["task_id"], "patch_sha256": "c" * 64, "test_sha256": "d" * 64,
                 "review_sha256": "e" * 64, "policy_sha256": "f" * 64}


def capture_for(plan, existing=None):
    given = {"repository": REPOSITORY, "labels": [], "existing": existing or {"total_count": 0, "items": []}}
    return {"schema_version": "1.0.0", "plan_sha256": plan["sha256"],
            "responses": {key: {"url": read["url"], "redirected": False, "status": 200, "body": given[key]}
                          for key, read in plan["binding"]["requests"].items()}}


class IssueJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = GitHubJournal(Path(self.temp.name) / "github.sqlite")
        self.addCleanup(self.journal.close)
        self.plan = codec.read_plan(INTENT, PROFILE)
        self.capture = capture_for(self.plan)
        self.preview = codec.preview_issue(INTENT, PROFILE, self.capture)

    def stage(self, intent=None, capture=None, sha=None):
        intent = intent or INTENT
        capture = capture if capture is not None else self.capture
        expected = sha or codec.preview_issue(intent, PROFILE, capture)["sha256"]
        return self.journal.stage_issue(intent, PROFILE, capture, expected)

    # Holding it

    def test_an_issue_intent_is_staged_and_reads_back_as_itself(self):
        staged = self.stage()
        self.assertEqual(staged["state"], "prepared")
        self.assertEqual(staged["task_id"], INTENT["task_id"])
        record = self.journal.get(INTENT["operation_id"])
        self.assertEqual(record["sha256"], staged["sha256"])
        self.assertTrue(issue_family(record["scope"]))
        self.assertEqual(scope_intent(record["scope"]), INTENT)

    def test_the_stored_scope_must_still_produce_the_preview_it_claims(self):
        self.stage()
        row = self.journal.db.execute("SELECT scope FROM intents WHERE operation_id=?",
                                      (INTENT["operation_id"],)).fetchone()
        scope = json.loads(row[0])
        scope["preview"]["binding"]["request"]["body"]["title"] = "Something else entirely"
        # The trigger forbids editing a scope, so this writes a fresh journal to prove the
        # read-side check rather than the write-side one.
        other = GitHubJournal(Path(self.temp.name) / "tampered.sqlite")
        self.addCleanup(other.close)
        other.db.execute("INSERT INTO intents VALUES(?,?,?,?,'prepared',NULL)",
                         (INTENT["operation_id"], INTENT["task_id"],
                          canonical(scope).decode(), digest(scope)))
        with self.assertRaises(Rejected):
            other.get(INTENT["operation_id"])

    def test_a_preview_digest_that_does_not_match_is_refused(self):
        with self.assertRaisesRegex(Rejected, "Expected GitHub issue preview required"):
            self.stage(sha="0" * 64)

    def test_one_live_operation_per_task_holds_for_issues_too(self):
        self.stage()
        # A second operation on the same task, with its own plan and capture: the marker it
        # searches for is its own, so it needs its own reads.
        other = {**INTENT, "operation_id": "c" * 32}
        plan = codec.read_plan(other, PROFILE)
        with self.assertRaisesRegex(Rejected, "already bound to another scope"):
            self.stage(other, capture_for(plan))

    def test_staging_the_same_scope_twice_is_the_same_record(self):
        first = self.stage()
        self.assertEqual(self.stage()["sha256"], first["sha256"])

    def test_a_pull_request_and_an_issue_can_share_one_journal(self):
        self.stage()
        pull = {"schema_version": "1.0.0", "task_id": "f" * 32, "operation_id": "e" * 32,
                "repository": "a/b", "base": "main",
                "head": "orchd/" + "f" * 32 + "/" + "e" * 32,
                "expected_base_sha": "1" * 40, "expected_head_sha": "2" * 40,
                "title": "Implement it", "body": "text"}
        staged = self.journal.stage(pull, "a/b", 1)
        self.assertEqual(staged["state"], "prepared")
        self.assertFalse(issue_family(self.journal.get(pull["operation_id"])["scope"]))
        self.assertTrue(issue_family(self.journal.get(INTENT["operation_id"])["scope"]))
        self.assertEqual(self.journal.audit()["status"], "valid")

    # Approving it

    def test_an_issue_is_approved_against_evidence_that_suits_an_issue(self):
        staged = self.stage()
        preview = prepare_approval(self.journal, INTENT["operation_id"], staged["sha256"], EVIDENCE)
        self.assertEqual(preview["kind"], "GitHubApprovalPreview")
        self.assertEqual(preview["binding"]["evidence"], EVIDENCE)
        assessment = check_approval(self.journal, preview, preview["sha256"], EVIDENCE)
        self.assertEqual(assessment["status"], "current")
        self.assertEqual(assessment["binding"]["blockers"], [])

    def test_a_pull_request_envelope_cannot_approve_an_issue(self):
        # An issue has no patch, no test and no review. Offering those is not evidence about
        # it, and the envelope is what says so.
        staged = self.stage()
        with self.assertRaisesRegex(Rejected, "evidence envelope"):
            prepare_approval(self.journal, INTENT["operation_id"], staged["sha256"], PULL_EVIDENCE)

    def test_an_issue_envelope_cannot_approve_a_pull_request(self):
        pull = {"schema_version": "1.0.0", "task_id": "f" * 32, "operation_id": "e" * 32,
                "repository": "a/b", "base": "main",
                "head": "orchd/" + "f" * 32 + "/" + "e" * 32,
                "expected_base_sha": "1" * 40, "expected_head_sha": "2" * 40,
                "title": "Implement it", "body": "text"}
        staged = self.journal.stage(pull, "a/b", 1)
        with self.assertRaisesRegex(Rejected, "evidence envelope"):
            prepare_approval(self.journal, pull["operation_id"], staged["sha256"],
                             {**EVIDENCE, "task_id": pull["task_id"]})

    def test_changed_evidence_blocks_the_approval_rather_than_passing_it(self):
        staged = self.stage()
        preview = prepare_approval(self.journal, INTENT["operation_id"], staged["sha256"], EVIDENCE)
        assessment = check_approval(self.journal, preview, preview["sha256"],
                                    {**EVIDENCE, "spec_sha256": "9" * 64})
        self.assertIn("evidence_mismatch", assessment["binding"]["blockers"])
        self.assertEqual(assessment["status"], "blocked")

    # Reserving it, which is what binds the approval to the dispatch

    def test_the_approved_scope_carries_the_request_the_dispatcher_will_send(self):
        staged = self.stage()
        preview = prepare_approval(self.journal, INTENT["operation_id"], staged["sha256"], EVIDENCE)
        request = preview["binding"]["scope"]["preview"]["binding"]["request"]
        # This is the link that was missing: the hash the dispatcher is given is the hash of
        # the request inside the scope a human approved, and it is reachable from the
        # approval rather than asserted beside it.
        self.assertEqual(request, self.preview["binding"]["request"])
        self.assertEqual(digest(request), digest(self.preview["binding"]["request"]))

    def test_a_reservation_is_consumed_exactly_once(self):
        staged = self.stage()
        reserved = self.journal.reserve(INTENT["operation_id"], staged["sha256"])
        self.assertEqual(reserved["state"], "uncertain")
        with self.assertRaisesRegex(Rejected, "reservation already consumed"):
            self.journal.reserve(INTENT["operation_id"], staged["sha256"])

    def test_an_approval_is_refused_once_the_reservation_is_consumed(self):
        staged = self.stage()
        preview = prepare_approval(self.journal, INTENT["operation_id"], staged["sha256"], EVIDENCE)
        self.journal.reserve(INTENT["operation_id"], staged["sha256"])
        assessment = check_approval(self.journal, preview, preview["sha256"], EVIDENCE)
        self.assertIn("journal_not_prepared", assessment["binding"]["blockers"])

    # Reconciling it

    def test_reconciliation_finds_the_issue_by_the_marker_it_carries(self):
        staged = self.stage()
        self.journal.reserve(INTENT["operation_id"], staged["sha256"])
        marker = codec.MARKER % INTENT["operation_id"]
        observed = capture_for(self.plan, existing={
            "total_count": 1,
            "items": [{"number": 42, "title": "Add rate limiting", "body": "text\n\n" + marker,
                       "state": "open", "html_url": "https://github.com/a/b/issues/42"}]})
        report = self.journal.reconcile(INTENT["operation_id"], staged["sha256"], observed)
        self.assertEqual(report["status"], "candidate_observed")
        self.assertEqual(report["binding"]["assessment"]["binding"]["candidate"]["number"], 42)

    def test_reconciliation_without_the_marker_stays_unresolved(self):
        staged = self.stage()
        self.journal.reserve(INTENT["operation_id"], staged["sha256"])
        report = self.journal.reconcile(INTENT["operation_id"], staged["sha256"], capture_for(self.plan))
        self.assertEqual(report["status"], "unresolved")

    def test_reconciliation_needs_a_consumed_reservation(self):
        staged = self.stage()
        with self.assertRaisesRegex(Rejected, "uncertain reservation"):
            self.journal.reconcile(INTENT["operation_id"], staged["sha256"], capture_for(self.plan))


class IssueStagingCommandTests(unittest.TestCase):
    """The same lifecycle as an operator reaches it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plan = codec.read_plan(INTENT, PROFILE)
        self.capture = capture_for(self.plan)
        self.preview = codec.preview_issue(INTENT, PROFILE, self.capture)
        self.files = {name: self.write(name + ".json", value) for name, value in
                      (("intent", INTENT), ("profile", PROFILE), ("capture", self.capture))}

    def write(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    def cli(self, *argv, expect=0):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["orch", *argv]), redirect_stdout(out), redirect_stderr(err):
            try:
                code = 0
                main()
            except SystemExit as exit:
                code = exit.code
        self.assertEqual(code, expect, err.getvalue())
        return json.loads(out.getvalue()) if code == 0 else err.getvalue()

    def stage_argv(self, sha=None):
        return ("github-issue-stage", "--journal", str(self.root / "github.sqlite"),
                "--intent", self.files["intent"], "--github-profile", self.files["profile"],
                "--transcript", self.files["capture"],
                "--expected-sha256", sha or self.preview["sha256"])

    def test_staging_from_the_command_line_holds_the_intent(self):
        staged = self.cli(*self.stage_argv())
        self.assertEqual(staged["state"], "prepared")
        self.assertEqual(staged["task_id"], INTENT["task_id"])
        self.assertIs(staged["live_authorized"], False)

    def test_the_command_line_refuses_a_digest_that_is_not_the_preview(self):
        refusal = self.cli(*self.stage_argv(sha="0" * 64), expect=2)
        self.assertIn("Cannot stage GitHub issue intent", refusal)
        self.assertFalse((self.root / "github.sqlite").exists(),
                         "a refused staging leaves no journal behind")

    def test_the_command_line_insists_on_every_part(self):
        self.assertIn("--intent", self.cli("github-issue-stage", expect=2))


if __name__ == "__main__":
    unittest.main()
