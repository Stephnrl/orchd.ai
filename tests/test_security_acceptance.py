"""The criterion-to-test mapping must stay true to the document, the suite and its own limits."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from orch.contracts import Rejected
from orch.security_acceptance import CRITERIA, DOCKER_GATES, assess, check_mapping, documented_ids, discovered_ids

ROOT = Path(__file__).resolve().parents[1]


def outcomes(names, verdict="passed", **overrides):
    result = {name: verdict for name in names}
    result.update(overrides)
    return result


class MappingTests(unittest.TestCase):
    def test_every_documented_criterion_is_mapped_to_tests_that_exist(self):
        available = check_mapping()
        self.assertEqual(tuple(c["id"] for c in CRITERIA), documented_ids())
        self.assertEqual(len(CRITERIA), 10)
        for criterion in CRITERIA:
            with self.subTest(criterion=criterion["id"]):
                self.assertTrue(criterion["offline"], "every criterion needs local evidence")
                self.assertTrue(criterion["deferred"], "every criterion states what it does not establish")
                self.assertTrue(set(criterion["offline"]) <= available)
                self.assertNotIn(criterion["id"], "".join(criterion["offline"]))

    def test_a_renamed_or_removed_test_is_detected(self):
        broken = copy.deepcopy(list(CRITERIA))
        broken[0]["offline"] = broken[0]["offline"] + ("test_workflow.WorkflowTests.test_renamed_away",)
        with patch("orch.security_acceptance.CRITERIA", tuple(broken)):
            with self.assertRaisesRegex(Rejected, "no longer exist"):
                check_mapping()

    def test_a_criterion_added_to_the_document_but_not_mapped_is_detected(self):
        with patch("orch.security_acceptance.documented_ids", return_value=documented_ids() + ("SEC-11",)):
            with self.assertRaisesRegex(Rejected, "differ from the ones"):
                check_mapping()
        with patch("orch.security_acceptance.documented_ids", return_value=documented_ids()[::-1]):
            with self.assertRaises(Rejected):
                check_mapping()

    def test_empty_duplicate_and_foreign_docker_mappings_are_refused(self):
        for mutate, message in (
                (lambda c: c.update(offline=(), docker=()), "names no test"),
                (lambda c: c.update(offline=c["offline"] + (c["offline"][0],)), "names a test twice"),
                (lambda c: c.update(docker=("test_docker_integration.DockerIsolationTests.test_invented_gate",)), "no longer exist"),
                (lambda c: c.update(docker=("test_workflow.WorkflowTests.test_state_matrix",)), "not the required five")):
            broken = copy.deepcopy(list(CRITERIA))
            mutate(broken[2])
            with self.subTest(message=message), patch("orch.security_acceptance.CRITERIA", tuple(broken)):
                with self.assertRaisesRegex(Rejected, message):
                    check_mapping()

    def test_a_document_without_criteria_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            empty = Path(directory) / "security.md"
            empty.write_text("# no criteria here\n", encoding="utf-8")
            with patch("orch.security_acceptance.DOC", empty):
                with self.assertRaisesRegex(Rejected, "lists no criteria"):
                    documented_ids()
            with patch("orch.security_acceptance.DOC", Path(directory) / "missing.md"):
                with self.assertRaisesRegex(Rejected, "unreadable"):
                    documented_ids()

    def test_the_named_docker_gates_are_exactly_the_required_five(self):
        named = {name for criterion in CRITERIA for name in criterion["docker"]}
        self.assertEqual(named, set(DOCKER_GATES))
        self.assertEqual(len(DOCKER_GATES), 5)
        self.assertTrue(set(DOCKER_GATES) <= discovered_ids())


class ReportTests(unittest.TestCase):
    """The report is exercised with synthetic outcomes; the suite itself is not rerun here."""

    def selected(self, include_docker=False):
        names = set()
        for criterion in CRITERIA:
            names.update(criterion["offline"])
            if include_docker:
                names.update(criterion["docker"])
        return names

    def test_a_clean_run_is_covered_and_claims_no_authority(self):
        names = self.selected()
        with patch("orch.security_acceptance._run", return_value=outcomes(names)) as run:
            report = assess()
        self.assertEqual(sorted(run.call_args.args[0]), sorted(names))
        self.assertEqual(report["status"], "covered")
        self.assertEqual(report["unmet_tests"], [])
        self.assertFalse(report["independent_review"])
        self.assertFalse(report["live_authorized"])
        self.assertFalse(report["docker_gates_included"])
        self.assertEqual([c["id"] for c in report["criteria"]], list(documented_ids()))
        self.assertIn("not an independent security review", report["meaning"])
        for criterion in report["criteria"]:
            self.assertEqual(criterion["status"], "covered")
            self.assertEqual(criterion["unmet_tests"], [])

    def test_a_failing_test_fails_only_its_own_criterion_and_the_report(self):
        names = self.selected()
        broken = sorted(CRITERIA[3]["offline"])[0]
        with patch("orch.security_acceptance._run", return_value=outcomes(names, **{broken: "failed"})):
            report = assess()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["unmet_tests"], [broken])
        failed = [c for c in report["criteria"] if c["status"] == "failed"]
        self.assertEqual([c["id"] for c in failed], [CRITERIA[3]["id"]])
        self.assertEqual(failed[0]["unmet_tests"], [broken])

    def test_an_unexpected_skip_is_not_treated_as_coverage(self):
        names = self.selected()
        skipped = sorted(CRITERIA[0]["offline"])[0]
        with patch("orch.security_acceptance._run", return_value=outcomes(names, **{skipped: "skipped"})):
            report = assess()
        self.assertEqual(report["status"], "failed")
        self.assertIn(skipped, report["unmet_tests"])

    def test_absent_docker_gates_are_reported_as_deferred_not_covered(self):
        with patch("orch.security_acceptance._run", return_value=outcomes(self.selected())):
            offline = assess()
        gated = [c for c in offline["criteria"] if c["docker_tests"]]
        self.assertEqual(len(gated), 2)
        for criterion in gated:
            self.assertEqual(set(criterion["docker_tests"].values()), {"not_run"})
            self.assertIn("required Docker gates were not run", criterion["deferred"][0])
        with patch("orch.security_acceptance._run", return_value=outcomes(self.selected(include_docker=True))):
            full = assess(include_docker=True)
        self.assertTrue(full["docker_gates_included"])
        for criterion in full["criteria"]:
            self.assertNotIn("required Docker gates were not run", " ".join(criterion["deferred"]))
            self.assertTrue(set(criterion["docker_tests"].values()) <= {"passed"})

    def test_every_criterion_keeps_its_stated_limits(self):
        with patch("orch.security_acceptance._run", return_value=outcomes(self.selected())):
            report = assess()
        for criterion in report["criteria"]:
            with self.subTest(criterion=criterion["id"]):
                self.assertTrue(criterion["deferred"])
                self.assertTrue(criterion["summary"])


class ScriptTests(unittest.TestCase):
    def run_script(self, *args):
        return subprocess.run([sys.executable, "scripts/security_acceptance.py", *args], cwd=ROOT,
                              capture_output=True, text=True, timeout=300)

    def test_mapping_only_is_fast_and_reports_the_inventory(self):
        result = self.run_script("--mapping-only")
        self.assertEqual(result.returncode, 0, result.stderr[-500:])
        payload = json.loads(result.stdout.splitlines()[0])
        self.assertEqual(payload["criteria"], list(documented_ids()))
        self.assertGreater(payload["tests"], 50)
        self.assertIn("PASS", result.stdout)

    def test_an_existing_destination_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory) / "report.json"
            existing.write_text("retained", encoding="utf-8")
            result = self.run_script("--mapping-only", "--destination", str(existing))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(existing.read_text(encoding="utf-8"), "retained")


if __name__ == "__main__":
    unittest.main()
