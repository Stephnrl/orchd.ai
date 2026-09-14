"""Run offline tests, accepting only the five explicitly separated Docker skips."""
import os
import argparse
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def accepted(result, expected_skips):
    skipped = [test.id() for test, _ in result.skipped]
    return (result.wasSuccessful() and not result.expectedFailures and result.testsRun > len(skipped)
            and sorted(skipped) == sorted(expected_skips))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--result', help='New machine-readable result file; never includes test output')
    args = parser.parse_args()
    # This lane never runs Docker, even if a developer has opted in elsewhere.
    os.environ.pop("ORCH_DOCKER_TEST_IMAGE", None)
    from orch.readiness import EXPECTED
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    passed = accepted(result, EXPECTED)
    if args.result:
        with Path(args.result).open('x', encoding='utf-8') as stream:
            json.dump({'status': 'passed' if passed else 'failed', 'tests_run': result.testsRun,
                       'skipped': sorted(test.id() for test, _ in result.skipped)}, stream, sort_keys=True)
    if not passed:
        print("FAIL: offline suite failed or its skip set differs from the required Docker gates", file=sys.stderr)
        return 1
    print("PASS: offline suite; exactly the five Docker gates are deferred to the Docker CI job")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
