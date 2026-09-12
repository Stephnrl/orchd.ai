"""Deterministic proposal-only implementation of the Phase 1 AgentProvider."""
from dataclasses import dataclass
import json

from contracts.interfaces import HealthResult, ProviderCapabilities, ValidatedContract
from .contracts import Rejected, error, make, now, validate


@dataclass(frozen=True)
class Scenario:
    plan_failure: bool = False
    implementation_failures: int = 0
    test_failures: int = 0
    reviews: tuple[str, ...] = ("ACCEPT",)
    review_failure: bool = False
    external_failure: bool = False

    def __post_init__(self):
        if not self.reviews or any(r not in ("ACCEPT", "NEEDS_CHANGES", "REJECT") for r in self.reviews):
            raise Rejected("Invalid mock review configuration")
        if not 0 <= self.implementation_failures <= 3 or not 0 <= self.test_failures <= 3:
            raise Rejected("Invalid failure budget")


class MockProvider:
    def __init__(self, store, scenario, review_index=0):
        self.store, self.scenario, self.review_index = store, scenario, review_index

    def capabilities(self):
        return ProviderCapabilities(True, True, True, False, ("lead_planner", "junior", "reviewer"))

    async def health_check(self):
        return HealthResult("ready", "mock-v1", "offline fixture provider")

    async def cancel(self, invocation_id):
        return None

    async def invoke(self, request: ValidatedContract) -> ValidatedContract:
        invocation = validate(dict(request.payload), "AgentInvocation")
        task = invocation["task_id"]
        context = [json.loads(self.store.read_artifact(a, task)) for a in invocation["supplied_artifacts"]]
        role = invocation["role"]
        started = now()
        result = None
        failed = False
        if role == "lead_planner":
            spec = context[0]
            from .contracts import ref
            from .fixtures import recipe
            failed = self.scenario.plan_failure
            result = make("ImplementationPlan", task, spec=ref(spec), repository=spec["repository"], planner_invocation_id=invocation["id"],
                          steps=["Replace greeting.txt with hello world"], allowed_paths=["greeting.txt"], permitted_tests=[recipe()],
                          risks=["Trusted fixture only"], max_changed_bytes=100, max_attempts=3)
        elif role == "junior":
            work = context[2]
            failed = work["attempt"] <= self.scenario.implementation_failures
            # Proposal schema is private and closed; only broker creates PatchReceipt.
            result = {"content": "wrong\n" if work["attempt"] <= self.scenario.test_failures else "hello world\n"}
        elif role == "reviewer":
            # Exactly the spec, plan, patch/diff, test, ReviewRequest. No conversation.
            spec, plan, patch, diff, test, review_request = context
            from .contracts import ref
            failed = self.scenario.review_failure
            # Fixture sequencing is operator configuration, never implementer conversation.
            choice = self.scenario.reviews[min(self.review_index, len(self.scenario.reviews) - 1)]
            result = make("ReviewDecision", task, request=ref(review_request), reviewer_invocation_id=invocation["id"], decision=choice,
                          findings=[] if choice == "ACCEPT" else [{"severity": "low", "message": "Fixture requests another implementation", "path": "greeting.txt", "line": 1}], summary=choice)
        else:
            raise Rejected("Mock role unsupported")
        receipt = make("AgentInvocationReceipt", task, invocation_id=invocation["id"], provider="mock", model="deterministic-v1",
                       started_at=started, ended_at=now(), exit_code=1 if failed else 0,
                       stdout=self.store.artifact(task, "mock invocation", "text/plain", "provider_broker"),
                       stderr=self.store.artifact(task, "fixture failure" if failed else "", "text/plain", "provider_broker"),
                       output_truncated=False, structured_result=None if failed else self.store.artifact(task, result),
                       input_tokens=None, output_tokens=None, cost=None, error=error("mock_failure") if failed else None)
        return ValidatedContract("AgentInvocationReceipt", receipt)
