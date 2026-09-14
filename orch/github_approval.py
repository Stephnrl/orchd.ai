"""Offline approval-scope preparation; no decisions, credentials or dispatch authority."""
from datetime import datetime, timedelta
import re

from .contracts import Rejected, canonical, digest, now, uid
from .github_journal import validated_intent


def _validate_evidence(evidence):
    fields = {'task_id', 'patch_sha256', 'test_sha256', 'review_sha256', 'policy_sha256'}
    if not isinstance(evidence, dict) or set(evidence) != fields:
        raise Rejected("Invalid GitHub approval evidence envelope")
    if not isinstance(evidence['task_id'], str) or not re.fullmatch(r'[a-f0-9]{32}', evidence['task_id']):
        raise Rejected("Invalid GitHub approval evidence task")
    for name in fields - {'task_id'}:
        if not isinstance(evidence[name], str) or not re.fullmatch(r'[a-f0-9]{64}', evidence[name]):
            raise Rejected("Invalid GitHub approval evidence digest")


def prepare_approval(journal, operation_id, expected_sha256, evidence):
    _validate_evidence(evidence)
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


def check_approval(journal, preview, expected_sha256, evidence):
    _validate_evidence(evidence)
    try:
        binding = preview['binding']
        _validate_evidence(binding['evidence'])
        intent = validated_intent(binding['scope'])
        issued = binding['issued_at']
        if not isinstance(issued, str) or not issued.endswith('Z'):
            raise Rejected("Invalid approval issue time")
        issued_time = datetime.fromisoformat(issued)
        expires = (issued_time + timedelta(minutes=15)).isoformat().replace('+00:00', 'Z')
        if not isinstance(binding['request_id'], str) or not re.fullmatch(r'[a-f0-9]{32}', binding['request_id']):
            raise Rejected("Invalid approval request ID")
        expected_binding = {"schema_version": "1.0.0", "request_id": binding['request_id'],
                            "task_id": intent['task_id'], "operation_id": intent['operation_id'],
                            "issued_at": issued, "expires_at": expires,
                            "scope_sha256": digest(binding['scope']), "scope": binding['scope'],
                            "evidence": binding['evidence']}
        expected = {"kind": "GitHubApprovalPreview", "binding": expected_binding, "sha256": digest(expected_binding),
                    "evidence_verified": False, "live_authorized": False, "retry_allowed": False}
        if (canonical(expected) != canonical(preview) or expected['sha256'] != expected_sha256
                or binding['evidence']['task_id'] != intent['task_id']):
            raise Rejected("Approval preview binding mismatch")
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise Rejected("Invalid saved GitHub approval preview") from exc
    journal.db.execute('BEGIN')
    try:
        journal._check_schema()
        record = journal.get(intent['operation_id'])
        checked = now()
        checked_time = datetime.fromisoformat(checked)
        blockers = []
        if checked_time < issued_time:
            blockers.append('not_yet_valid')
        if checked_time >= datetime.fromisoformat(expires):
            blockers.append('expired')
        if record['state'] != 'prepared':
            blockers.append('journal_not_prepared')
        if record['sha256'] != binding['scope_sha256'] or record['task_id'] != intent['task_id']:
            blockers.append('scope_mismatch')
        if canonical(evidence) != canonical(binding['evidence']):
            blockers.append('evidence_mismatch')
        result_binding = {"schema_version": "1.0.0", "preview_sha256": expected_sha256,
                          "journal_scope_sha256": record['sha256'], "journal_state": record['state'],
                          "evidence_sha256": digest(evidence), "checked_at": checked, "blockers": blockers}
        result = {"kind": "GitHubApprovalAssessment", "binding": result_binding, "sha256": digest(result_binding),
                  "status": "blocked" if blockers else "current", "evidence_verified": False,
                  "live_authorized": False, "retry_allowed": False}
        journal.db.execute('COMMIT')
        return result
    except BaseException:
        journal.db.execute('ROLLBACK')
        raise
