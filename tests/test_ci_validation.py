import unittest
from types import SimpleNamespace

from scripts.ci_tests import accepted


class CIValidationTests(unittest.TestCase):
    def result(self, ids, success=True, count=10):
        return SimpleNamespace(skipped=[(SimpleNamespace(id=lambda value=value: value), "reason") for value in ids],
                               testsRun=count, wasSuccessful=lambda: success)

    def test_only_exact_expected_skips_are_accepted(self):
        self.assertTrue(accepted(self.result(["docker-b", "docker-a"]), ["docker-a", "docker-b"]))
        for ids in ([], ["docker-a"], ["docker-a", "docker-b", "offline"], ["docker-a", "docker-a"]):
            self.assertFalse(accepted(self.result(ids), ["docker-a", "docker-b"]))

    def test_failed_or_empty_run_never_passes(self):
        self.assertFalse(accepted(self.result(["docker"], success=False), ["docker"]))
        self.assertFalse(accepted(self.result(["docker"], count=1), ["docker"]))
        self.assertFalse(accepted(self.result([], count=0), []))
