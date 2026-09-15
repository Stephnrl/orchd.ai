"""Validate design contracts. Does not claim to test an unimplemented runtime."""
import copy
import json
from datetime import datetime
from pathlib import Path
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
schema = json.loads((ROOT / "contracts/v1/contracts.schema.json").read_text(encoding="utf-8"))
examples = json.loads((ROOT / "contracts/v1/examples.json").read_text(encoding="utf-8"))
Draft202012Validator.check_schema(schema)
formats = FormatChecker()


@formats.checks("date-time", raises=ValueError)
def utc_timestamp(value):
    if not isinstance(value, str):
        return True
    return value.endswith("Z") and datetime.fromisoformat(value).utcoffset().total_seconds() == 0


validator = Draft202012Validator(schema, format_checker=formats)
expected = {r["$ref"].split("/")[-1] for r in schema["oneOf"]}
assert len(expected) == 21 and set(examples) == expected


def check_refs(value):
    if isinstance(value, dict):
        if "$ref" in value:
            assert value["$ref"].startswith("#/$defs/")
            assert value["$ref"].split("/")[-1] in schema["$defs"]
        for child in value.values():
            check_refs(child)
    elif isinstance(value, list):
        for child in value:
            check_refs(child)


check_refs(schema)
negative_count = 0


def rejected(value):
    global negative_count
    assert not validator.is_valid(value), f"Unexpected acceptance: {value}"
    negative_count += 1


for name, example in examples.items():
    validator.validate(example)
    for field in schema["$defs"][name]["required"]:
        invalid = copy.deepcopy(example)
        del invalid[field]
        rejected(invalid)
    for key, value in [("unexpected", True), ("schema_version", "2.0.0"),
                       ("created_at", "yesterday"), ("task_id", "../other")]:
        rejected({**example, key: value})

for path in ["../secret", "/etc/passwd", "a/../../secret", "a/./b", "C:\\secret", "//server/share"]:
    rejected({**examples["TaskSpec"], "allowed_paths": [path]})
rejected({**examples["ReviewDecision"], "decision": "LGTM"})
rejected({**examples["TaskSpec"], "created_at": "2026-02-30T14:00:00Z"})
rejected({**examples["ApprovalDecision"], "actor": {"principal_id": "agent", "role": "junior"}})
rejected({**examples["TestReceipt"], "exit_code": 1})
rejected({**examples["TestReceipt"], "producer": {"principal_id": "agent", "role": "junior"}})
rejected({**examples["ToolDecision"], "decision": "modify", "effective_request": None})
rejected({**examples["ToolDecision"], "decision": "require_human_approval", "approval_request": None})
rejected({**examples["ExternalActionReceipt"], "simulated": True, "external_url": "https://github.com/example/example/pull/1"})
print(f"PASS: 21 examples, {negative_count} negative cases, metaschema and local references")
