"""Rebuild the committed v1 design schemas and structural examples deterministically."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
S = {"type": "string", "minLength": 1}
N = {"type": "integer", "minimum": 0}
B = {"type": "boolean"}
ID = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"}
HASH = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
TIME = {"type": "string", "format": "date-time", "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$"}
PATH = {"type": "string", "pattern": r"^(?!.*(?:^|/)\.{1,2}(?:/|$))[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$"}


def enum(*values):
    return {"type": "string", "enum": list(values)}


def array(item, minimum=0):
    return {"type": "array", "items": item, "minItems": minimum}


def nullable(item):
    return {"anyOf": [item, {"type": "null"}]}


def ref(name):
    return {"$ref": f"#/$defs/{name}"}


def obj(**props):
    return {"type": "object", "additionalProperties": False,
            "properties": props, "required": list(props)}


STATES = "DRAFT_SPEC AWAITING_CLARIFICATION SPEC_READY PLANNING AWAITING_PLAN_APPROVAL READY_FOR_IMPLEMENTATION IMPLEMENTING TESTING REVIEWING CHANGES_REQUESTED AWAITING_ACTION_APPROVAL READY_FOR_PR PR_CREATED BLOCKED FAILED COMPLETED CANCELLED".split()
ROLE = enum("human", "project_manager", "lead_clarifier", "lead_planner", "junior", "reviewer", "guardian", "orchestrator", "test_runner", "action_broker")
defs = {
    "ArtifactRef": obj(artifact_id=ID, sha256=HASH, media_type=S, size_bytes=N,
                       trust=enum("untrusted", "trusted_receipt", "human_approved"),
                       producer_id=ID, redacted=B),
    "DocumentRef": obj(id=ID, sha256=HASH),
    "SecretRef": obj(broker=ID, reference=ID),
    "RepositoryRef": obj(repository_id=ID, base_commit={"type": "string", "pattern": "^(?:[a-f0-9]{40}|[a-f0-9]{64})$"}, branch=S),
    "Error": obj(code=S, message=S, retryable=B),
    "Finding": obj(severity=enum("info", "low", "medium", "high", "critical"),
                   message=S, path=nullable(PATH), line=nullable({"type": "integer", "minimum": 1})),
    "Command": obj(recipe_id=ID, recipe_sha256=HASH, argv=array(S, 1),
                   cwd=enum(".", "workspace"), timeout_seconds={"type": "integer", "minimum": 1, "maximum": 3600}),
    "EnvironmentMetadata": obj(os=S, architecture=S, tool_version=S, image_digest=HASH),
    "Actor": obj(principal_id=ID, role=ROLE),
}
ART = ref("ArtifactRef")
DOC = ref("DocumentRef")
contracts = []


def contract(name, **props):
    contracts.append(name)
    defs[name] = obj(schema_version={"const": "1.0.0"}, contract_type={"const": name},
                     id=ID, task_id=ID, created_at=TIME, **props)


contract("TaskSpec", revision=N, title=S, request=ART, repository=ref("RepositoryRef"),
         acceptance_criteria=array(S, 1), constraints=array(S), allowed_paths=array(PATH, 1),
         external_references=array(S), confirmed_by=nullable(ID))
contract("ClarificationRequest", spec=DOC, invocation_id=ID,
         questions=array(obj(question_id=ID, question=S), 1))
contract("ClarificationResponse", request=DOC, actor=ref("Actor"),
         answers=array(obj(question_id=ID, answer=S), 1))
contract("ImplementationPlan", spec=DOC, repository=ref("RepositoryRef"), planner_invocation_id=ID,
         steps=array(S, 1), allowed_paths=array(PATH, 1), permitted_tests=array(ref("Command"), 1),
         risks=array(S), max_changed_bytes={"type": "integer", "minimum": 1}, max_attempts={"type": "integer", "minimum": 1, "maximum": 3})
contract("WorkOrder", spec=DOC, plan=DOC, plan_approval=DOC, repository=ref("RepositoryRef"),
         attempt={"type": "integer", "minimum": 1}, workspace_id=ID, allowed_paths=array(PATH, 1),
         permitted_tests=array(ref("Command"), 1), allowed_capabilities=array(S), deadline=TIME,
         max_changed_bytes={"type": "integer", "minimum": 1})
contract("AgentInvocation", role=ROLE, provider=ID, model=nullable(S), adapter_version=S,
         prompt_version=S, system_instructions=ART, supplied_artifacts=array(ART),
         context_manifest=ART, workspace_id=nullable(ID), allowed_capabilities=array(S),
         timeout_seconds={"type": "integer", "minimum": 1, "maximum": 3600},
         max_output_bytes={"type": "integer", "minimum": 1}, expected_result_contract=S)
contract("AgentInvocationReceipt", invocation_id=ID, provider=ID, model=nullable(S),
         started_at=TIME, ended_at=TIME, exit_code=nullable({"type": "integer"}),
         stdout=ART, stderr=ART, output_truncated=B, structured_result=nullable(ART),
         input_tokens=nullable(N), output_tokens=nullable(N), cost=nullable(obj(amount={"type": "number", "minimum": 0}, currency=S)),
         error=nullable(ref("Error")))
contract("ToolRequest", operation_id=ID, invocation_id=nullable(ID), actor=ref("Actor"),
         work_order=nullable(DOC), workspace_id=nullable(ID), tool=enum("apply_patch", "test", "git", "github", "jira", "secret", "network", "container", "infrastructure"),
         command=nullable(ref("Command")), target_paths=array(PATH), payload=nullable(ART),
         secret_references=array(ref("SecretRef")), request_sha256=HASH, policy_version=S)
contract("ToolDecision", operation_id=ID, request=DOC, policy_version=S,
         decision=enum("allow", "deny", "modify", "require_human_approval"), reasons=array(S, 1),
         effective_request=nullable(DOC), approval_request=nullable(DOC), expires_at=TIME)
contract("PatchReceipt", operation_id=ID, work_order=DOC, producer=ref("Actor"), repository=ref("RepositoryRef"),
         started_at=TIME, ended_at=TIME, requested_commands=array(ref("Command")),
         executed_commands=array(obj(argv=array(S, 1), working_directory=S, started_at=TIME,
                                     ended_at=TIME, exit_code=nullable({"type": "integer"}), stdout=ART, stderr=ART)),
         snapshot_sha256=HASH, diff=ART, changed_files=array(obj(path=PATH, change=enum("add", "modify", "delete"),
         before_sha256=nullable(HASH), after_sha256=nullable(HASH)), 1), changed_bytes=N)
contract("TestRequest", operation_id=ID, work_order=DOC, patch=DOC, snapshot_sha256=HASH,
         command=ref("Command"), runner_image_digest=HASH)
contract("TestReceipt", operation_id=ID, request=DOC, patch=DOC, snapshot_sha256=HASH,
         producer=ref("Actor"), requested_command=ref("Command"), executed_argv=array(S, 1),
         working_directory=S, environment=ref("EnvironmentMetadata"), started_at=TIME, ended_at=TIME,
         exit_code=nullable({"type": "integer"}), outcome=enum("passed", "failed", "timeout", "error"),
         stdout=ART, stderr=ART, output_truncated=B, error=nullable(ref("Error")))
contract("ReviewRequest", spec=DOC, plan=DOC, patch=DOC, test_receipts=array(DOC, 1),
         implementer_invocation_id=ID, reviewer_invocation_id=ID)
contract("ReviewDecision", request=DOC, reviewer_invocation_id=ID,
         decision=enum("ACCEPT", "REJECT", "NEEDS_CHANGES"), findings=array(ref("Finding")), summary=S)
contract("ApprovalRequest", kind=enum("plan", "action"), subject=DOC, subject_sha256=HASH,
         task_revision=N, policy_version=S, summary=S, evidence=array(DOC, 1), expires_at=TIME)
contract("ApprovalDecision", request=DOC, subject_sha256=HASH, actor=ref("Actor"),
         decision=enum("approve", "reject"), reason=S, decided_at=TIME)
contract("ExternalActionReceipt", operation_id=ID, request=DOC, approval=DOC, adapter=ID,
         simulated=B, destination=S, outcome=enum("succeeded", "failed", "unknown"),
         external_id=nullable(S), external_url=nullable({"type": "string", "format": "uri"}),
         started_at=TIME, ended_at=TIME, response=nullable(ART), error=nullable(ref("Error")))
contract("TaskEvent", sequence={"type": "integer", "minimum": 1}, task_revision=N,
         event_type=S, actor=ref("Actor"), provider=nullable(ID), model=nullable(S),
         invocation_id=nullable(ID), operation_id=nullable(ID), from_state=nullable(enum(*STATES)),
         to_state=nullable(enum(*STATES)), payload=ART, artifacts=array(ART))
contract("TaskState", revision=N, state=enum(*STATES), assigned_role=nullable(ROLE),
         active_invocation_id=nullable(ID), active_operation_id=nullable(ID), container_id=nullable(ID),
         resume_state=nullable(enum(*[s for s in STATES if s not in ("BLOCKED", "FAILED", "COMPLETED", "CANCELLED")])),
         spec=nullable(DOC), plan=nullable(DOC), work_order=nullable(DOC), patch=nullable(DOC),
         pending_approval=nullable(DOC), last_event_sequence=N, error=nullable(ref("Error")))

# Shape constraints complement (but cannot replace) authoritative domain checks.
defs["ApprovalDecision"]["properties"]["actor"] = obj(principal_id=ID, role={"const": "human"})
defs["PatchReceipt"]["properties"]["producer"] = obj(principal_id=ID, role={"const": "action_broker"})
defs["TestReceipt"]["properties"]["producer"] = obj(principal_id=ID, role={"const": "test_runner"})
defs["TestReceipt"]["allOf"] = [{"if": {"properties": {"outcome": {"const": "passed"}}},
    "then": {"properties": {"exit_code": {"const": 0}, "error": {"type": "null"}}}}]
defs["ExternalActionReceipt"]["allOf"] = [{"if": {"properties": {"simulated": {"const": True}}},
    "then": {"properties": {"external_url": {"type": "null"}}}}]
defs["ToolDecision"]["allOf"] = [
    {"if": {"properties": {"decision": {"const": "modify"}}},
     "then": {"properties": {"effective_request": DOC}}},
    {"if": {"properties": {"decision": {"const": "require_human_approval"}}},
     "then": {"properties": {"approval_request": DOC}}},
]


def sample(schema):
    if "$ref" in schema:
        return sample(defs[schema["$ref"].split("/")[-1]])
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    if "anyOf" in schema:
        return None
    if schema.get("type") == "object":
        return {k: sample(v) for k, v in schema["properties"].items()}
    if schema.get("type") == "array":
        return [sample(schema["items"])] * schema.get("minItems", 0)
    if schema.get("type") in ("integer", "number"):
        return schema.get("minimum", 0)
    if schema.get("type") == "boolean":
        return False
    if schema.get("format") == "date-time":
        return "2026-09-12T14:00:00Z"
    if schema == HASH:
        return "a" * 64
    if schema.get("pattern", "").startswith("^(?:[a-f0-9]"):
        return "b" * 40
    return "example"


if __name__ == "__main__":
    out = ROOT / "contracts" / "v1"
    out.mkdir(parents=True, exist_ok=True)
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema",
              "$id": "urn:orchd:contracts:1.0.0", "title": "orchd.ai v1 contracts",
              "oneOf": [ref(n) for n in contracts], "$defs": defs}
    (out / "contracts.schema.json").write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
    examples = {n: sample(defs[n]) for n in contracts}
    examples["TestReceipt"]["error"] = None
    examples["TestReceipt"]["exit_code"] = 0
    (out / "examples.json").write_text(json.dumps(examples, indent=2) + "\n", encoding="utf-8")
    print(f"Generated {len(contracts)} contract definitions and structural examples")
