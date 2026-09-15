"""Task-kernel admission for the bounded pilot; no provider or remote action authority."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

from .contracts import Rejected, canonical, digest, error, make, now, redact, ref, uid
from .pilot import Pilot, workspace_files
from .storage import Store

WORKLOAD = 'repository-json-v1'
EDGES = {'DRAFT_SPEC': {'SPEC_READY'}, 'SPEC_READY': {'AWAITING_PLAN_APPROVAL'},
         'AWAITING_PLAN_APPROVAL': {'READY_FOR_IMPLEMENTATION', 'CANCELLED'},
         'READY_FOR_IMPLEMENTATION': {'IMPLEMENTING', 'CANCELLED'},
         'IMPLEMENTING': {'AWAITING_ACTION_APPROVAL', 'FAILED', 'BLOCKED'},
         'AWAITING_ACTION_APPROVAL': {'COMPLETED', 'CANCELLED'}, 'BLOCKED': {'FAILED'}}


class RepositoryTasks:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def _pilot(self, read_only=False):
        root = self.store.root / 'repository-pilots'
        if read_only and not (root / 'pilot.sqlite').is_file(): raise Rejected('Repository pilot journal is missing')
        mode = 'local-data' if self.engine.executor.mode == 'trusted-fixture' else 'docker'
        return Pilot(root, read_only=read_only, executor=mode, image=self.engine.executor.image)

    def _scope(self, state, context):
        if context.get('workload') != WORKLOAD: raise Rejected('Wrong task workload')
        plan = self.store.get(state['plan'], state['task_id'], 'RepositoryTaskPlan')
        scope = json.loads(self.store.read_artifact(plan['scope'], state['task_id']))
        if (plan['spec'] != state['spec'] or digest(scope) != plan['scope_sha256'] or scope['id'] != plan['pilot_id']
                or scope.get('task_binding') != {'store': str(self.store.root), 'task_id': state['task_id']}
                or scope['journal_root'] != str(self.store.root / 'repository-pilots')):
            raise Rejected('Repository task scope mismatch or restored store path')
        return plan, scope

    def _artifact(self, task, value, media='application/json'):
        raw = value if isinstance(value, str) else canonical(value).decode()
        item = self.store.artifact(task, raw, media, trust='trusted_receipt')
        if item['redacted'] or item['sha256'] != hashlib.sha256(raw.encode()).hexdigest():
            raise Rejected('Repository evidence cannot be altered by redaction')
        return item

    def _move(self, state, context, target):
        before = state['state']
        if target not in EDGES.get(before, set()): raise Rejected('Forbidden repository task transition')
        state['state'], state['revision'] = target, state['revision']+1
        if target == 'BLOCKED': state['resume_state'] = before
        self.engine._event(state, context, 'state_transition', before=before)

    def _request(self, state, context, kind):
        subject = state['plan'] if kind == 'plan' else context['repository_result']
        evidence = [state['spec'], state['plan']] + ([subject] if kind == 'action' else [])
        _, scope = self._scope(state, context)
        expiry = scope['expires_at'] if kind == 'plan' else (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
        expiry = datetime.fromisoformat(expiry).isoformat().replace('+00:00', 'Z')
        request = make('ApprovalRequest', state['task_id'], kind=kind, subject=subject,
                       subject_sha256=digest({'subject': subject, 'evidence': evidence, 'task_id': state['task_id'],
                                              'revision': state['revision']+1, 'policy': WORKLOAD}),
                       task_revision=state['revision']+1, policy_version=WORKLOAD, evidence=evidence, expires_at=expiry,
                       summary='Approve exact repository pilot execution' if kind == 'plan' else 'Accept validated local snapshot; no remote action')
        state['pending_approval'] = self.store.put(request)
        context[kind+'_approval_request'] = ref(request)
        self.engine._event(state, context, 'approval_requested', {'request': ref(request)})

    def _approved(self, state, context):
        plan, scope = self._scope(state, context)
        request = self.store.get(context['plan_approval_request'], state['task_id'], 'ApprovalRequest')
        decision = self.store.get(context['plan_approval'], state['task_id'], 'ApprovalDecision')
        row = self.store.db.execute('SELECT decision_id FROM approvals WHERE request_id=?', (request['id'],)).fetchone()
        evidence = [state['spec'], state['plan']]
        expected = digest({'subject': state['plan'], 'evidence': evidence, 'task_id': state['task_id'],
                           'revision': request['task_revision'], 'policy': WORKLOAD})
        if (not row or row[0] != decision['id'] or decision['decision'] != 'approve'
                or decision['request'] != ref(request) or decision['subject_sha256'] != expected
                or request['subject_sha256'] != expected or request['subject'] != state['plan']
                or request['evidence'] != evidence or request['policy_version'] != WORKLOAD or request['expires_at'] <= now()):
            raise Rejected('Repository execution approval missing, expired or changed')
        return plan, scope

    def create(self, intent, title='Repository JSON validation', principal='local-operator'):
        if not isinstance(title, str) or not 1 <= len(title) <= 200 or redact(title) != title or redact(canonical(intent).decode()) != canonical(intent).decode():
            raise Rejected('Invalid or secret-bearing repository task')
        task = uid()
        with self.store.exclusive():
            pilot = self._pilot()
            try: prepared = pilot.prepare(intent, task_binding={'store': str(self.store.root), 'task_id': task})
            finally: pilot.close()
            # A crash before this transaction leaves an unadmitted pilot. Its task
            # binding cannot pass admission without a committed kernel operation.
            with self.store.transaction():
                state = make('TaskState', task, revision=0, state='DRAFT_SPEC', assigned_role=None, active_invocation_id=None,
                             active_operation_id=None, container_id=None, resume_state=None, spec=None, plan=None,
                             work_order=None, patch=None, pending_approval=None, last_event_sequence=0, error=None)
                context = {'workload': WORKLOAD, 'completion_semantics': 'human_accepted_local_snapshot'}
                self.store.db.execute('INSERT INTO tasks VALUES(?,?,?)', (task, canonical(state).decode(), canonical(context).decode()))
                scope = prepared['scope']
                spec = make('TaskSpec', task, revision=0, title=title, request=self._artifact(task, intent),
                            repository={'repository_id': 'local-'+digest(scope['repository'])[:24], 'base_commit': scope['base_commit'], 'branch': 'codex/pilot-'+scope['id']},
                            acceptance_criteria=['Validate the approved JSON assertions and obtain human acceptance of the local snapshot'],
                            constraints=['No provider invocation or external action', 'One execution attempt; no automatic retry'],
                            allowed_paths=sorted(scope['replacements']), external_references=[], confirmed_by=principal)
                state['spec'] = self.store.put(spec)
                self.engine._event(state, context, 'task_created', {'spec': state['spec']}, role='human')
                self._move(state, context, 'SPEC_READY')
                plan = make('RepositoryTaskPlan', task, spec=state['spec'], pilot_id=scope['id'], scope_sha256=prepared['scope_sha256'], scope=self._artifact(task, scope))
                state['plan'] = self.store.put(plan)
                self._request(state, context, 'plan')
                self._move(state, context, 'AWAITING_PLAN_APPROVAL')
        return task

    def _result(self, state, context):
        result = self.store.get(context['repository_result'], state['task_id'], 'RepositoryTaskResult')
        report = json.loads(self.store.read_artifact(result['report'], state['task_id']))
        plan, scope = self._scope(state, context)
        if result['plan'] != state['plan'] or result['outcome'] != 'passed' or report['scope_sha256'] != plan['scope_sha256'] or report['status'] != 'passed' or not report['matches_receipt']:
            raise Rejected('Passing task-bound repository result required')
        expected = scope['replacements']
        if len(result['snapshots']) != len(expected) or {x['path'] for x in result['snapshots']} != set(expected): raise Rejected('Incomplete retained snapshot')
        for item in result['snapshots']:
            if self.store.read_artifact(item['content'], state['task_id']) != expected[item['path']]: raise Rejected('Retained snapshot changed')
        return result

    def approve(self, task, request_id, decision, expected_revision, principal='local-operator'):
        if decision not in ('approve', 'reject') or type(expected_revision) is not int: raise Rejected('Invalid repository approval')
        with self.store.exclusive(), self.store.transaction():
            state, context = self.store.task(task)
            self._scope(state, context)
            if state['revision'] != expected_revision or not state['pending_approval'] or state['pending_approval']['id'] != request_id:
                raise Rejected('Stale repository approval')
            request = self.store.get(state['pending_approval'], task, 'ApprovalRequest')
            kind = 'plan' if state['state'] == 'AWAITING_PLAN_APPROVAL' else 'action' if state['state'] == 'AWAITING_ACTION_APPROVAL' else None
            subject = state['plan'] if kind == 'plan' else context.get('repository_result')
            evidence = [state['spec'], state['plan']] + ([subject] if kind == 'action' else [])
            expected = digest({'subject': subject, 'evidence': evidence, 'task_id': task, 'revision': expected_revision, 'policy': WORKLOAD})
            if (kind is None or request['kind'] != kind or request['subject'] != subject or request['evidence'] != evidence
                    or request['subject_sha256'] != expected or request['task_revision'] != expected_revision
                    or request['policy_version'] != WORKLOAD or request['expires_at'] <= now()): raise Rejected('Changed or expired repository approval')
            if kind == 'action': self._result(state, context)
            accepted = make('ApprovalDecision', task, request=ref(request), subject_sha256=expected,
                            actor={'role': 'human', 'principal_id': principal}, decision=decision, reason='Operator '+decision, decided_at=now())
            context[kind+'_approval'] = self.store.put(accepted)
            self.store.db.execute('INSERT INTO approvals VALUES(?,?)', (request_id, accepted['id']))
            state['pending_approval'] = None
            self.engine._event(state, context, 'approval_granted' if decision == 'approve' else 'approval_rejected', {'decision': ref(accepted)}, role='human')
            self._move(state, context, 'CANCELLED' if decision == 'reject' else 'READY_FOR_IMPLEMENTATION' if kind == 'plan' else 'COMPLETED')
            if state['state'] == 'COMPLETED': self.engine._event(state, context, 'repository_snapshot_accepted', {'result': context['repository_result']}, role='human')
            return accepted

    def advance(self, task, expected_snapshot=None):
        with self.store.exclusive():
            state, context = self.store.task(task)
            if expected_snapshot is not None: raise Rejected('Repository tasks are not admitted to fixture batches')
            if state['state'] == 'IMPLEMENTING':
                with self.store.transaction():
                    state['error'] = error('repository_execution_uncertain')
                    self._move(state, context, 'BLOCKED')
                return self.engine.task(task)
            if state['state'] != 'READY_FOR_IMPLEMENTATION': return self.engine.task(task)
            plan, scope = self._approved(state, context)
            pilot = self._pilot(read_only=True)
            try:
                if pilot.inspect(scope['id'])['status'] != 'prepared': raise Rejected('Pilot was already consumed')
            finally: pilot.close()
            operation = uid()
            with self.store.transaction():
                self.store.db.execute("INSERT INTO operations(id,task_id,stage,slot,status,result) VALUES(?,?, 'repository_pilot','0','started',NULL)", (operation, task))
                state['active_operation_id'] = operation
                self._move(state, context, 'IMPLEMENTING')
                self.engine._event(state, context, 'repository_execution_reserved', {'pilot_id': scope['id']}, operation=operation)
            pilot = self._pilot()
            try:
                report = pilot.run(scope['id'], plan['scope_sha256'], 0)
                if not report['matches_receipt']: raise Rejected('Pilot receipt does not match its workspace')
                with self.store.transaction():
                    files = workspace_files(scope, pilot.root / scope['id'])
                    snapshots = [{'path': p, 'content': self._artifact(task, value, 'application/json')} for p, value in files.items()]
                    result = make('RepositoryTaskResult', task, plan=state['plan'], outcome=report['status'], report=self._artifact(task, report), snapshots=snapshots)
                    context['repository_result'] = self.store.put(result)
                    self.store.complete_operation(operation, 1, {'repository_result': context['repository_result']})
                    state['active_operation_id'] = None
                    self.engine._event(state, context, 'repository_execution_completed', {'result': context['repository_result']}, operation=operation)
                    if report['status'] == 'passed':
                        self._request(state, context, 'action')
                        self._move(state, context, 'AWAITING_ACTION_APPROVAL')
                    else: self._move(state, context, 'FAILED')
            except (Rejected, OSError):
                state, context = self.store.task(task)
                with self.store.transaction():
                    state['error'] = error('repository_execution_uncertain')
                    self._move(state, context, 'BLOCKED')
            finally: pilot.close()
            if self.engine.after_operation: self.engine.after_operation('repository_pilot')
            return self.engine.task(task)

    def diagnostics(self, task):
        """Persisted task preconditions plus the retained pilot's state. No container, daemon or broker is probed."""
        import sqlite3
        from .pilot_retention import eligibility
        state, context = self.store.task(task)
        _, scope = self._scope(state, context)
        applicable = state['state'] == 'BLOCKED' and bool(state['active_operation_id'])
        reasons = ['Repository recovery inspects the retained pilot and reconciles containers; it never retries execution.']
        pilot_report = None
        try:
            pilot = self._pilot(read_only=True)
            try:
                report = pilot.inspect(scope['id'])
                blockers, _ = eligibility(pilot, scope['id'], report)
            finally: pilot.close()
            workers = report.get('workers') or {'phases': {}}
            pilot_report = {'id': scope['id'], 'status': report['status'], 'revision': report['revision'], 'recovery': report['recovery'],
                            'reclamation': report['reclamation'], 'matches_receipt': report['matches_receipt'],
                            'unresolved_dispatches': sorted(p for p, e in workers['phases'].items() if e['status'] == 'dispatched'),
                            'reclaimable': not blockers, 'reclamation_blockers': blockers}
        except (Rejected, OSError, sqlite3.Error, KeyError, TypeError, ValueError) as exc:
            reasons.append('Retained pilot is unavailable for inspection: ' + (str(exc) if isinstance(exc, Rejected) else type(exc).__name__))
        return {'task_id': task, 'revision': state['revision'], 'recovery_status': 'preconditions_met' if applicable else 'not_applicable',
                'reasons': reasons, 'pilot': pilot_report,
                'runtime_checked': False, 'next_step': 'Recover using this task revision; missing journals or changed runtime will block cleanup.'}

    def recover(self, task, expected_revision, principal='local-operator'):
        with self.store.exclusive():
            state, context = self.store.task(task)
            plan, scope = self._scope(state, context)
            if type(expected_revision) is not int or state['revision'] != expected_revision or state['state'] != 'BLOCKED' or not state['active_operation_id']:
                raise Rejected('Repository recovery requires current blocked task')
            operation = state['active_operation_id']
            retained_operation = self.store.db.execute('SELECT task_id,stage,status FROM operations WHERE id=?', (operation,)).fetchone()
            if not retained_operation or tuple(retained_operation) != (task, 'repository_pilot', 'started'):
                raise Rejected('Repository recovery operation mismatch')
            pilot = self._pilot(read_only=True)
            try: report = pilot.inspect(scope['id'])
            finally: pilot.close()
            pilot = self._pilot()
            try:
                if report['status'] == 'prepared': report = pilot.abandon(scope['id'], plan['scope_sha256'], report['revision'])
                elif report['status'] == 'reserved': report = pilot.reconcile(scope['id'], plan['scope_sha256'], report['revision'])
                if report['status'] not in ('abandoned', 'interrupted', 'passed', 'failed'): raise Rejected('Repository cleanup remains uncertain')
                with self.store.transaction():
                    result = make('RepositoryTaskResult', task, plan=state['plan'], outcome='interrupted', report=self._artifact(task, report), snapshots=[])
                    context['repository_result'] = self.store.put(result)
                    self.store.complete_operation(operation, 1, {'failure': 'repository_interrupted', 'repository_result': context['repository_result']})
                    state['active_operation_id'] = state['resume_state'] = None
                    self.engine._event(state, context, 'repository_interruption_reconciled', {'principal': principal, 'result': context['repository_result']}, role='human', operation=operation)
                    self._move(state, context, 'FAILED')
            finally: pilot.close()
            return self.engine.task(task)

    def cancel(self, task, expected_revision, reason, principal='local-operator'):
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500 or redact(reason) != reason or type(expected_revision) is not int:
            raise Rejected('Invalid repository cancellation')
        with self.store.exclusive(), self.store.transaction():
            state, context = self.store.task(task)
            self._scope(state, context)
            if state['revision'] != expected_revision or state['active_operation_id'] or 'CANCELLED' not in EDGES.get(state['state'], set()):
                raise Rejected('Repository task is active, terminal or stale')
            state['pending_approval'] = None
            self.engine._event(state, context, 'operator_cancellation', {'principal': principal, 'reason': reason}, role='human')
            self._move(state, context, 'CANCELLED')
            return self.engine.task(task)

    def renew(self, task, request_id, expected_revision, principal='local-operator'):
        with self.store.exclusive(), self.store.transaction():
            state, context = self.store.task(task)
            if (state['state'] != 'AWAITING_ACTION_APPROVAL' or type(expected_revision) is not int or state['revision'] != expected_revision
                    or not state['pending_approval'] or state['pending_approval']['id'] != request_id):
                raise Rejected('Expired execution scope requires a newly prepared repository task')
            request = self.store.get(state['pending_approval'], task, 'ApprovalRequest')
            if request['expires_at'] > now(): raise Rejected('Result approval has not expired')
            self._result(state, context)
            self._request(state, context, 'action')
            state['revision'] += 1
            self.engine._event(state, context, 'approval_renewed', {'principal': principal}, role='human')
            return self.engine.task(task)


def require_task_admission(scope):
    """The pilot reads authoritative approval/operation state; CLI flags cannot supply it."""
    binding = scope['task_binding']
    store = Store(binding['store'], read_only=True)
    try:
        from types import SimpleNamespace
        tasks = RepositoryTasks(SimpleNamespace(store=store))
        state, context = store.task(binding['task_id'])
        plan, approved_scope = tasks._approved(state, context)
        operation = store.db.execute('SELECT task_id,stage,status FROM operations WHERE id=?', (state['active_operation_id'],)).fetchone()
        if (approved_scope != scope or state['state'] != 'IMPLEMENTING' or not operation
                or tuple(operation) != (binding['task_id'], 'repository_pilot', 'started')):
            raise Rejected('Pilot requires a live task-kernel reservation')
    except (KeyError, TypeError, ValueError) as exc:
        raise Rejected('Pilot requires a valid current task-kernel approval and reservation') from exc
    finally: store.close()
