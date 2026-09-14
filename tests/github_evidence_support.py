"""Register supporting files before test fixtures compute receipt references."""
from orch.contracts import canonical, digest, ref


def register_operations(store, documents):
    for role, stage in (('patch', 'implementation'), ('test', 'tests')):
        receipt = documents[role]
        store.db.execute('INSERT INTO operations(id,task_id,stage,slot,status,result) VALUES(?,?,?,?,?,?)',
                         (receipt['operation_id'], receipt['task_id'], stage, str(documents['work_order']['attempt']),
                          'done', canonical({role: ref(receipt), 'failure': None}).decode()))
        request = dict(schema_version='1.0.0', operation_id=receipt['operation_id'], task_id=receipt['task_id'],
                       generation=1, nonce='e' * 32, workspace='private-fixture-workspace',
                       recipe='edit' if role == 'patch' else 'test', args=['hello world\n'] if role == 'patch' else [],
                       mode='trusted-fixture', image=None, source_digest='a' * 64)
        store.db.execute('INSERT INTO broker_requests VALUES(?,?,?)',
                         (receipt['operation_id'], 1, canonical(request).decode()))
        envelope = {key: request[key] for key in ('schema_version', 'operation_id', 'task_id', 'generation', 'nonce', 'source_digest')}
        envelope.update(request_sha256=digest(request), result=dict(command=['fixture'], started=receipt['started_at'],
                        ended=receipt['ended_at'], code=0, stdout='stdout', stderr='stderr', truncated=False, failure=None))
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
    documents['spec']['request'] = store.artifact(task, 'original task request', media_type='text/plain')
    documents['patch']['diff'] = store.artifact(task, 'fixture diff', media_type='text/plain')
    for channel in ('stdout', 'stderr'):
        documents['test'][channel] = store.artifact(task, channel, media_type='text/plain')
    for command in documents['patch']['executed_commands']:
        for channel in ('stdout', 'stderr'):
            command[channel] = store.artifact(task, 'patch ' + channel, media_type='text/plain')
