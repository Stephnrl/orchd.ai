"""Machine-readable execution of the mandatory Docker gates, isolated from the API."""
import io
import json
from pathlib import Path
import unittest


class Results(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.passed_ids = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed_ids.append(test.id())


def main():
    from .readiness import EXPECTED
    suite = unittest.defaultTestLoader.discover(str(Path(__file__).resolve().parents[1] / "tests"), pattern="test_docker_integration.py")
    result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=Results, verbosity=0).run(suite)
    report = {"schema_version": "1.0.0", "expected": EXPECTED, "passed": sorted(result.passed_ids),
              "failed": sorted(test.id() for test, _ in result.failures), "errors": sorted(test.id() for test, _ in result.errors),
              "skipped": sorted(test.id() for test, _ in result.skipped), "tests_run": result.testsRun}
    print(json.dumps(report))
    return 0 if result.wasSuccessful() and not result.skipped and report["passed"] == EXPECTED else 2


if __name__ == "__main__":
    raise SystemExit(main())
