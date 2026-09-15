"""Check the mandatory security criteria against the tests that exercise them.

This reports local evidence about this source on this host. It is not an independent
security review, a deployment authorization or a claim about any live provider.
"""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from orch.contracts import Rejected, canonical
from orch.security_acceptance import CRITERIA, assess, check_mapping


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mapping-only", action="store_true",
                        help="Check that every criterion is documented and every named test exists, without running them")
    parser.add_argument("--image", help="Opt in to the five Docker gates using this preloaded digest-pinned image")
    parser.add_argument("--destination", help="New file for the retained report; existing evidence is never overwritten")
    args = parser.parse_args()
    destination = Path(args.destination).absolute() if args.destination else None
    if destination is not None and (destination.exists() or destination.is_symlink()):
        print("FAIL: use a new report path; existing evidence is never overwritten", file=sys.stderr)
        return 2
    try:
        if args.mapping_only:
            check_mapping()
            print(json.dumps({"kind": "SecurityAcceptanceMapping", "criteria": [c["id"] for c in CRITERIA],
                              "tests": sum(len(c["offline"]) + len(c["docker"]) for c in CRITERIA)}))
            print("PASS: every criterion is documented and every named test exists")
            return 0
        if args.image:
            os.environ["ORCH_DOCKER_TEST_IMAGE"] = args.image
        else:
            os.environ.pop("ORCH_DOCKER_TEST_IMAGE", None)
        report = assess(include_docker=bool(args.image))
    except (Rejected, OSError, ValueError) as exc:
        print("FAIL: " + str(exc), file=sys.stderr)
        return 2
    if destination is not None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(canonical(report))
            stream.flush()
            os.fsync(stream.fileno())
    print(json.dumps({"status": report["status"], "tests_run": report["tests_run"],
                      "docker_gates_included": report["docker_gates_included"],
                      "criteria": {c["id"]: c["status"] for c in report["criteria"]},
                      "unmet_tests": report["unmet_tests"],
                      "independent_review": report["independent_review"],
                      "live_authorized": report["live_authorized"]}))
    if report["status"] != "covered":
        print("FAIL: a criterion's named tests did not pass", file=sys.stderr)
        return 2
    print("PASS: every criterion's named tests passed on this host; deferred items remain listed in the report")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
