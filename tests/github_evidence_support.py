"""Register supporting files before test fixtures compute receipt references."""
from orch.contracts import digest, ref


def link_plan_evidence(documents):
    work, plan = documents['work_order'], documents['plan']
    request, decision = documents['plan_request'], documents['plan_decision']
    request.update(subject=ref(plan), evidence=[work['spec'], ref(plan)])
    request['subject_sha256'] = digest({'subject': request['subject'], 'evidence': request['evidence'],
                                      'task_id': work['task_id'], 'revision': request['task_revision'], 'policy': request['policy_version']})
    decision.update(request=ref(request), subject_sha256=request['subject_sha256'])
    work.update(plan=ref(plan), plan_approval=ref(decision))
    documents['review_request']['plan'] = ref(plan)


def register_support(store, task, documents):
    documents['patch']['diff'] = store.artifact(task, 'fixture diff', media_type='text/plain')
    for channel in ('stdout', 'stderr'):
        documents['test'][channel] = store.artifact(task, channel, media_type='text/plain')
    for command in documents['patch']['executed_commands']:
        for channel in ('stdout', 'stderr'):
            command[channel] = store.artifact(task, 'patch ' + channel, media_type='text/plain')
