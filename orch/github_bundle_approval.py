"""Journal-bound portable review previews; no approval decision or dispatch authority."""
from datetime import datetime
import re

from .contracts import Rejected, canonical, digest
from .github_approval import prepare_approval, check_approval
from .github_bundle import _verified_bundle
from .github_evidence import RECORD_ROLES, _check_tool_scope


def _evidence(payload):
    selection = payload['selection']
    return {'task_id': selection['task_id'],
            **{role + '_sha256': selection[role]['sha256'] for role in RECORD_ROLES}}


def _policy_current(bundle, checked):
    window = bundle['binding']['assessment']['binding']['claims']['policy_window']
    return datetime.fromisoformat(window['issued_at']) <= datetime.fromisoformat(checked) < datetime.fromisoformat(window['expires_at'])


def _tool_scope(store, payload, bundle, operation, scope):
    claims = bundle['binding']['assessment']['binding']['claims']
    return _check_tool_scope(store, claims['tool_request'], payload['selection']['task_id'], operation, scope)


def prepare_bundle_approval(journal, operation, expected_scope_sha256, source, expected_bundle_sha256):
    with _verified_bundle(source, expected_bundle_sha256) as (store, payload, bundle):
        if bundle['status'] != 'claims_consistent':
            raise Rejected('Bundle claims must be current before preview')
        journal.db.execute('BEGIN')
        try:
            journal._check_schema()
            record = journal.get(operation)
            if record['state'] != 'prepared' or record['sha256'] != expected_scope_sha256:
                raise Rejected('Bundle preview requires expected prepared scope')
            journal.db.execute('COMMIT')
        except BaseException:
            journal.db.execute('ROLLBACK')
            raise
        tool = _tool_scope(store, payload, bundle, operation, record['scope'])
        if tool['status'] != 'matches':
            raise Rejected('Bundle tool request does not match journal scope')
        approval = prepare_approval(journal, operation, expected_scope_sha256, _evidence(payload))
        if not _policy_current(bundle, approval['binding']['issued_at']):
            raise Rejected('Bundle policy expired during preview preparation')
        binding = {'schema_version': '1.0.0', 'bundle_sha256': expected_bundle_sha256, 'approval': approval}
        return {'kind': 'GitHubBundleApprovalPreview', 'binding': binding, 'sha256': digest(binding),
                'evidence_verified': False, 'live_authorized': False, 'retry_allowed': False}


def _preview(preview, expected_sha256, expected_bundle_sha256):
    try:
        binding = preview['binding']
        if not isinstance(expected_bundle_sha256, str) or not re.fullmatch('[a-f0-9]{64}', expected_bundle_sha256):
            raise Rejected('Expected bundle digest required')
        expected_binding = {'schema_version': '1.0.0', 'bundle_sha256': expected_bundle_sha256, 'approval': binding['approval']}
        expected = {'kind': 'GitHubBundleApprovalPreview', 'binding': expected_binding, 'sha256': digest(expected_binding),
                    'evidence_verified': False, 'live_authorized': False, 'retry_allowed': False}
        if canonical(preview) != canonical(expected) or expected['sha256'] != expected_sha256:
            raise Rejected('Bundle preview binding mismatch')
        return binding['approval']
    except (ValueError, KeyError, TypeError, RecursionError) as exc:
        raise Rejected('Invalid bundle approval preview') from exc


def assess_bundle_approval(journal, preview, expected_sha256, source, expected_bundle_sha256):
    approval_preview = _preview(preview, expected_sha256, expected_bundle_sha256)
    try:
        initial_evidence = approval_preview['binding']['evidence']
        approval_sha = approval_preview['sha256']
    except (TypeError, KeyError) as exc:
        raise Rejected('Invalid nested approval preview') from exc
    approval = check_approval(journal, approval_preview, approval_sha, initial_evidence)
    bundle = tool = None
    if approval['status'] == 'current':
        with _verified_bundle(source, expected_bundle_sha256) as (store, payload, bundle):
            binding = approval_preview['binding']
            tool = _tool_scope(store, payload, bundle, binding['operation_id'], binding['scope'])
            approval = check_approval(journal, approval_preview, approval_sha, _evidence(payload))
    blockers = ['approval:' + reason for reason in approval['binding']['blockers']]
    if bundle is not None:
        blockers.extend('evidence:' + reason for reason in bundle['binding']['assessment']['binding']['claims']['blockers'])
        if not _policy_current(bundle, approval['binding']['checked_at']):
            blockers.append('evidence:policy_not_current')
    if tool is not None:
        blockers.extend('tool:' + reason for reason in tool['binding']['blockers'])
    binding = {'schema_version': '1.0.0', 'preview_sha256': expected_sha256,
               'bundle_sha256': expected_bundle_sha256, 'approval': approval,
               'bundle': bundle, 'tool_scope': tool, 'blockers': sorted(set(blockers))}
    return {'kind': 'GitHubBundleApprovalAssessment', 'binding': binding, 'sha256': digest(binding),
            'status': 'blocked' if blockers else 'current', 'bundle_bytes_verified': bundle is not None,
            'evidence_verified': False, 'live_authorized': False, 'retry_allowed': False}
