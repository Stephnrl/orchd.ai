"""Register supporting files before test fixtures compute receipt references."""
from orch.contracts import canonical, digest, ref


def register_operations(store, documents):
    for role, stage in (('patch', 'implementation'), ('test', 'tests')):
        receipt = documents[role]
        store.db.execute('INSERT INTO operations(id,task_id,stage,slot,status,result) VALUES(?,?,?,?,?,?)',
                         (receipt['operation_id'], receipt['task_id'], stage, str(documents['work_order']['attempt']),
                          'done', canonical({role: ref(receipt), 'failure': None}).decode()))


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
