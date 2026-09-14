"""Register supporting files before test fixtures compute receipt references."""
from orch.contracts import canonical, digest, ref
from orch.execution import Executor
from orch.fixtures import EDIT_CODE
import hashlib
from pathlib import PurePosixPath


def register_operations(store, documents):
    for role, stage in (('patch', 'implementation'), ('test', 'tests')):
        receipt = documents[role]
        command = receipt if role == 'test' else (receipt['executed_commands'] or [documents['test']])[0]
        result = {role: ref(receipt), 'failure': None}
        if role == 'patch':
            result['snapshot'] = store.artifact(receipt['task_id'], 'hello world\n', media_type='text/plain')
        store.db.execute('INSERT INTO operations(id,task_id,stage,slot,status,result) VALUES(?,?,?,?,?,?)',
                         (receipt['operation_id'], receipt['task_id'], stage, str(documents['work_order']['attempt']),
                          'done', canonical(result).decode()))
        request = dict(schema_version='1.0.0', operation_id=receipt['operation_id'], task_id=receipt['task_id'],
                       generation=1, nonce='e' * 32, workspace=command['working_directory'],
                       recipe='edit' if role == 'patch' else 'test', args=['hello world\n'] if role == 'patch' else [],
                       mode='trusted-fixture', image=None, source_digest='a' * 64)
        store.db.execute('INSERT INTO broker_requests VALUES(?,?,?)',
                         (receipt['operation_id'], 1, canonical(request).decode()))
        envelope = {key: request[key] for key in ('schema_version', 'operation_id', 'task_id', 'generation', 'nonce', 'source_digest')}
        envelope.update(request_sha256=digest(request), result=dict(command=command.get('executed_argv', command.get('argv')),
                        started=command['started_at'], ended=command['ended_at'], code=command['exit_code'],
                        stdout=store.read_artifact(command['stdout'], receipt['task_id'])[:262144],
                        stderr=store.read_artifact(command['stderr'], receipt['task_id'])[:262144],
                        truncated=command.get('output_truncated', False), failure=None))
        artifact = store.artifact(receipt['task_id'], envelope)
        store.db.execute('INSERT INTO execution_provenance VALUES(?,?,?,?)',
                         (receipt['operation_id'], 1, digest(envelope), canonical(artifact).decode()))


def link_plan_evidence(documents):
    work, plan = documents['work_order'], documents['plan']
    request, decision = documents['plan_request'], documents['plan_decision']
    work['spec'] = plan['spec'] = documents['review_request']['spec'] = ref(documents['spec'])
    request.update(subject=ref(plan), evidence=[work['spec'], ref(plan)])
    request['subject_sha256'] = digest({'subject': request['subject'], 'evidence': request['evidence'],
                                      'task_id': work['task_id'], 'revision': request['task_revision'], 'policy': request['policy_version']})
    decision.update(request=ref(request), subject_sha256=request['subject_sha256'])
    work.update(plan=ref(plan), plan_approval=ref(decision))
    documents['review_request']['plan'] = ref(plan)


def register_support(store, task, documents):
    snapshot = store.artifact(task, 'hello world\n', media_type='text/plain')
    for role in ('spec', 'plan', 'work_order'):
        documents[role]['allowed_paths'] = ['greeting.txt']
    for role in ('plan', 'work_order'):
        documents[role]['max_changed_bytes'] = snapshot['size_bytes']
    documents['patch']['changed_bytes'] = snapshot['size_bytes']
    documents['patch']['changed_files'] = [dict(path='greeting.txt', change='modify',
        before_sha256=hashlib.sha256(b'hello\n').hexdigest(), after_sha256=snapshot['sha256'])]
    for role in ('patch', 'test', 'test_request'):
        documents[role]['snapshot_sha256'] = snapshot['sha256']
    documents['spec']['request'] = store.artifact(task, 'original task request', media_type='text/plain')
    documents['patch']['diff'] = store.artifact(task, '--- greeting.txt\n+++ greeting.txt\n@@ -1 +1 @@\n-hello\n+hello world\n', media_type='text/x-diff')
    for channel in ('stdout', 'stderr'):
        documents['test'][channel] = store.artifact(task, channel, media_type='text/plain')
    if not documents['patch']['executed_commands']:
        test = documents['test']
        argv = Executor('trusted-fixture').command_for(PurePosixPath('.'), EDIT_CODE, ['hello world\n'],
            documents['patch']['operation_id'], python_executable='historical-python')
        documents['patch']['executed_commands'] = [dict(argv=argv, working_directory='.',
            started_at=documents['patch']['started_at'], ended_at=documents['patch']['ended_at'], exit_code=0,
            stdout=test['stdout'], stderr=test['stderr'])]
    for command in documents['patch']['executed_commands']:
        for channel in ('stdout', 'stderr'):
            command[channel] = store.artifact(task, 'patch ' + channel, media_type='text/plain')
