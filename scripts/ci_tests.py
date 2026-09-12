"""Run offline tests, accepting only the five explicitly separated Docker skips."""
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def accepted(result, expected_skips):
    skipped = [test.id() for test, _ in result.skipped]
    return (result.wasSuccessful() and result.testsRun > len(skipped)
            and sorted(skipped) == sorted(expected_skips))


def main():
    # This lane never runs Docker, even if a developer has opted in elsewhere.
    os.environ.pop("ORCH_DOCKER_TEST_IMAGE", None)
    from orch.readiness import EXPECTED
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not accepted(result, EXPECTED):
        print("FAIL: offline suite failed or its skip set differs from the required Docker gates", file=sys.stderr)
        return 1
    print("PASS: offline suite; exactly the five Docker gates are deferred to the Docker CI job")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
