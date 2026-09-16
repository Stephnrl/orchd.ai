"""The issue a confirmed specification implies — derived, never written.

The point of confirming a specification by hash is that a person read exactly those words.
If an agent then wrote the issue text, the confirmation would relate to nothing that reached
the repository. So the issue is a function of what was signed, and these tests fix that it
is one: same specification, same bytes, including the operation identifier — which is what
lets an interrupted dispatch resume instead of filing a second issue.
"""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import test_workflow as helpers
from orch.__main__ import main
from orch import github_issues as codec, task_drafts, task_issue
from orch.contracts import Rejected, canonical

PROFILE = {"schema_version": "1.0.0", "codec": "github-rest-2026-03-10",
           "repository": "a/b", "repository_id": 1}
SPEC = {"title": "Add a rate limit to the public API",
        "request": "Requests from one client should be limited.",
        "acceptance_criteria": ["A client over the limit receives 429",
                                "The limit is configurable without a redeploy"],
        "constraints": ["No new runtime dependency"],
        "allowed_paths": ["api/limits.py"]}


class DerivedIssueTests(unittest.TestCase):
    def setUp(self):
        helpers.WorkflowTests.setUp(self)

    def tearDown(self):
        helpers.WorkflowTests.tearDown(self)

    def confirmed(self, **changes):
        task = self.engine.create_task("Rate limiting", draft=True)
        task_drafts.draft(self.engine, task, {**SPEC, **changes})
        sha = task_drafts.show(self.engine, task)["spec_sha256"]
        task_drafts.confirm(self.engine, task, sha, "steph")
        return task

    # It is derived, not written

    def test_the_issue_says_what_the_specification_says(self):
        task = self.confirmed()
        derived = task_issue.derive(self.engine, task, PROFILE)["intent"]
        self.assertEqual(derived["title"], SPEC["title"])
        self.assertIn(SPEC["request"], derived["body"])
        for criterion in SPEC["acceptance_criteria"]:
            self.assertIn("- " + criterion, derived["body"])
        self.assertIn("- " + SPEC["constraints"][0], derived["body"])
        self.assertEqual(derived["repository"], PROFILE["repository"])
        self.assertEqual(derived["task_id"], task)

    def test_the_same_specification_always_derives_the_same_bytes(self):
        task = self.confirmed()
        first = task_issue.derive(self.engine, task, PROFILE)["intent"]
        second = task_issue.derive(self.engine, task, PROFILE)["intent"]
        self.assertEqual(canonical(first), canonical(second))

    def test_a_different_specification_derives_a_different_operation(self):
        # The operation identifier follows what was confirmed, so two different confirmed
        # specifications can never share the marker that makes creation idempotent.
        one = task_issue.derive(self.engine, self.confirmed(), PROFILE)["intent"]
        two = task_issue.derive(self.engine, self.confirmed(title="Something else"), PROFILE)["intent"]
        self.assertNotEqual(one["operation_id"], two["operation_id"])

    def test_deriving_twice_resumes_the_same_operation(self):
        # This is what lets an interrupted dispatch be reconciled rather than repeated: the
        # marker the first attempt would have written is the one the second searches for.
        task = self.confirmed()
        first = task_issue.derive(self.engine, task, PROFILE)["intent"]
        second = task_issue.derive(self.engine, task, PROFILE)["intent"]
        self.assertEqual(first["operation_id"], second["operation_id"])
        self.assertEqual(codec.MARKER % first["operation_id"], codec.MARKER % second["operation_id"])

    def test_an_empty_section_is_left_out_rather_than_rendered_empty(self):
        task = self.confirmed(constraints=[])
        body = task_issue.derive(self.engine, task, PROFILE)["intent"]["body"]
        self.assertIn("## Acceptance criteria", body)
        self.assertNotIn("## Constraints", body)

    # Only a confirmed specification implies anything

    def test_an_unconfirmed_draft_implies_no_issue(self):
        task = self.engine.create_task("Rate limiting", draft=True)
        task_drafts.draft(self.engine, task, SPEC)
        with self.assertRaisesRegex(Rejected, "confirm it first"):
            task_issue.derive(self.engine, task, PROFILE)

    def test_a_task_with_no_specification_implies_no_issue(self):
        task = self.engine.create_task("Rate limiting", draft=True)
        with self.assertRaisesRegex(Rejected, "no specification"):
            task_issue.derive(self.engine, task, PROFILE)

    def test_the_derivation_names_who_confirmed_it(self):
        task = self.confirmed()
        report = task_issue.derive(self.engine, task, PROFILE)
        self.assertEqual(report["confirmed_by"], "steph")
        self.assertEqual(report["spec_sha256"], self.engine.store.task(task)[0]["spec"]["sha256"])
        self.assertIs(report["live_authorized"], False)

    # It must still be a valid intent

    def test_the_derived_intent_satisfies_the_issue_contract(self):
        task = self.confirmed()
        derived = task_issue.derive(self.engine, task, PROFILE)["intent"]
        self.assertEqual(codec.validate_intent(derived, PROFILE), derived)

    def test_a_specification_too_large_to_render_is_refused_rather_than_trimmed(self):
        # Trimming would mean the issue no longer says what was confirmed.
        task = self.confirmed(
            acceptance_criteria=[("%02d " % n) + "x" * 496 for n in range(20)],
            constraints=[("%02d " % n) + "y" * 496 for n in range(20)])
        with self.assertRaisesRegex(Rejected, "byte budget"):
            task_issue.derive(self.engine, task, PROFILE)

    def test_a_destination_the_profile_does_not_name_cannot_be_derived_for(self):
        task = self.confirmed()
        with self.assertRaises(Rejected):
            task_issue.derive(self.engine, task, {**PROFILE, "repository": "not-a-repo"})

    # Through the command line

    def test_the_command_line_prints_an_intent_ready_to_stage(self):
        task = self.confirmed()
        profile = Path(self.temp.name) / "profile.json"
        profile.write_text(json.dumps(PROFILE), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["orch", "task-issue-intent", "--task", task,
                                        "--github-profile", str(profile),
                                        "--data", self.temp.name, "--trusted-fixture"]), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                main()
                code = 0
            except SystemExit as exit:
                code = exit.code
        self.assertEqual(code, 0, err.getvalue())
        printed = json.loads(out.getvalue())
        self.assertEqual(printed, task_issue.derive(self.engine, task, PROFILE)["intent"])
        # And what it printed is directly what the next step consumes.
        self.assertEqual(codec.read_plan(printed, PROFILE)["kind"], "GitHubIssueReadPlan")


if __name__ == "__main__":
    unittest.main()
