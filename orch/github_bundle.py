"""Portable, bounded local evidence; never a workflow restore or dispatch input."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from contextlib import contextmanager

from .contracts import Rejected, canonical, digest, validate
from .github_evidence import (
    ARTIFACT, MAX_BYTES, RECORD_ROLES, _artifact_bytes, _linked,
    _record_claims_snapshot, _stored_record,
)
from .storage import Store

MAX_BUNDLE_BYTES = 16 * 1024 * 1024
MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_ARTIFACTS = 40


def _collect(store, selection, *, require_consistent=False):
    """Caller holds one SQLite read snapshot; artifact bytes are verified as read."""
    assessment = _record_claims_snapshot(store, selection)
    if require_consistent and assessment['status'] != 'claims_consistent':
        raise Rejected('Cannot export blocked evidence claims')
    claims, task = assessment['binding']['claims'], selection['task_id']
    references = [(selection[role], kind) for role, kind in RECORD_ROLES.items()]
    references += [(claims[key], kind) for key, kind in (
        ('test_request', 'TestRequest'), ('work_order', 'WorkOrder'),
        ('review_request', 'ReviewRequest'), ('tool_request', 'ToolRequest'))]
    references += [(claims['plan_approval'][key], kind) for key, kind in (
        ('spec', 'TaskSpec'), ('plan', 'ImplementationPlan'),
        ('request', 'ApprovalRequest'), ('decision', 'ApprovalDecision'))]
    records = sorted([_stored_record(store, reference, task, kind) for reference, kind in references], key=lambda item: item['id'])
    if len({item['id'] for item in records}) != len(records):
        raise Rejected('Duplicate bundle records')
    if sum(len(canonical(item)) for item in records) > MAX_RECORD_BYTES:
        raise Rejected('Bundle record byte budget exceeded')
    operations, requests, provenance = [], [], []
    for role in ('patch', 'test'):
        operation = claims['execution']['operations'][role]['operation_id']
        row = store.db.execute('SELECT id,task_id,stage,slot,status,generation,result FROM operations WHERE id=? AND task_id=?', (operation, task)).fetchone()
        entry = dict(row)
        entry['result'] = json.loads(entry['result'])
        operations.append(entry)
        generation = entry['generation']
        row = store.db.execute('SELECT request FROM broker_requests WHERE operation_id=? AND generation=?', (operation, generation)).fetchone()
        if row is None:
            raise Rejected('Missing bundle broker request')
        requests.append({'operation_id': operation, 'generation': generation, 'request': json.loads(row[0])})
        row = store.db.execute('SELECT envelope_sha256,artifact FROM execution_provenance WHERE operation_id=? AND generation=?', (operation, generation)).fetchone()
        if row is None:
            raise Rejected('Missing bundle provenance')
        provenance.append({'operation_id': operation, 'generation': generation, 'envelope_sha256': row[0], 'artifact': json.loads(row[1])})
    artifacts = {}

    def visit(value):
        if isinstance(value, dict):
            if 'artifact_id' in value:
                if not ARTIFACT.is_valid(value):
                    raise Rejected('Invalid bundle artifact reference')
                previous = artifacts.setdefault(value['artifact_id'], value)
                if previous != value:
                    raise Rejected('Conflicting bundle artifact references')
            else:
                for child in value.values():
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(records)
    visit(operations)
    visit(provenance)
    if len(artifacts) > MAX_ARTIFACTS or sum(item['size_bytes'] for item in artifacts.values()) > MAX_ARTIFACT_BYTES:
        raise Rejected('Bundle artifact budget exceeded')
    files = [{'reference': item, 'base64': base64.b64encode(_artifact_bytes(store, item, task)).decode('ascii')}
             for _, item in sorted(artifacts.items())]
    plan = claims['plan_approval']
    approval = {'request_id': plan['request']['id'], 'decision_id': plan['decision']['id']}
    if not plan['registered']:
        raise Rejected('Bundle requires registered plan approval evidence')
    payload = {'selection': selection, 'records': records, 'artifacts': files,
               'operations': sorted(operations, key=lambda item: item['id']),
               'broker_requests': sorted(requests, key=lambda item: item['operation_id']),
               'provenance': sorted(provenance, key=lambda item: item['operation_id']), 'approval': approval}
    return payload, assessment


def _report(payload, assessment, file_sha):
    binding = {'schema_version': '1.0.0', 'bundle_sha256': file_sha,
               'selection': payload['selection'], 'record_count': len(payload['records']),
               'artifact_count': len(payload['artifacts']),
               'artifact_bytes': sum(item['reference']['size_bytes'] for item in payload['artifacts']),
               'assessment': assessment}
    return {'kind': 'GitHubEvidenceBundleAssessment', 'binding': binding, 'sha256': digest(binding),
            'status': assessment['status'], 'evidence_verified': False,
            'live_authorized': False, 'retry_allowed': False}


def _regular_path(path):
    if any(_linked(part) for part in (path, *path.parents)):
        raise Rejected('Bundle path must not contain links')


def _publish(destination, raw):
    """Publish a fully flushed sibling with exclusive hard-link creation, never replace."""
    path = Path(destination).absolute()
    _regular_path(path)
    if not path.parent.is_dir() or path.exists():
        raise Rejected('Bundle destination must be new with an existing parent')
    fd, temporary = tempfile.mkstemp(prefix='.orch-evidence-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _regular_path(path)
        os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def export_bundle(store, selection, destination):
    path = Path(destination).absolute()
    _regular_path(path)
    if path.resolve().is_relative_to(store.root):
        raise Rejected('Bundle destination must be outside workflow storage')
    store.db.execute('BEGIN')
    try:
        payload, assessment = _collect(store, selection, require_consistent=True)
        raw = canonical({'kind': 'GitHubEvidenceBundle', 'schema_version': '1.0.0', 'payload': payload, 'sha256': digest(payload)})
        if len(raw) > MAX_BUNDLE_BYTES:
            raise Rejected('Bundle byte budget exceeded')
        store.db.execute('COMMIT')
    except BaseException:
        store.db.execute('ROLLBACK')
        raise
    _publish(path, raw)
    return _report(payload, assessment, hashlib.sha256(raw).hexdigest())


def _keys(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise Rejected('Invalid bundle structure')


def _load(path, expected_sha256):
    if not isinstance(expected_sha256, str) or not re.fullmatch('[a-f0-9]{64}', expected_sha256):
        raise Rejected('Expected bundle digest required')
    path = Path(path).absolute()
    _regular_path(path)
    if not path.is_file():
        raise Rejected('Bundle must be a regular file')
    with path.open('rb') as stream:
        raw = stream.read(MAX_BUNDLE_BYTES + 1)
    if len(raw) > MAX_BUNDLE_BYTES or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise Rejected('Bundle size or expected digest mismatch')
    bundle = json.loads(raw)
    _keys(bundle, ('kind', 'schema_version', 'payload', 'sha256'))
    if (bundle['kind'] != 'GitHubEvidenceBundle' or bundle['schema_version'] != '1.0.0'
            or canonical(bundle) != raw or digest(bundle['payload']) != bundle['sha256']):
        raise Rejected('Invalid bundle encoding or digest')
    payload = bundle['payload']
    _keys(payload, ('selection', 'records', 'artifacts', 'operations', 'broker_requests', 'provenance', 'approval'))
    for key, minimum, maximum in (('records', 12, 12), ('artifacts', 1, MAX_ARTIFACTS),
                                   ('operations', 2, 2), ('broker_requests', 2, 2), ('provenance', 2, 2)):
        if not isinstance(payload[key], list) or not minimum <= len(payload[key]) <= maximum:
            raise Rejected('Bundle collection budget exceeded')
    if sum(len(canonical(item)) for item in payload['records']) > MAX_RECORD_BYTES:
        raise Rejected('Bundle record byte budget exceeded')
    return payload


def _materialize(store, payload):
    selection = payload['selection']
    _keys(selection, ('task_id', *RECORD_ROLES))
    task = selection['task_id']
    if not isinstance(task, str) or not re.fullmatch('[a-f0-9]{32}', task):
        raise Rejected('Invalid bundle task')
    store.db.execute('INSERT INTO tasks VALUES(?,?,?)', (task, '{}', '{}'))
    for document in payload['records']:
        validate(document)
        if document['task_id'] != task or len(canonical(document)) > MAX_BYTES:
            raise Rejected('Invalid bundle record ownership or size')
        store.put(document)
    total = 0
    for artifact in payload['artifacts']:
        _keys(artifact, ('reference', 'base64'))
        item = artifact['reference']
        if not ARTIFACT.is_valid(item) or type(item['size_bytes']) is not int or not 0 <= item['size_bytes'] <= MAX_BYTES:
            raise Rejected('Invalid bundle artifact')
        total += item['size_bytes']
        if total > MAX_ARTIFACT_BYTES or not isinstance(artifact['base64'], str) or len(artifact['base64']) > 4 * ((MAX_BYTES + 2) // 3):
            raise Rejected('Bundle artifact byte budget exceeded')
        raw = base64.b64decode(artifact['base64'], validate=True)
        if base64.b64encode(raw).decode('ascii') != artifact['base64'] or len(raw) != item['size_bytes'] or hashlib.sha256(raw).hexdigest() != item['sha256']:
            raise Rejected('Bundle artifact byte mismatch')
        store.db.execute('INSERT INTO artifacts VALUES(?,?,?)', (item['artifact_id'], task, canonical(item).decode()))
        (store.root / 'artifacts' / item['sha256']).write_bytes(raw)
    for operation in payload['operations']:
        _keys(operation, ('id', 'task_id', 'stage', 'slot', 'status', 'generation', 'result'))
        if (operation['task_id'] != task or type(operation['generation']) is not int or operation['generation'] < 1
                or any(not isinstance(operation[key], str) for key in ('id', 'stage', 'slot', 'status'))
                or len(canonical(operation['result'])) > MAX_BYTES):
            raise Rejected('Invalid bundle operation')
        store.db.execute('INSERT INTO operations(id,task_id,stage,slot,status,generation,result) VALUES(?,?,?,?,?,?,?)',
                         (*[operation[key] for key in ('id', 'task_id', 'stage', 'slot', 'status', 'generation')], canonical(operation['result']).decode()))
    for request in payload['broker_requests']:
        _keys(request, ('operation_id', 'generation', 'request'))
        store.db.execute('INSERT INTO broker_requests VALUES(?,?,?)',
                         (request['operation_id'], request['generation'], canonical(request['request']).decode()))
    for entry in payload['provenance']:
        _keys(entry, ('operation_id', 'generation', 'envelope_sha256', 'artifact'))
        store.db.execute('INSERT INTO execution_provenance VALUES(?,?,?,?)',
                         (entry['operation_id'], entry['generation'], entry['envelope_sha256'], canonical(entry['artifact']).decode()))
    approval = payload['approval']
    _keys(approval, ('request_id', 'decision_id'))
    store.db.execute('INSERT INTO approvals VALUES(?,?)', (approval['request_id'], approval['decision_id']))


@contextmanager
def _verified_bundle(source, expected_sha256):
    """Keep verified, query-only scratch storage alive for composed local checks."""
    try:
        payload = _load(source, expected_sha256)
        with tempfile.TemporaryDirectory(prefix='orch-evidence-verify-') as root:
            store = Store(root)
            try:
                with store.transaction():
                    _materialize(store, payload)
                store.db.execute('PRAGMA query_only=ON')
                store.db.execute('BEGIN')
                collected, assessment = _collect(store, payload['selection'])
                if canonical(collected) != canonical(payload):
                    raise Rejected('Bundle includes missing, extra or inconsistent dependencies')
                store.db.execute('COMMIT')
                yield store, payload, _report(payload, assessment, expected_sha256)
            finally:
                store.close()
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError, sqlite3.Error) as exc:
        raise Rejected('Invalid evidence bundle') from exc


def verify_bundle(source, expected_sha256):
    """Reassess untrusted bundle claims in disposable storage; never adopt the database."""
    with _verified_bundle(source, expected_sha256) as (_, _, report):
        return report
