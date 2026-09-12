import argparse
import json
from pathlib import Path

from .api import serve
from .engine import Engine
from .execution import Executor
from .provider import Scenario
from .provider import MockProvider
from .cli_provider import FixtureCliProvider


def main():
    parser = argparse.ArgumentParser(description="Offline orchd.ai fixture workflow")
    parser.add_argument("command", choices=["demo", "serve", "history", "backup", "restore", "gc", "recover", "doctor", "verify-runtime", "provider-check"])
    parser.add_argument("--data", default=".runtime/phase2")
    parser.add_argument("--trusted-fixture", action="store_true", help="Local fixed test programs only; NOT a sandbox")
    parser.add_argument("--fixture-provider", choices=["in-process", "cli"], default="in-process", help="Offline provider fixture transport")
    parser.add_argument("--image", help="Preloaded digest-pinned Python image for Docker")
    parser.add_argument("--task")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--destination")
    parser.add_argument("--expected-revision", type=int)
    parser.add_argument("--provider", choices=["github_copilot_cli", "abc_binary_ai_placeholder"])
    parser.add_argument("--executable", help="Absolute provider executable path for read-only inventory")
    parser.add_argument("--expected-sha256", help="Approved executable digest for provider-check")
    args = parser.parse_args()
    if args.command == "provider-check":
        if not args.provider:
            parser.error("provider-check requires --provider")
        from .copilot import provider_check
        print(json.dumps(provider_check(args.provider, args.executable, args.expected_sha256), indent=2))
        raise SystemExit(2)  # No live provider is admitted in this milestone.
    if args.command == "doctor":
        from .readiness import doctor
        report = doctor(args.image)
        print(json.dumps(report, indent=2))
        raise SystemExit(0 if report["status"] == "ready" else 2)
    if args.command == "verify-runtime":
        if not args.image or not args.destination:
            parser.error("verify-runtime requires --image and --destination")
        from .readiness import verify_runtime
        report = verify_runtime(args.image, args.destination)
        print(json.dumps(report, indent=2))
        raise SystemExit(0 if report["status"] == "passed" else 2)
    if args.command == "restore":
        from .maintenance import restore
        print(json.dumps(restore(args.data, args.destination)))
        return
    if args.command in ("backup", "gc"):
        from .storage import Store
        from .maintenance import backup, collect_orphans
        store = Store(args.data)
        try:
            print(json.dumps(backup(store, args.destination) if args.command == "backup" else collect_orphans(store)))
        finally:
            store.close()
        return
    engine = Engine(args.data, Executor("trusted-fixture" if args.trusted_fixture else "docker", args.image), provider_factory=FixtureCliProvider if args.fixture_provider == "cli" else MockProvider)
    try:
        if args.command == "serve":
            serve(engine, args.port)
        elif args.command == "history":
            print(json.dumps([{ "event": e, "payload": json.loads(engine.store.read_artifact(e["payload"], args.task))} for e in engine.events(args.task)], indent=2))
        elif args.command == "recover":
            print(json.dumps(engine.recover(args.task, args.expected_revision)))
        else:
            task = engine.create_task(scenario=Scenario(reviews=("NEEDS_CHANGES", "ACCEPT")))
            engine.run(task)
            print("Paused:", engine.task(task)["state"]["state"])
            # Explicit demo script represents the two human boundary calls; run() never approves.
            for _ in range(2):
                state = engine.task(task)["state"]
                engine.approve(task, state["pending_approval"]["id"], "approve", state["revision"])
                engine.run(task)
                print("State:", engine.task(task)["state"]["state"])
            print("Task:", task)
            print("Events:", len(engine.events(task)))
            print("Data:", engine.store.root)
            print("Simulation:", engine.task(task)["context"]["external"])
    finally:
        engine.close()


if __name__ == "__main__":
    main()
