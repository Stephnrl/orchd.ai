"""Task-owned local artifact byte checks; no semantic approval or execution authority."""
import hashlib
import json
import stat
from datetime import datetime

from jsonschema import Draft202012Validator

from .contracts import Rejected, SCHEMA, canonical, digest, validate, ref, now
from .broker import validate_wire
from .github_approval import _validate_evidence, check_approval

ARTIFACT = Draft202012Validator({'$defs': SCHEMA['$defs'], '$ref': '#/$defs/ArtifactRef'})
MAX_BYTES = 1024 * 1024
MAX_SUPPORT_REFERENCES = 32
MAX_SUPPORT_BYTES = 8 * 1024 * 1024


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
    if _linked(directory) or not directory.is_dir() or _linked(path) or not path.is_file():
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
    test_request = _stored_record(store, test['request'], task, 'TestRequest')
    work = _stored_record(store, patch['work_order'], task, 'WorkOrder')
    plan_claims = _plan_claims(store, task, work)
    execution_claims = _execution_claims(store, task, work, patch, test)
    review_request = _stored_record(store, review['request'], task, 'ReviewRequest')
    tool = _stored_record(store, policy['request'], task, 'ToolRequest')
    supporting = _check_supporting_artifacts(store, task, patch, test)
    blockers = []
    blockers.extend(plan_claims['blockers'])
    blockers.extend(execution_claims['blockers'])
    if work['repository'] != patch['repository']:
        blockers.append('work_order_repository_mismatch')
    if work['spec'] != review_request['spec'] or work['plan'] != review_request['plan']:
        blockers.append('work_order_review_mismatch')
    if test_request['command'] not in work['permitted_tests']:
        blockers.append('work_order_test_not_permitted')
    if any(item['path'] not in work['allowed_paths'] for item in patch['changed_files']):
        blockers.append('work_order_path_not_permitted')
    if patch['changed_bytes'] > work['max_changed_bytes']:
        blockers.append('work_order_byte_limit_exceeded')
    if (test_request['operation_id'] != test['operation_id'] or test_request['patch'] != ref(patch)
            or test_request['snapshot_sha256'] != test['snapshot_sha256']
            or test_request['work_order'] != patch['work_order']):
        blockers.append('test_request_binding_mismatch')
    if test_request['command'] != test['requested_command']:
        blockers.append('test_request_command_mismatch')
    if test_request['runner_image_digest'] != test['environment']['image_digest']:
        blockers.append('test_runner_image_mismatch')
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
    if not datetime.fromisoformat(work['created_at']) <= datetime.fromisoformat(patch['started_at']):
        blockers.append('work_order_created_after_patch_start')
    if (datetime.fromisoformat(work['deadline']) < datetime.fromisoformat(work['created_at'])
            or any(datetime.fromisoformat(receipt['ended_at']) > datetime.fromisoformat(work['deadline'])
                   for receipt in (patch, test))):
        blockers.append('work_order_deadline_exceeded')
    if datetime.fromisoformat(work['created_at']) > datetime.fromisoformat(checked):
        blockers.append('work_order_from_future')
    if not datetime.fromisoformat(patch['created_at']) <= datetime.fromisoformat(test_request['created_at']) <= datetime.fromisoformat(test['started_at']):
        blockers.append('test_request_timeline_invalid')
    if datetime.fromisoformat(test_request['created_at']) > datetime.fromisoformat(checked):
        blockers.append('test_request_from_future')
    blockers.extend(_timeline_blockers(patch, test, review_request, review, tool, policy, checked))
    if not datetime.fromisoformat(policy['created_at']) <= datetime.fromisoformat(checked) < datetime.fromisoformat(policy['expires_at']):
        blockers.append('policy_not_current')
    return {"records": {role: ref(document) for role, document in documents.items()},
            "execution": execution_claims, "plan_approval": plan_claims, "work_order": ref(work), "test_request": ref(test_request), "review_request": ref(review_request), "tool_request": ref(tool), "checked_at": checked,
            "policy_window": {"issued_at": policy['created_at'], "expires_at": policy['expires_at']},
            "supporting_artifacts": supporting,
            "blockers": blockers}


def _execution_claims(store, task, work, patch, test):
    operations, blockers = {}, []
    for role, stage, receipt in (('patch', 'implementation', patch), ('test', 'tests', test)):
        row = store.db.execute('SELECT stage,slot,status,generation,CASE WHEN length(CAST(result AS BLOB))<=? THEN result END AS result FROM operations WHERE id=? AND task_id=?',
                               (MAX_BYTES, receipt['operation_id'], task)).fetchone()
        if row is None:
            raise Rejected('Missing task-owned execution operation')
        completed = (row['stage'] == stage and row['slot'] == str(work['attempt']) and row['status'] == 'done'
                     and type(row['generation']) is int and row['generation'] >= 1)
        request_sha = None
        envelope_sha = None
        if not completed:
            blockers.append(role + '_operation_not_completed')
        else:
            request_row = store.db.execute('SELECT CASE WHEN length(CAST(request AS BLOB))<=? THEN request END FROM broker_requests WHERE operation_id=? AND generation=?',
                                          (MAX_BYTES, receipt['operation_id'], row['generation'])).fetchone()
            if request_row is None or request_row[0] is None:
                raise Rejected('Missing or oversized broker request evidence')
            try:
                request = json.loads(request_row[0])
                validate_wire(request, 'Request')
                if canonical(request).decode() != request_row[0]:
                    raise Rejected('Noncanonical broker request evidence')
            except (ValueError, TypeError, RecursionError) as exc:
                raise Rejected('Invalid broker request evidence') from exc
            request_sha = digest(request)
            if (request['operation_id'] != receipt['operation_id'] or request['task_id'] != task
                    or request['generation'] != row['generation'] or type(request['generation']) is not int
                    or request['recipe'] != ('edit' if role == 'patch' else 'test')
                    or len(request['args']) != (1 if role == 'patch' else 0)):
                blockers.append(role + '_broker_request_mismatch')
            envelope_sha, envelope_blockers = _broker_envelope(store, task, receipt['operation_id'], row['generation'], request)
            blockers.extend(role + '_' + reason for reason in envelope_blockers)
        result_sha = None
        if row['result'] is not None:
            try:
                result = json.loads(row['result'])
                if not isinstance(result, dict) or canonical(result).decode() != row['result']:
                    raise Rejected('Invalid execution result')
                result_sha = digest(result)
                if result.get(role) != ref(receipt) or 'failure' not in result or result['failure'] is not None:
                    blockers.append(role + '_operation_result_mismatch')
            except (ValueError, TypeError, RecursionError) as exc:
                raise Rejected('Invalid execution result') from exc
        else:
            blockers.append(role + '_operation_result_missing')
        operations[role] = {'operation_id': receipt['operation_id'], 'result_sha256': result_sha,
                            'broker_request_sha256': request_sha, 'broker_envelope_sha256': envelope_sha}
    return {'operations': operations, 'blockers': blockers}


def _broker_envelope(store, task, operation, generation, request):
    row = store.db.execute('SELECT envelope_sha256,CASE WHEN length(CAST(artifact AS BLOB))<=8192 THEN artifact END AS artifact FROM execution_provenance WHERE operation_id=? AND generation=?',
                           (operation, generation)).fetchone()
    if row is None or row['artifact'] is None:
        raise Rejected('Missing or oversized broker envelope metadata')
    try:
        item = json.loads(row['artifact'])
        if canonical(item).decode() != row['artifact']:
            raise Rejected('Noncanonical broker envelope metadata')
        raw = _artifact_bytes(store, item, task)
        envelope = json.loads(raw)
        validate_wire(envelope, 'Envelope')
        if canonical(envelope) != raw or digest(envelope) != row['envelope_sha256']:
            raise Rejected('Broker envelope digest mismatch')
    except (ValueError, TypeError, RecursionError) as exc:
        raise Rejected('Invalid broker envelope evidence') from exc
    blockers = []
    if (any(envelope[key] != request[key] for key in ('schema_version', 'operation_id', 'task_id', 'generation', 'nonce', 'source_digest'))
            or type(envelope['generation']) is not int or envelope['request_sha256'] != digest(request)):
        blockers.append('broker_envelope_binding_mismatch')
    result = envelope['result']
    if result['code'] != 0 or result['failure'] is not None or result['truncated']:
        blockers.append('broker_execution_not_successful')
    if any(len(result[channel].encode()) > 262144 for channel in ('stdout', 'stderr')):
        raise Rejected('Broker envelope output exceeds byte budget')
    return digest(envelope), blockers


def _plan_claims(store, task, work):
    plan = _stored_record(store, work['plan'], task, 'ImplementationPlan')
    spec = _stored_record(store, plan['spec'], task, 'TaskSpec')
    original_request = _artifact_bytes(store, spec['request'], task)
    decision = _stored_record(store, work['plan_approval'], task, 'ApprovalDecision')
    request = _stored_record(store, decision['request'], task, 'ApprovalRequest')
    registration = store.db.execute('SELECT decision_id FROM approvals WHERE request_id=?', (request['id'],)).fetchone()
    registered = registration is not None and registration[0] == decision['id']
    blockers = []
    if not registered:
        blockers.append('plan_approval_not_registered')
    if spec['confirmed_by'] is None:
        blockers.append('spec_not_confirmed')
    if spec['repository'] != plan['repository'] or any(path not in spec['allowed_paths'] for path in plan['allowed_paths']):
        blockers.append('spec_plan_scope_mismatch')
    if datetime.fromisoformat(spec['created_at']) > datetime.fromisoformat(plan['created_at']):
        blockers.append('spec_created_after_plan')
    if (plan['spec'] != work['spec'] or plan['repository'] != work['repository']
            or plan['allowed_paths'] != work['allowed_paths'] or plan['permitted_tests'] != work['permitted_tests']
            or work['max_changed_bytes'] > plan['max_changed_bytes'] or work['attempt'] > plan['max_attempts']):
        blockers.append('plan_work_order_mismatch')
    expected = digest({'subject': ref(plan), 'evidence': [work['spec'], ref(plan)], 'task_id': task,
                       'revision': request['task_revision'], 'policy': request['policy_version']})
    if (request['kind'] != 'plan' or request['subject'] != ref(plan)
            or request['evidence'] != [work['spec'], ref(plan)] or request['subject_sha256'] != expected
            or decision['subject_sha256'] != expected or decision['decision'] != 'approve'):
        blockers.append('plan_approval_binding_mismatch')
    instant = datetime.fromisoformat
    if not (instant(plan['created_at']) <= instant(request['created_at']) <= instant(decision['decided_at'])
            <= instant(decision['created_at']) <= instant(work['created_at']) < instant(request['expires_at'])
            and instant(work['deadline']) <= instant(request['expires_at'])):
        blockers.append('plan_approval_timeline_invalid')
    return {'spec': ref(spec), 'spec_request': {'artifact_id': spec['request']['artifact_id'],
            'sha256': spec['request']['sha256'], 'size_bytes': len(original_request)},
            'plan': ref(plan), 'request': ref(request), 'decision': ref(decision),
            'registered': registered, 'blockers': blockers}


def _timeline_blockers(patch, test, review_request, review, tool, policy, checked):
    instant = datetime.fromisoformat
    blockers = []
    for role, receipt in (('patch', patch), ('test', test)):
        if not instant(receipt['started_at']) <= instant(receipt['ended_at']) <= instant(receipt['created_at']):
            blockers.append(role + '_execution_window_invalid')
    if not instant(patch['created_at']) <= instant(test['started_at']):
        blockers.append('test_predates_patch')
    if not instant(test['created_at']) <= instant(review_request['created_at']) <= instant(review['created_at']):
        blockers.append('review_timeline_invalid')
    if not instant(review['created_at']) <= instant(tool['created_at']) <= instant(policy['created_at']):
        blockers.append('policy_timeline_invalid')
    if any(instant(document['created_at']) > instant(checked)
           for document in (patch, test, review_request, review, tool, policy)):
        blockers.append('evidence_from_future')
    if any(not instant(patch['started_at']) <= instant(command['started_at'])
           <= instant(command['ended_at']) <= instant(patch['ended_at'])
           for command in patch['executed_commands']):
        blockers.append('patch_command_window_invalid')
    return blockers


def _check_supporting_artifacts(store, task, patch, test):
    if 3 + 2 * len(patch['executed_commands']) > MAX_SUPPORT_REFERENCES:
        raise Rejected("Supporting evidence reference budget exceeded")
    references = [('patch.diff', patch['diff']), ('test.stdout', test['stdout']), ('test.stderr', test['stderr'])]
    for index, command in enumerate(patch['executed_commands']):
        references.extend((f'patch.command.{index}.{channel}', command[channel]) for channel in ('stdout', 'stderr'))
    total = 0
    for label, item in references:
        if not ARTIFACT.is_valid(item) or type(item['size_bytes']) is not int or not 0 <= item['size_bytes'] <= MAX_BYTES:
            raise Rejected("Invalid supporting artifact reference")
        total += item['size_bytes']
    if total > MAX_SUPPORT_BYTES:
        raise Rejected("Supporting evidence byte budget exceeded")
    checked = []
    for label, item in references:
        raw = _artifact_bytes(store, item, task)
        checked.append({"role": label, "artifact_id": item['artifact_id'], "sha256": item['sha256'], "size_bytes": len(raw)})
    return checked


def _check_evidence(store, evidence, contents_check=False):
    _validate_evidence(evidence)
    directory = store.root / 'artifacts'
    if _linked(directory) or not directory.is_dir():
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


def _linked(path):
    if path.is_symlink():
        return True
    try:
        # lstat also covers junctions on Python 3.11, before Path.is_junction.
        return bool(getattr(path.lstat(), 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except FileNotFoundError:
        return False
