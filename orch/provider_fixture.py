"""Stdlib-only fixture CLI: no tools, network, workspace editing or database access."""
import argparse
import hashlib
import json
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-p")
    parser.add_argument("--behavior", default="normal", choices=["normal", "invalid-json", "nonzero", "flood", "wait", "secret", "wrong-task", "wrong-shape"])
    args = parser.parse_args()
    if args.behavior == "wait":
        time.sleep(30)
        return
    if args.behavior == "nonzero":
        print("fixture error", file=sys.stderr)
        raise SystemExit(7)
    if args.behavior == "flood":
        print("x" * 200000)
        return
    if args.behavior == "invalid-json":
        print("not JSON")
        return
    if args.behavior == "secret":
        print("token=fixture-canary")
        return
    prompt = json.loads(args.p or sys.stdin.buffer.read(65537))
    invocation = prompt["invocation"]
    context = [item["data"] for item in prompt["artifacts"]]
    scenario = prompt["fixture_scenario"]
    role = invocation["role"]

    def contract(kind, **fields):
        return {"schema_version": "1.0.0", "contract_type": kind,
                "id": hashlib.sha256((invocation["id"] + kind).encode()).hexdigest()[:32],
                "task_id": invocation["task_id"], "created_at": invocation["created_at"], **fields}

    if role == "lead_planner":
        if scenario["plan_failure"]:
            raise SystemExit(1)
        spec = context[0]
        result = contract("ImplementationPlan", spec=prompt["artifacts"][0]["contract_reference"], repository=spec["repository"], planner_invocation_id=invocation["id"],
                          steps=["Replace greeting.txt with hello world"], allowed_paths=["greeting.txt"], permitted_tests=prompt["fixture_tests"],
                          risks=["Trusted fixture only"], max_changed_bytes=100, max_attempts=3)
    elif role == "junior":
        attempt = context[2]["attempt"]
        if attempt <= scenario["implementation_failures"]:
            raise SystemExit(1)
        result = {"content": "wrong\n" if attempt <= scenario["test_failures"] else "hello world\n"}
    elif role == "reviewer":
        if scenario["review_failure"]:
            raise SystemExit(1)
        choice = scenario["reviews"][min(prompt["fixture_review_index"], len(scenario["reviews"]) - 1)]
        result = contract("ReviewDecision", request=prompt["artifacts"][-1]["contract_reference"], reviewer_invocation_id=invocation["id"], decision=choice,
                          findings=[] if choice == "ACCEPT" else [{"severity": "low", "message": "Fixture requests another implementation", "path": "greeting.txt", "line": 1}], summary=choice)
    else:
        raise SystemExit(2)
    if args.behavior == "wrong-task":
        result["task_id"] = "wrong-task"
    if args.behavior == "wrong-shape":
        result = {"approved": True, "command": "arbitrary shell"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
