"""Replaceable deterministic policy and external-action actors; no state authority."""
from datetime import datetime, timedelta, timezone
from typing import Protocol

from contracts.interfaces import ValidatedContract
from .contracts import Rejected, error, make, now, ref, validate
from .fixtures import recipe


class FixtureGuardian:
    def evaluate(self, request: ValidatedContract, approval_request=None) -> ValidatedContract:
        tool = validate(dict(request.payload), "ToolRequest")
        permitted = (tool["tool"] in ("apply_patch", "test", "github")
                     and tool["secret_references"] == [] and tool["policy_version"] == "fixture-v1"
                     and (tool["command"] is None if tool["tool"] != "test" else tool["command"] == recipe())
                     and tool["target_paths"] == ([] if tool["tool"] == "github" else ["greeting.txt"]))
        choice = "deny"
        if permitted:
            choice = "require_human_approval" if tool["tool"] == "github" else "allow"
        if choice == "require_human_approval" and approval_request is None:
            choice = "deny"
        decision = make("ToolDecision", tool["task_id"], operation_id=tool["operation_id"], request=ref(tool), policy_version="fixture-v1",
                        decision=choice, reasons=["fixed offline fixture policy"], effective_request=None,
                        approval_request=approval_request if choice == "require_human_approval" else None,
                        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z"))
        return ValidatedContract("ToolDecision", decision)


class ExternalAction(Protocol):
    def execute(self, operation_id, request, approval, fail=False) -> dict:
        """Return ExternalActionReceipt. Workflow engine enforces approval before dispatch."""
        ...


class SimulatedPRAction:
    def __init__(self, store):
        self.store = store

    def execute(self, operation_id, request, approval, fail=False):
        validate(request, "ToolRequest")
        validate(approval, "ApprovalDecision")
        if request["operation_id"] != operation_id or request["tool"] != "github":
            raise Rejected("External operation mismatch")
        import json
        payload = json.loads(self.store.read_artifact(request["payload"], request["task_id"]))
        repo = payload["repository"]
        return make("ExternalActionReceipt", request["task_id"], operation_id=operation_id, request=ref(request), approval=ref(approval), adapter="simulated-github", simulated=True,
                    destination=repo["repository_id"] + "/" + repo["branch"], outcome="failed" if fail else "succeeded", external_id=None if fail else "sim-pr-" + operation_id, external_url=None,
                    started_at=now(), ended_at=now(), response=self.store.artifact(request["task_id"], {"repository": repo["repository_id"], "branch": repo["branch"], "pull_request_number": int(operation_id[:8], 16), "task_id": request["task_id"], "simulation": True}), error=error("simulated_action_failure") if fail else None)
