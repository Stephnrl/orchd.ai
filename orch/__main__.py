import argparse
import json
from pathlib import Path

from .api import serve
from .engine import Engine
from .execution import Executor
from .provider import Scenario


def main():
    parser = argparse.ArgumentParser(description="Offline orchd.ai fixture workflow")
    parser.add_argument("command", choices=["demo", "serve", "history"])
    parser.add_argument("--data", default=".runtime/phase2")
    parser.add_argument("--trusted-fixture", action="store_true", help="Local fixed test programs only; NOT a sandbox")
    parser.add_argument("--image", help="Preloaded digest-pinned Python image for Docker")
    parser.add_argument("--task")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    engine = Engine(args.data, Executor("trusted-fixture" if args.trusted_fixture else "docker", args.image))
    try:
        if args.command == "serve":
            serve(engine, args.port)
        elif args.command == "history":
            print(json.dumps([{ "event": e, "payload": json.loads(engine.store.read_artifact(e["payload"], args.task))} for e in engine.events(args.task)], indent=2))
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
