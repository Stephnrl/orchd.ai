"""Schema validation and canonical, task-bound contract helpers."""
import copy
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
import rfc8785

SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "contracts/v1/contracts.schema.json").read_text())
FORMATS = FormatChecker()


@FORMATS.checks("date-time", raises=ValueError)
def timestamp(value):
    return not isinstance(value, str) or (value.endswith("Z") and datetime.fromisoformat(value).utcoffset().total_seconds() == 0)


VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FORMATS)


class Rejected(ValueError):
    pass


class Transient(Rejected):
    """A refusal that may stop being one without anybody doing anything.

    A task held by another agent is released or its lease lapses; a full admission bound
    empties as work finishes. Both refuse now and may permit later, which is a different
    answer from "your role may not do this" and deserves a different reaction from a caller.
    It stays a `Rejected`, so nothing that refuses broadly starts permitting narrowly.
    """


def validate(value, kind=None):
    try:
        VALIDATOR.validate(value)
    except Exception as exc:
        raise Rejected("Invalid contract") from exc
    if kind and value["contract_type"] != kind:
        raise Rejected("Wrong contract type")
    return copy.deepcopy(value)


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def uid():
    return uuid.uuid4().hex


def canonical(value):
    return rfc8785.dumps(value)


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def make(contract_type, task_id, **fields):
    return validate(dict(schema_version="1.0.0", contract_type=contract_type, id=uid(), task_id=task_id, created_at=now(), **fields), contract_type)


def ref(value):
    return {"id": value["id"], "sha256": digest(value)}


def error(code):
    return {"code": code, "message": code, "retryable": False}


def actor(role):
    return {"principal_id": role, "role": role}


def redact(text):
    # No secrets are accepted by the fixture API. Defense in depth for outputs.
    text = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", "[REDACTED]", text, flags=re.S)
    return re.sub(r"(?i)(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|bearer\s+\S+|(?:token|password|secret)\s*[=:]\s*\S+)", "[REDACTED]", text)
