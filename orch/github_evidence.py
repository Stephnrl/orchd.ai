"""Task-owned local artifact byte checks; no semantic approval or execution authority."""
import hashlib
import json
from datetime import datetime

from jsonschema import Draft202012Validator

from .contracts import Rejected, SCHEMA, canonical, digest, validate, ref, now
from .github_approval import _validate_evidence, check_approval

ARTIFACT = Draft202012Validator({'$defs': SCHEMA['$defs'], '$ref': '#/$defs/ArtifactRef'})
MAX_BYTES = 1024 * 1024


def assess_approval_evidence(journal, store, preview, expected_sha256, evidence):
    """Compose fresh checks, then recheck approval after artifact I/O."""
    approval = check_approval(journal, preview, expected_sha256, evidence)
    artifacts = claims = tool_scope = None
    if approval['status'] == 'current':
        claims = check_evidence_contents(store, evidence)
        artifacts = claims['binding']['artifact_evidence']
        tool_scope = _check_tool_scope(store, claims['binding']['claims']['tool_request'], evidence['task_id'],
                                       preview['binding']['operation_id'], preview['binding']['scope'])
        approval = check_approval(journal, preview, expected_sha256, evidence)
    blockers = ['approval:' + reason for reason in approval['binding']['blockers']]
    if claims is not None:
        blockers.extend('evidence:' + reason for reason in claims['binding']['claims']['blockers'])
        window = claims['binding']['claims']['policy_window']
        checked = datetime.fromisoformat(approval['binding']['checked_at'])
        if not datetime.fromisoformat(window['issued_at']) <= checked < datetime.fromisoformat(window['expires_at']):
            blockers.append('evidence:policy_not_current')
    if tool_scope is not None:
        blockers.extend('tool:' + reason for reason in tool_scope['binding']['blockers'])
    binding = {"schema_version": "1.0.0", "approval": approval, "artifact_evidence": artifacts,
               "evidence_claims": claims, "tool_scope": tool_scope, "blockers": sorted(set(blockers))}
    return {"kind": "GitHubCombinedApprovalAssessment", "binding": binding, "sha256": digest(binding),
            "status": 'blocked' if blockers else 'current', "artifact_bytes_verified": artifacts is not None,
            "evidence_verified": False, "live_authorized": False, "retry_allowed": False}


def check_evidence(store, evidence):
    return _check_evidence(store, evidence)


def check_evidence_contents(store, evidence):
    return _check_evidence(store, evidence, contents_check=True)


def _artifact_bytes(store, item, task):
    if not ARTIFACT.is_valid(item) or type(item['size_bytes']) is not int or not 0 <= item['size_bytes'] <= MAX_BYTES:
        raise Rejected("Invalid or oversized artifact reference")
    row = store.db.execute("SELECT CASE WHEN length(CAST(payload AS BLOB))<=8192 THEN payload END FROM artifacts WHERE id=? AND task_id=?", (item['artifact_id'], task)).fetchone()
    if row is None or row[0] != canonical(item).decode():
        raise Rejected("Artifact reference is not owned by task")
    directory = store.root / 'artifacts'
    path = directory / item['sha256']
    if directory.is_symlink() or not directory.is_dir() or path.is_symlink() or not path.is_file():
        raise Rejected("Evidence artifact must be a regular file")
    with path.open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) != item['size_bytes'] or hashlib.sha256(raw).hexdigest() != item['sha256']:
        raise Rejected("Evidence artifact byte mismatch")
    return raw


def _check_tool_scope(store, reference, task, operation, scope):
    store.db.execute('BEGIN')
    try:
        tool = _stored_record(store, reference, task, 'ToolRequest')
        blockers = []
        if tool['operation_id'] != operation:
            blockers.append('operation_mismatch')
        if tool['request_sha256'] != digest({key: value for key, value in tool.items() if key != 'request_sha256'}):
            blockers.append('request_digest_mismatch')
        if (tool['tool'] != 'github' or tool['command'] is not None or tool['target_paths']
                or tool['secret_references'] or tool['work_order'] is not None or tool['workspace_id'] is not None):
            blockers.append('unsupported_tool_scope')
        payload_sha = None
        if tool['payload'] is None:
            blockers.append('missing_payload')
        else:
            raw = _artifact_bytes(store, tool['payload'], task)
            payload_sha = hashlib.sha256(raw).hexdigest()
            if raw != canonical({"adapter": "github-draft-preview", "scope": scope}):
                blockers.append('payload_scope_mismatch')
        binding = {"schema_version": "1.0.0", "tool_request": reference, "scope_sha256": digest(scope),
                   "payload_sha256": payload_sha, "blockers": blockers}
        result = {"kind": "GitHubToolScopeAssessment", "binding": binding, "sha256": digest(binding),
                  "status": 'blocked' if blockers else 'matches', "live_authorized": False}
        store.db.execute('COMMIT')
        return result
    except BaseException:
        store.db.execute('ROLLBACK')
        raise


def _stored_record(store, reference, task, kind):
    row = store.db.execute("SELECT CASE WHEN length(CAST(payload AS BLOB))<=? THEN payload END FROM records WHERE id=? AND task_id=? AND kind=?", (MAX_BYTES, reference['id'], task, kind)).fetchone()
    if row is None:
        raise Rejected("Missing task-owned evidence record")
    try:
        document = validate(json.loads(row[0]), kind)
        if canonical(document).decode() != row[0] or ref(document) != reference or document['task_id'] != task:
            raise Rejected("Evidence record binding mismatch")
        return document
    except (ValueError, TypeError, RecursionError) as exc:
        raise Rejected("Invalid evidence record") from exc


def _assess_contents(store, task, contents):
    documents = {}
    for role, kind in (('patch', 'PatchReceipt'), ('test', 'TestReceipt'), ('review', 'ReviewDecision'), ('policy', 'ToolDecision')):
        try:
            document = validate(json.loads(contents[role]), kind)
            if canonical(document) != contents[role] or document['task_id'] != task:
                raise Rejected("Noncanonical or cross-task evidence contents")
            documents[role] = _stored_record(store, ref(document), task, kind)
        except (ValueError, TypeError, RecursionError) as exc:
            raise Rejected("Invalid evidence contents") from exc
    patch, test, review, policy = (documents[role] for role in ('patch', 'test', 'review', 'policy'))
    review_request = _stored_record(store, review['request'], task, 'ReviewRequest')
    tool = _stored_record(store, policy['request'], task, 'ToolRequest')
    blockers = []
    if test['patch'] != ref(patch) or test['snapshot_sha256'] != patch['snapshot_sha256']:
        blockers.append('test_patch_mismatch')
    if test['outcome'] != 'passed' or test['exit_code'] != 0 or test['output_truncated']:
        blockers.append('test_not_passed_completely')
    if test['requested_command']['argv'] != test['executed_argv']:
        blockers.append('test_command_mismatch')
    if any(command['exit_code'] != 0 for command in patch['executed_commands']):
        blockers.append('patch_command_failed')
    if review['decision'] != 'ACCEPT' or review['findings']:
        blockers.append('review_not_clean_accept')
    if review_request['patch'] != ref(patch) or review_request['test_receipts'] != [ref(test)]:
        blockers.append('review_evidence_mismatch')
    if (review['reviewer_invocation_id'] != review_request['reviewer_invocation_id']
            or review_request['implementer_invocation_id'] == review_request['reviewer_invocation_id']):
        blockers.append('reviewer_binding_mismatch')
    if (policy['decision'] != 'require_human_approval' or policy['approval_request'] is None
            or policy['effective_request'] is not None or tool['tool'] != 'github'
            or policy['operation_id'] != tool['operation_id'] or policy['policy_version'] != tool['policy_version']):
        blockers.append('policy_request_mismatch')
    checked = now()
    if not datetime.fromisoformat(policy['created_at']) <= datetime.fromisoformat(checked) < datetime.fromisoformat(policy['expires_at']):
        blockers.append('policy_not_current')
    return {"records": {role: ref(document) for role, document in documents.items()},
            "review_request": ref(review_request), "tool_request": ref(tool), "checked_at": checked,
            "policy_window": {"issued_at": policy['created_at'], "expires_at": policy['expires_at']},
            "blockers": blockers}


def _check_evidence(store, evidence, contents_check=False):
    _validate_evidence(evidence)
    directory = store.root / 'artifacts'
    if directory.is_symlink() or not directory.is_dir():
        raise Rejected("Invalid artifact directory")
    store.db.execute('BEGIN')
    try:
        if not store.db.execute('SELECT 1 FROM tasks WHERE id=?', (evidence['task_id'],)).fetchone():
            raise Rejected("Unknown evidence task")
        artifacts, contents = {}, {}
        for role in ('patch', 'test', 'review', 'policy'):
            sha = evidence[role + '_sha256']
            row = store.db.execute("SELECT id, CASE WHEN length(CAST(payload AS BLOB))<=8192 THEN payload END AS payload FROM artifacts WHERE task_id=? AND json_extract(payload,'$.sha256')=? ORDER BY id LIMIT 1", (evidence['task_id'], sha)).fetchone()
            if row is None:
                raise Rejected("Evidence artifact is not owned by the task")
            try:
                item = json.loads(row['payload'])
                if (not ARTIFACT.is_valid(item) or item['artifact_id'] != row['id']
                        or item['sha256'] != sha or canonical(item).decode() != row['payload']):
                    raise Rejected("Invalid artifact metadata")
                if type(item['size_bytes']) is not int or not 0 <= item['size_bytes'] <= MAX_BYTES:
                    raise Rejected("Evidence artifact exceeds budget")
            except (ValueError, TypeError, KeyError, RecursionError) as exc:
                raise Rejected("Invalid evidence artifact metadata") from exc
            raw = _artifact_bytes(store, item, evidence['task_id'])
            artifacts[role] = {"artifact_id": item['artifact_id'], "sha256": sha, "size_bytes": len(raw)}
            if contents_check:
                contents[role] = raw
        binding = {"schema_version": "1.0.0", "task_id": evidence['task_id'],
                   "evidence_sha256": digest(evidence), "artifacts": artifacts}
        result = {"kind": "GitHubArtifactEvidenceAssessment", "binding": binding, "sha256": digest(binding),
                  "status": "bytes_match", "artifact_bytes_verified": True, "evidence_verified": False,
                  "live_authorized": False, "retry_allowed": False}
        if contents_check:
            claims = _assess_contents(store, evidence['task_id'], contents)
            combined = {"schema_version": "1.0.0", "artifact_evidence": result, "claims": claims}
            result = {"kind": "GitHubEvidenceClaimsAssessment", "binding": combined, "sha256": digest(combined),
                      "status": 'blocked' if claims['blockers'] else 'claims_consistent',
                      "artifact_bytes_verified": True, "evidence_verified": False,
                      "live_authorized": False, "retry_allowed": False}
        store.db.execute('COMMIT')
        return result
    except BaseException:
        store.db.execute('ROLLBACK')
        raise
