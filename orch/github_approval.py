"""Offline approval-scope preparation; no decisions, credentials or dispatch authority."""
from datetime import datetime, timedelta
import re

from .contracts import Rejected, digest, now, uid


def prepare_approval(journal, operation_id, expected_sha256, evidence):
    fields = {'task_id', 'patch_sha256', 'test_sha256', 'review_sha256', 'policy_sha256'}
    if not isinstance(evidence, dict) or set(evidence) != fields:
        raise Rejected("Invalid GitHub approval evidence envelope")
    for name in fields - {'task_id'}:
        if not isinstance(evidence[name], str) or not re.fullmatch(r'[a-f0-9]{64}', evidence[name]):
            raise Rejected("Invalid GitHub approval evidence digest")
    journal.db.execute('BEGIN')
    try:
        journal._check_schema()
        record = journal.get(operation_id)
        if record['state'] != 'prepared' or record['sha256'] != expected_sha256:
            raise Rejected("Approval preview requires the expected prepared scope")
        if evidence['task_id'] != record['task_id']:
            raise Rejected("Approval evidence belongs to another task")
        issued = now()
        expires = (datetime.fromisoformat(issued) + timedelta(minutes=15)).isoformat().replace('+00:00', 'Z')
        binding = {"schema_version": "1.0.0", "request_id": uid(), "task_id": record['task_id'],
                   "operation_id": record['operation_id'], "issued_at": issued, "expires_at": expires,
                   "scope_sha256": record['sha256'], "scope": record['scope'], "evidence": dict(evidence)}
        report = {"kind": "GitHubApprovalPreview", "binding": binding, "sha256": digest(binding),
                  "evidence_verified": False, "live_authorized": False, "retry_allowed": False}
        journal.db.execute('COMMIT')
        return report
    except BaseException:
        journal.db.execute('ROLLBACK')
        raise
