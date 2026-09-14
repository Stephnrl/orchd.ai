"""Register supporting files before test fixtures compute receipt references."""


def register_support(store, task, documents):
    documents['patch']['diff'] = store.artifact(task, 'fixture diff', media_type='text/plain')
    for channel in ('stdout', 'stderr'):
        documents['test'][channel] = store.artifact(task, channel, media_type='text/plain')
    for command in documents['patch']['executed_commands']:
        for channel in ('stdout', 'stderr'):
            command[channel] = store.artifact(task, 'patch ' + channel, media_type='text/plain')
