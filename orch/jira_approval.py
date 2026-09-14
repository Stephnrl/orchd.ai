"""Offline Jira review and freshness diagnostics; no approval admission."""
from datetime import datetime, timedelta

from .contracts import Rejected, canonical, digest, now, uid
from .jira_actions import FLAGS, closed, identifier, text_value, request, responses, guarded, preview_action
from .jira_preview import prepare_comment


def instant(value):
    identifier(value, r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z')
    return datetime.fromisoformat(value)


def evidence_check(evidence, task_id):
    closed(evidence, ('task_id', 'review_sha256', 'policy_sha256'))
    if evidence['task_id'] != task_id:
        raise Rejected('Jira evidence task mismatch')
    for key in ('review_sha256', 'policy_sha256'): identifier(evidence[key], '[a-f0-9]{64}')


@guarded
def prepare_approval(journal, operation_id, scope_sha256, evidence, author_key):
    text_value(author_key, 256)
    record = journal.get(operation_id)
    if record['state'] != 'prepared' or record['sha256'] != scope_sha256:
        raise Rejected('Expected prepared Jira scope required')
    scope = record['scope']
    if scope.get('kind') != 'JiraActionScope' and scope['author_key'] != author_key:
        raise Rejected('Expected comment author differs from retained scope')
    evidence_check(evidence, record['task_id'])
    issued = now()
    binding = {'schema_version': '1.0.0', 'request_id': uid(), 'task_id': record['task_id'],
               'operation_id': operation_id, 'scope_sha256': scope_sha256, 'preview_sha256': scope['preview_sha256'],
               'author_key': author_key, 'evidence': evidence, 'issued_at': issued,
               'expires_at': (instant(issued)+timedelta(minutes=15)).isoformat().replace('+00:00', 'Z')}
    return {'kind': 'JiraApprovalPreview', 'binding': binding, 'sha256': digest(binding), 'evidence_verified': False, **FLAGS}


def validated_preview(preview, expected_sha256):
    closed(preview, ('kind', 'binding', 'sha256', 'evidence_verified', *FLAGS))
    binding = preview['binding']
    closed(binding, ('schema_version', 'request_id', 'task_id', 'operation_id', 'scope_sha256', 'preview_sha256',
                     'author_key', 'evidence', 'issued_at', 'expires_at'))
    for key in ('request_id', 'task_id', 'operation_id'): identifier(binding[key], '[a-f0-9]{32}')
    for key in ('scope_sha256', 'preview_sha256'): identifier(binding[key], '[a-f0-9]{64}')
    text_value(binding['author_key'], 256)
    evidence_check(binding['evidence'], binding['task_id'])
    if (binding['schema_version'] != '1.0.0' or preview['kind'] != 'JiraApprovalPreview'
            or preview['sha256'] != digest(binding) or preview['sha256'] != expected_sha256
            or preview['evidence_verified'] is not False or any(preview[key] is not False for key in FLAGS)
            or binding['expires_at'] != (instant(binding['issued_at'])+timedelta(minutes=15)).isoformat().replace('+00:00', 'Z')):
        raise Rejected('Invalid Jira approval preview')
    return binding


def assess_record(record, binding, evidence, checked):
    evidence_check(evidence, binding['task_id'])
    blockers = []
    if record['state'] != 'prepared': blockers.append('journal_not_prepared')
    if record['sha256'] != binding['scope_sha256'] or record['task_id'] != binding['task_id'] or record['scope']['preview_sha256'] != binding['preview_sha256']:
        blockers.append('scope_mismatch')
    if record['scope'].get('kind') != 'JiraActionScope' and record['scope']['author_key'] != binding['author_key']:
        blockers.append('author_scope_mismatch')
    if canonical(evidence) != canonical(binding['evidence']): blockers.append('evidence_mismatch')
    if instant(checked) < instant(binding['issued_at']): blockers.append('not_yet_valid')
    if instant(checked) >= instant(binding['expires_at']): blockers.append('expired')
    return blockers


@guarded
def check_approval(journal, preview, expected_sha256, evidence):
    binding = validated_preview(preview, expected_sha256)
    record = journal.get(binding['operation_id'])
    checked = now()
    blockers = assess_record(record, binding, evidence, checked)
    result = {'schema_version': '1.0.0', 'preview_sha256': expected_sha256, 'scope_sha256': record['sha256'],
              'journal_state': record['state'], 'evidence_sha256': digest(evidence), 'checked_at': checked, 'blockers': blockers}
    return {'kind': 'JiraApprovalAssessment', 'binding': result, 'sha256': digest(result),
            'status': 'blocked' if blockers else 'current', 'evidence_verified': False, **FLAGS}


@guarded
def preflight_plan(journal, preview, expected_sha256):
    binding = validated_preview(preview, expected_sha256)
    record = journal.get(binding['operation_id'])
    if assess_record(record, binding, binding['evidence'], now()):
        raise Rejected('Jira preflight needs current prepared approval scope')
    scope = record['scope']
    target = scope['target']
    if scope.get('kind') == 'JiraActionScope':
        reads = dict(scope['read_plan']['binding']['requests'])
        permission = {'create_issue': 'CREATE_ISSUES', 'transition': 'TRANSITION_ISSUES',
                      'issue_link': 'LINK_ISSUES', 'github_link': 'LINK_ISSUES', 'epic_link': 'EDIT_ISSUES'}[scope['intent']['action']]
    else:
        reads = {'issue': request(scope['issue_observations']['url'])}
        permission = 'ADD_COMMENTS'
    required = ['BROWSE_PROJECTS', permission]
    base = target['base_url']+'/rest/api/2'
    reads['identity'] = request(base+'/myself')
    reads['permissions'] = request(base+'/mypermissions?projectId='+target['project_id']+
                                  ('&issueId='+target['issue_id'] if 'issue_id' in target else '')+'&permissions='+','.join(required))
    result = {'schema_version': '1.0.0', 'approval_sha256': expected_sha256, 'scope_sha256': record['sha256'],
              'required_permissions': required, 'requests': reads}
    return {'kind': 'JiraPreflightReadPlan', 'binding': result, 'sha256': digest(result), **FLAGS}


@guarded
def assess_preflight(journal, preview, expected_sha256, evidence, capture, observed_at):
    observed = instant(observed_at)
    binding = validated_preview(preview, expected_sha256)
    # Hold a consistent snapshot during refresh, then recheck outside that snapshot.
    journal.db.execute('BEGIN')
    try:
        plan = preflight_plan(journal, preview, expected_sha256)
        bodies = responses(plan, capture)
        record = journal.get(binding['operation_id'])
        scope = record['scope']
        checked = now()
        blockers = assess_record(record, binding, evidence, checked)
        if observed < instant(binding['issued_at']): blockers.append('observation_predates_review')
        if observed > instant(checked): blockers.append('observation_from_future')
        if instant(checked) >= observed+timedelta(minutes=5): blockers.append('observation_stale')
        identity = bodies['identity']
        if not isinstance(identity, dict) or identity.get('key') != binding['author_key'] or identity.get('active') is not True:
            blockers.append('author_identity_mismatch')
        permissions = bodies['permissions']
        if not isinstance(permissions, dict) or not isinstance(permissions.get('permissions'), dict):
            raise Rejected('Jira permission response malformed')
        for key in plan['binding']['required_permissions']:
            entry = permissions['permissions'].get(key)
            if not isinstance(entry, dict) or entry.get('key') != key or entry.get('havePermission') is not True:
                blockers.append('permission_missing:'+key)
        try:
            if scope.get('kind') == 'JiraActionScope':
                refresh = {'schema_version': '1.0.0', 'plan_sha256': scope['read_plan']['sha256'],
                           'responses': {key: capture['responses'][key] for key in scope['read_plan']['binding']['requests']}}
                refreshed = preview_action(scope['intent'], scope['profile'], refresh)
            else:
                refreshed = prepare_comment(scope['intent'], scope['target'], capture['responses']['issue'])
            if refreshed['sha256'] != scope['preview_sha256']: blockers.append('remote_context_changed')
        except Rejected:
            blockers.append('remote_context_changed')
        journal.db.execute('COMMIT')
        final_checked = now()
        blockers.extend(assess_record(journal.get(binding['operation_id']), binding, evidence, final_checked))
        if instant(final_checked) >= observed+timedelta(minutes=5): blockers.append('observation_stale')
        result = {'schema_version': '1.0.0', 'approval_sha256': expected_sha256, 'plan_sha256': plan['sha256'],
                  'scope_sha256': record['sha256'], 'capture_sha256': digest(capture), 'evidence_sha256': digest(evidence),
                  'observed_at': observed_at, 'checked_at': final_checked, 'blockers': sorted(set(blockers))}
        report = {'kind': 'JiraPreflightAssessment', 'binding': result, 'sha256': digest(result),
                  'status': 'blocked' if blockers else 'current', 'evidence_verified': False,
                  'remote_identity_verified': False, **FLAGS}
        return report
    except BaseException:
        if journal.db.in_transaction:
            journal.db.execute('ROLLBACK')
        raise


@guarded
def deployment_check(profile):
    from .jira_actions import validate_profile
    validate_profile(profile)
    gates = ['deployed_version_conformance', 'credential_broker_and_revocation', 'trusted_tls_transport',
             'independent_containment_review', 'authenticated_approval_admission', 'workflow_evidence_verification',
             'authoritative_reconciliation_and_restore_policy']
    binding = {'schema_version': '1.0.0', 'profile_sha256': digest(profile), 'codec': profile['codec'],
               'configured_project': profile['project_key'], 'blocking_gates': gates}
    return {'kind': 'JiraDeploymentReadiness', 'binding': binding, 'sha256': digest(binding), 'status': 'blocked', **FLAGS}
