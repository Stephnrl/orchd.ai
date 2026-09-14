"""Operator-started, serial fixture batches. Never approve or replay uncertain work."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import re

from .broker import atomic_json, file_lock
from .contracts import Rejected, canonical, digest, now, uid
from .engine import PAUSED
from .github_preview import load_intent
from .maintenance import plain
from .readiness import code_digest

MAX_TASKS = 16
MAX_BATCHES = 1024
REVIEW_STATES = {'FAILED', 'BLOCKED'}


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{32}', value):
        raise Rejected('Invalid batch/task identifier')
    return value


def snapshot(engine, task):
    value = engine.task(task)
    return value, digest(value)


class Batches:
    def __init__(self, engine):
        self.engine = engine
        self.root = plain(engine.store.root / 'batches')

    def _directory(self):
        plain(self.root)
        self.root.mkdir(exist_ok=True)
        return self.root

    def _path(self, batch_id):
        return plain(self.root / (identifier(batch_id) + '.json'))

    def _scope(self):
        return {'store_sha256': digest(str(self.engine.store.root)), 'source_sha256': code_digest(),
                'worker_mode': self.engine.executor.mode, 'image': self.engine.executor.image,
                'provider': getattr(self.engine.provider_factory, 'provider_id', 'mock')}

    def _load(self, batch_id):
        path = self._path(batch_id)
        value = load_intent(path)
        try:
            if (set(value) != {'kind', 'id', 'scope', 'scope_sha256', 'revision', 'entries'}
                    or value['kind'] != 'SerialWorkflowBatch' or value['id'] != batch_id
                    or digest(value['scope']) != value['scope_sha256']
                    or type(value['revision']) is not int or value['revision'] < 0):
                raise Rejected('Invalid batch journal')
            scope = value['scope']
            if set(scope) != {'binding', 'created_at', 'expires_at', 'tasks'} or not 1 <= len(scope['tasks']) <= MAX_TASKS:
                raise Rejected('Invalid batch scope')
            binding = scope['binding']
            if (set(binding) != {'store_sha256', 'source_sha256', 'worker_mode', 'image', 'provider'}
                    or any(not re.fullmatch('[a-f0-9]{64}', binding[k]) for k in ('store_sha256', 'source_sha256'))
                    or binding['worker_mode'] not in ('trusted-fixture', 'docker')
                    or binding['provider'] not in ('mock', 'fixture_cli')):
                raise Rejected('Invalid batch execution binding')
            if binding['image'] is not None and (not isinstance(binding['image'], str) or len(binding['image']) > 256 or not re.fullmatch(r'[a-zA-Z0-9./_-]+@sha256:[a-f0-9]{64}', binding['image'])):
                raise Rejected('Invalid batch image binding')
            for key in ('created_at', 'expires_at'):
                if not isinstance(scope[key], str) or not scope[key].endswith('Z'):
                    raise Rejected('Invalid batch timestamp')
                datetime.fromisoformat(scope[key])
            if len(value['entries']) != len(scope['tasks']): raise Rejected('Invalid batch entries')
            seen = set()
            for item, entry in zip(scope['tasks'], value['entries']):
                identifier(item['task_id'])
                if (set(item) != {'task_id', 'revision', 'snapshot_sha256'} or item['task_id'] in seen
                        or type(item['revision']) is not int or item['revision'] < 0
                        or not re.fullmatch('[a-f0-9]{64}', item['snapshot_sha256'])):
                    raise Rejected('Invalid batch task')
                seen.add(item['task_id'])
                if (set(entry) != {'status', 'last_snapshot_sha256', 'result', 'steps'}
                        or entry['status'] not in {'pending', 'running', 'stopped', 'abandoned'}
                        or type(entry['steps']) is not int or not 0 <= entry['steps'] <= 40
                        or not re.fullmatch('[a-f0-9]{64}', entry['last_snapshot_sha256'])):
                    raise Rejected('Invalid batch progress')
                if entry['status'] == 'pending' and (entry['steps'] or entry['result'] is not None or entry['last_snapshot_sha256'] != item['snapshot_sha256']):
                    raise Rejected('Changed pending batch entry')
                if entry['status'] == 'stopped' and entry['result'] not in PAUSED:
                    raise Rejected('Invalid batch stopping state')
                if entry['status'] != 'stopped' and entry['result'] is not None:
                    raise Rejected('Unexpected batch result')
            return value
        except (KeyError, TypeError, ValueError) as exc:
            raise Rejected('Invalid batch journal') from exc

    def _save(self, value):
        value['revision'] += 1
        atomic_json(self._path(value['id']), value)

    def _expected(self, value, sha, revision):
        if (not isinstance(sha, str) or sha != value['scope_sha256']
                or type(revision) is not int or revision != value['revision']):
            raise Rejected('Batch scope or revision changed')

    def _current(self, scope):
        try:
            created, expiry = (datetime.fromisoformat(scope[k]) for k in ('created_at', 'expires_at'))
            if (scope['binding'] != self._scope() or not created <= datetime.now(timezone.utc) < expiry
                    or expiry-created != timedelta(minutes=30)):
                raise Rejected('Batch source, store, runtime or review window changed')
        except (TypeError, ValueError) as exc:
            raise Rejected('Batch scope is not current') from exc

    def create(self, requests):
        if not isinstance(requests, dict) or set(requests) != {'tasks'} or not isinstance(requests['tasks'], list) or not 1 <= len(requests['tasks']) <= MAX_TASKS:
            raise Rejected('Supply one to sixteen explicit tasks')
        self._directory()
        with file_lock(plain(self.root / 'dispatch.lock')), self.engine.store.exclusive():
            if len(list(self.root.glob('*.json'))) >= MAX_BATCHES: raise Rejected('Batch retention capacity reached')
            tasks, seen = [], set()
            for request in requests['tasks']:
                if not isinstance(request, dict) or set(request) != {'task_id', 'expected_revision'}:
                    raise Rejected('Invalid batch request')
                task = identifier(request['task_id'])
                value, sha = snapshot(self.engine, task)
                state = value['state']
                if (task in seen or type(request['expected_revision']) is not int
                        or request['expected_revision'] != state['revision'] or state['state'] in PAUSED
                        or any(state[k] for k in ('active_operation_id', 'active_invocation_id', 'container_id'))):
                    raise Rejected('Task is stale, paused, duplicated or requires recovery')
                seen.add(task)
                tasks.append({'task_id': task, 'revision': state['revision'], 'snapshot_sha256': sha})
            created = now()
            scope = {'binding': self._scope(), 'created_at': created,
                     'expires_at': (datetime.fromisoformat(created)+timedelta(minutes=30)).isoformat().replace('+00:00', 'Z'), 'tasks': tasks}
            value = {'kind': 'SerialWorkflowBatch', 'id': uid(), 'scope': scope, 'scope_sha256': digest(scope),
                     'revision': 0, 'entries': [{'status':'pending', 'last_snapshot_sha256': t['snapshot_sha256'], 'result':None, 'steps':0} for t in tasks]}
            with self._path(value['id']).open('xb') as stream:
                stream.write(canonical(value))
                stream.flush()
                os.fsync(stream.fileno())
            return value

    def inspect(self, batch_id):
        # No directory creation or journal mutation during inspection.
        value = self._load(batch_id)
        observations = []
        for task, entry in zip(value['scope']['tasks'], value['entries']):
            try:
                current, sha = snapshot(self.engine, task['task_id'])
                observations.append({'task_id': task['task_id'], 'state': current['state']['state'],
                                     'revision': current['state']['revision'], 'snapshot_sha256': sha,
                                     'matches_checkpoint': sha == entry['last_snapshot_sha256']})
            except (Rejected, KeyError):
                observations.append({'task_id': task['task_id'], 'unavailable': True})
        return {'kind':'SerialBatchInspection', 'batch':value, 'observations':observations,
                'execution_authorized':False, 'automatic_retry':False}

    def list(self, after=None):
        if after is not None: identifier(after)
        plain(self.root)
        ids = sorted(identifier(path.stem) for path in self.root.glob('*.json') if after is None or path.stem > after)
        items = []
        for batch_id in ids[:50]:
            value = self._load(batch_id)
            counts = {status:sum(e['status'] == status for e in value['entries']) for status in ('pending','running','stopped','abandoned')}
            items.append({'id':batch_id, 'revision':value['revision'], 'scope_sha256':value['scope_sha256'],
                          'created_at':value['scope']['created_at'], 'expires_at':value['scope']['expires_at'], 'counts':counts})
        return {'kind':'SerialBatchList', 'items':items, 'next':ids[49] if len(ids)>50 else None}

    def run(self, batch_id, sha, revision):
        with file_lock(plain(self.root / 'dispatch.lock')):
            value = self._load(batch_id)
            self._expected(value, sha, revision)
            scope = value['scope']
            self._current(scope)
            if any(e['status'] == 'running' or (e['status'] == 'stopped' and e['result'] in REVIEW_STATES) for e in value['entries']):
                raise Rejected('Interrupted or failed entries require explicit review')
            for task, entry in zip(scope['tasks'], value['entries']):
                if entry['status'] != 'pending': continue
                entry['status'] = 'running'
                self._save(value)  # Durable reservation before the first possible workflow effect.
                try:
                    for _ in range(40):
                        self._current(scope)
                        current = self.engine.advance(task['task_id'], expected_snapshot=entry['last_snapshot_sha256'])
                        entry['steps'] += 1
                        entry['last_snapshot_sha256'] = digest(current)
                        if current['state']['state'] in PAUSED:
                            entry.update(status='stopped', result=current['state']['state'])
                        self._save(value)
                        if entry['status'] == 'stopped': break
                    if entry['status'] == 'running' or entry['result'] in REVIEW_STATES: break
                except (Rejected, OSError, KeyError):
                    # A rejection may happen after durable effects. Never infer safe retry.
                    return self._load(batch_id)
            return value

    def abandon(self, batch_id, sha, revision, task_id, expected_snapshot):
        identifier(task_id)
        with file_lock(plain(self.root / 'dispatch.lock')), self.engine.store.exclusive():
            value = self._load(batch_id)
            self._expected(value, sha, revision)
            matches = [i for i, task in enumerate(value['scope']['tasks']) if task['task_id'] == task_id]
            if len(matches) != 1: raise Rejected('Task not in this batch')
            entry = value['entries'][matches[0]]
            if entry['status'] == 'abandoned' or (entry['status'] == 'stopped' and entry['result'] not in REVIEW_STATES):
                raise Rejected('Finished entry cannot be rewritten')
            current, current_sha = snapshot(self.engine, task_id)
            if (expected_snapshot != current_sha or any(current['state'][k] for k in ('active_operation_id', 'active_invocation_id', 'container_id'))):
                raise Rejected('Review current task and resolve active work before abandoning')
            entry.update(status='abandoned', result=None, last_snapshot_sha256=current_sha)
            self._save(value)
            return value
