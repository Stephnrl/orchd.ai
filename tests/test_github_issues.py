"""The GitHub issue codec: request data, checked against what was observed, never dispatched.

A project manager's work is tracking work, and until now the only GitHub intent this control
plane had was a pull request. These tests fix the half that was missing — and fix that it is
a codec: every record it returns says `live_authorized: false`, and it says so because there
is no dispatch path at all.
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
from orch import github_issues as issues
from orch.contracts import Rejected, digest

PROFILE = {"schema_version": "1.0.0", "codec": "github-rest-2026-03-10",
           "repository": "Stephnrl/orchd.ai", "repository_id": 12345}
INTENT = {"schema_version": "1.0.0", "task_id": "a" * 32, "operation_id": "b" * 32,
          "repository": "Stephnrl/orchd.ai", "title": "Add rate limiting",
          "body": "Requests from one client should be limited.", "labels": ["enhancement"]}
REPOSITORY = {"id": 12345, "full_name": "Stephnrl/orchd.ai", "archived": False,
              "disabled": False, "has_issues": True}
LABELS = [{"name": "enhancement"}, {"name": "bug"}]
EMPTY = {"total_count": 0, "items": []}


def found(*rows):
    return {"total_count": len(rows), "items": list(rows)}


def issue(number=7, title="Add rate limiting", body="", state="open"):
    return {"number": number, "title": title, "body": body, "state": state,
            "html_url": "https://github.com/Stephnrl/orchd.ai/issues/" + str(number)}


class GitHubIssueTests(unittest.TestCase):
    def capture(self, plan, **bodies):
        given = {"repository": REPOSITORY, "labels": LABELS, "existing": EMPTY,
                 "search": EMPTY, **bodies}
        return {"schema_version": "1.0.0", "plan_sha256": plan["sha256"],
                "responses": {key: {"url": read["url"], "redirected": False, "status": 200,
                                    "body": given[key]}
                              for key, read in plan["binding"]["requests"].items()}}

    def preview(self, intent=None, **bodies):
        intent = intent or INTENT
        plan = issues.read_plan(intent, PROFILE)
        return issues.preview_issue(intent, PROFILE, self.capture(plan, **bodies))

    # The destination

    def test_a_profile_names_a_repository_by_id_as_well_as_name(self):
        self.assertEqual(issues.validate_profile(dict(PROFILE)), PROFILE)
        for broken, complaint in (({**PROFILE, "repository_id": "12345"}, "repository ID"),
                                  ({**PROFILE, "repository_id": 0}, "repository ID"),
                                  ({**PROFILE, "repository": "no-slash"}, "repository"),
                                  ({**PROFILE, "repository": "Stephnrl/orchd.git"}, "repository"),
                                  ({**PROFILE, "codec": "github-rest-2020-01-01"}, "codec profile"),
                                  ({key: value for key, value in PROFILE.items() if key != "codec"}, "profile shape")):
            with self.assertRaisesRegex(Rejected, complaint):
                issues.validate_profile(broken)

    def test_an_intent_may_not_name_a_repository_the_profile_does_not(self):
        with self.assertRaisesRegex(Rejected, "destination is not allowed"):
            issues.validate_intent({**INTENT, "repository": "someone/else"}, PROFILE)

    # The intent

    def test_an_intent_is_checked_against_its_contract_and_its_content(self):
        self.assertEqual(issues.validate_intent(dict(INTENT), PROFILE), INTENT)
        for broken, complaint in (({**INTENT, "title": "   "}, "needs a title"),
                                  ({**INTENT, "title": "bad\x07title"}, "control character"),
                                  ({**INTENT, "labels": ["a", "a"]}, "repeats a label"),
                                  ({**INTENT, "task_id": "nope"}, "Invalid GitHub issue intent"),
                                  ({**INTENT, "extra": 1}, "Invalid GitHub issue intent")):
            with self.assertRaisesRegex(Rejected, complaint):
                issues.validate_intent(broken, PROFILE)

    def test_an_intent_carrying_a_secret_is_refused(self):
        with self.assertRaisesRegex(Rejected, "secrets"):
            issues.validate_intent({**INTENT, "body": "use token: ghp_aaaaaaaaaaaaaaaaaaaa"}, PROFILE)

    def test_an_intent_may_not_forge_its_own_operation_marker(self):
        # The marker is how an already-created issue is recognised. An intent that writes one
        # itself could make a different operation's issue look like its own.
        forged = issues.MARKER % ("c" * 32)
        with self.assertRaisesRegex(Rejected, "own operation marker"):
            issues.validate_intent({**INTENT, "body": "text\n" + forged}, PROFILE)

    # The read plan

    def test_a_read_plan_asks_for_exactly_what_it_needs(self):
        plan = issues.read_plan(INTENT, PROFILE)
        self.assertEqual(sorted(plan["binding"]["requests"]), ["existing", "labels", "repository"])
        self.assertEqual(plan["binding"]["intent_sha256"], digest(INTENT))
        self.assertEqual(plan["binding"]["profile_sha256"], digest(PROFILE))
        for read in plan["binding"]["requests"].values():
            self.assertEqual(read["method"], "GET")
            self.assertTrue(read["url"].startswith("https://api.github.com/"))

    def test_every_record_says_it_authorizes_nothing(self):
        records = [issues.read_plan(INTENT, PROFILE), issues.duplicate_plan(PROFILE, "rate limiting"),
                   self.preview()]
        for record in records:
            self.assertIs(record["live_authorized"], False, record["kind"])
            self.assertIs(record["remote_effect_confirmed"], False, record["kind"])

    # The capture

    def test_a_capture_must_answer_the_plan_it_claims_to(self):
        plan = issues.read_plan(INTENT, PROFILE)
        good = self.capture(plan)
        self.assertEqual(sorted(issues.responses(plan, good)), ["existing", "labels", "repository"])
        stale = {**good, "plan_sha256": "0" * 64}
        with self.assertRaisesRegex(Rejected, "binding mismatch"):
            issues.responses(plan, stale)
        short = {**good, "responses": {key: value for key, value in good["responses"].items() if key != "labels"}}
        with self.assertRaisesRegex(Rejected, "different plan"):
            issues.responses(plan, short)

    def test_a_redirected_or_failed_read_is_not_an_observation(self):
        plan = issues.read_plan(INTENT, PROFILE)
        for change, complaint in (({"redirected": True}, "different origin"),
                                  ({"status": 404}, "unavailable"),
                                  ({"url": "https://api.github.com/repos/someone/else"}, "different origin")):
            capture = self.capture(plan)
            capture["responses"]["repository"].update(change)
            with self.assertRaisesRegex(Rejected, complaint):
                issues.responses(plan, capture)

    # What was observed

    def test_a_renamed_or_unsuitable_repository_stops_everything(self):
        for body, complaint in (({**REPOSITORY, "id": 999}, "identity mismatch"),
                                ({**REPOSITORY, "full_name": "someone/else"}, "identity mismatch"),
                                ({**REPOSITORY, "archived": True}, "archived or disabled"),
                                ({**REPOSITORY, "has_issues": False}, "issues disabled")):
            with self.assertRaisesRegex(Rejected, complaint):
                self.preview(repository=body)

    def test_a_label_the_repository_does_not_have_is_named_rather_than_created(self):
        # Creating an issue with an unknown label creates the label too, which is an effect
        # nobody reviewed.
        with self.assertRaisesRegex(Rejected, "no label enhancement"):
            self.preview(labels=[{"name": "bug"}])

    def test_a_truncated_search_is_refused_because_it_hides_what_exists(self):
        with self.assertRaisesRegex(Rejected, "incomplete"):
            self.preview(existing={"total_count": 9, "items": [issue()]})

    def test_a_pull_request_returned_by_an_issue_search_is_refused(self):
        with self.assertRaisesRegex(Rejected, "pull request"):
            self.preview(existing=found({**issue(), "pull_request": {"url": "x"}}))

    # The write it would make

    def test_a_preview_is_the_exact_request_and_nothing_is_sent(self):
        preview = self.preview()
        write = preview["binding"]["request"]
        self.assertEqual(write["method"], "POST")
        self.assertEqual(write["url"], "https://api.github.com/repos/Stephnrl/orchd.ai/issues")
        self.assertEqual(write["body"]["title"], INTENT["title"])
        self.assertEqual(write["body"]["labels"], ["enhancement"])
        self.assertEqual(preview["binding"]["repository_id"], PROFILE["repository_id"])

    def test_the_body_carries_the_operation_that_wrote_it(self):
        write = self.preview()["binding"]["request"]["body"]
        self.assertTrue(write["body"].startswith(INTENT["body"]))
        self.assertEqual(write["body"].splitlines()[-1], issues.MARKER % INTENT["operation_id"])

    def test_an_empty_body_is_still_marked_without_leading_blank_lines(self):
        write = self.preview({**INTENT, "body": ""})["binding"]["request"]["body"]
        self.assertEqual(write["body"], issues.MARKER % INTENT["operation_id"])

    def test_an_operation_that_already_created_its_issue_refuses_to_create_another(self):
        marked = issue(number=42, body="text\n\n" + (issues.MARKER % INTENT["operation_id"]))
        with self.assertRaisesRegex(Rejected, r"already created issue #42"):
            self.preview(existing=found(marked))

    # Answering "is this already an issue?"

    def test_a_duplicate_plan_asks_the_question_without_answering_it(self):
        plan = issues.duplicate_plan(PROFILE, "rate limiting in:title")
        self.assertEqual(sorted(plan["binding"]["requests"]), ["repository", "search"])
        self.assertIn("rate%20limiting", plan["binding"]["requests"]["search"]["url"])
        self.assertIn("is%3Aissue", plan["binding"]["requests"]["search"]["url"])

    def test_duplicate_search_terms_are_bounded_and_checked(self):
        for terms, complaint in ((" ", "1 to 200"), ("x" * 201, "1 to 200"),
                                 ("token: ghp_aaaaaaaaaaaaaaaaaaaa", "secret"),
                                 ("bad\x07terms", "control character")):
            with self.assertRaisesRegex(Rejected, complaint):
                issues.duplicate_plan(PROFILE, terms)

    def test_a_duplicate_report_lists_what_was_found_and_decides_nothing(self):
        plan = issues.duplicate_plan(PROFILE, "rate limiting")
        capture = self.capture(plan, search=found(issue(number=3, title="Rate limit the API")))
        report = issues.duplicates(plan, capture, PROFILE)
        self.assertEqual(report["found"], 1)
        self.assertEqual(report["binding"]["issues"], [
            {"number": 3, "title": "Rate limit the API", "state": "open",
             "html_url": "https://github.com/Stephnrl/orchd.ai/issues/3"}])
        self.assertIs(report["live_authorized"], False)

    def test_a_duplicate_report_finding_nothing_says_so(self):
        plan = issues.duplicate_plan(PROFILE, "rate limiting")
        report = issues.duplicates(plan, self.capture(plan), PROFILE)
        self.assertEqual(report["found"], 0)
        self.assertEqual(report["binding"]["issues"], [])

    def test_a_read_plan_is_not_a_duplicate_plan(self):
        plan = issues.read_plan(INTENT, PROFILE)
        with self.assertRaisesRegex(Rejected, "not a duplicate plan"):
            issues.duplicates(plan, self.capture(plan), PROFILE)

    # After an uncertain dispatch

    def test_reconciliation_finds_the_issue_this_operation_created(self):
        marked = issue(number=42, body="text\n\n" + (issues.MARKER % INTENT["operation_id"]))
        plan = issues.read_plan(INTENT, PROFILE)
        report = issues.reconcile_issue(INTENT, PROFILE, self.capture(plan, existing=found(marked)))
        self.assertEqual(report["status"], "candidate_observed")
        self.assertEqual(report["binding"]["candidate"]["number"], 42)
        self.assertEqual(report["binding"]["blockers"], [])

    def test_reconciliation_with_nothing_to_find_stays_unresolved(self):
        plan = issues.read_plan(INTENT, PROFILE)
        report = issues.reconcile_issue(INTENT, PROFILE, self.capture(plan))
        self.assertEqual(report["status"], "unresolved")
        self.assertEqual(report["binding"]["blockers"], ["no_candidate_observed"])
        self.assertIsNone(report["binding"]["candidate"])

    def test_two_issues_carrying_one_marker_are_a_blocker_not_a_choice(self):
        marker = issues.MARKER % INTENT["operation_id"]
        plan = issues.read_plan(INTENT, PROFILE)
        capture = self.capture(plan, existing=found(issue(number=1, body=marker), issue(number=2, body=marker)))
        report = issues.reconcile_issue(INTENT, PROFILE, capture)
        self.assertEqual(report["status"], "unresolved")
        self.assertIn("duplicate_operation_marker", report["binding"]["blockers"])

    def test_an_issue_without_the_marker_is_not_this_operation_s(self):
        plan = issues.read_plan(INTENT, PROFILE)
        capture = self.capture(plan, existing=found(issue(number=9, body="a similar issue")))
        self.assertEqual(issues.reconcile_issue(INTENT, PROFILE, capture)["status"], "unresolved")


class GitHubIssueCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = self.write("profile.json", PROFILE)
        self.intent = self.write("intent.json", INTENT)

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

    def capture_for(self, plan, **bodies):
        given = {"repository": REPOSITORY, "labels": LABELS, "existing": EMPTY, "search": EMPTY, **bodies}
        return {"schema_version": "1.0.0", "plan_sha256": plan["sha256"],
                "responses": {key: {"url": read["url"], "redirected": False, "status": 200,
                                    "body": given[key]}
                              for key, read in plan["binding"]["requests"].items()}}

    def test_the_read_plan_and_preview_work_from_the_command_line(self):
        plan = self.cli("github-issue-read-plan", "--github-profile", self.profile, "--intent", self.intent)
        self.assertEqual(plan["kind"], "GitHubIssueReadPlan")
        transcript = self.write("capture.json", self.capture_for(plan))
        preview = self.cli("github-issue-preview", "--github-profile", self.profile,
                           "--intent", self.intent, "--transcript", transcript)
        self.assertEqual(preview["binding"]["request"]["method"], "POST")
        self.assertIs(preview["live_authorized"], False)

    def test_the_duplicate_question_works_from_the_command_line(self):
        plan = self.cli("github-issue-duplicate-plan", "--github-profile", self.profile,
                        "--terms", "rate limiting")
        self.assertEqual(plan["kind"], "GitHubIssueDuplicatePlan")
        transcript = self.write("search.json", self.capture_for(plan, search=found(issue(number=3))))
        report = self.cli("github-issue-duplicates", "--github-profile", self.profile,
                          "--terms", "rate limiting", "--transcript", transcript)
        self.assertEqual(report["found"], 1)

    def test_reconciliation_works_from_the_command_line(self):
        plan = self.cli("github-issue-read-plan", "--github-profile", self.profile, "--intent", self.intent)
        marked = issue(number=42, body=issues.MARKER % INTENT["operation_id"])
        transcript = self.write("recon.json", self.capture_for(plan, existing=found(marked)))
        report = self.cli("github-issue-reconcile", "--github-profile", self.profile,
                          "--intent", self.intent, "--transcript", transcript)
        self.assertEqual(report["binding"]["candidate"]["number"], 42)

    def test_the_command_line_insists_on_what_each_step_needs(self):
        self.assertIn("--github-profile", self.cli("github-issue-read-plan", expect=2))
        self.assertIn("--terms", self.cli("github-issue-duplicate-plan", "--github-profile", self.profile, expect=2))
        self.assertIn("--intent", self.cli("github-issue-read-plan", "--github-profile", self.profile, expect=2))
        self.assertIn("--transcript", self.cli("github-issue-preview", "--github-profile", self.profile,
                                               "--intent", self.intent, expect=2))

    def test_a_mismatched_capture_is_refused_at_the_command_line(self):
        plan = self.cli("github-issue-read-plan", "--github-profile", self.profile, "--intent", self.intent)
        wrong = self.capture_for(plan)
        wrong["plan_sha256"] = "0" * 64
        refusal = self.cli("github-issue-preview", "--github-profile", self.profile, "--intent", self.intent,
                           "--transcript", self.write("wrong.json", wrong), expect=2)
        self.assertIn("GitHub issue step rejected", refusal)


if __name__ == "__main__":
    unittest.main()
