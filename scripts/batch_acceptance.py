"""Synthetic batch walkthrough: serial, then parallel, then an agent holding a task.

The batch rounds run two tasks strictly in order, pausing at every human decision. The
parallel walkthrough advances two more tasks with two real worker processes at once and
shows the admission bound refusing a third. The agent walkthrough then holds a task the way
a container does: claim in the role the next step needs, renew, refuse a worker meanwhile,
and release on the way down.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from datetime import datetime, timedelta

import json
import secrets as randomness

from orch import agent_sessions, task_drafts
from orch.agent_client import AgentClient
from orch.batches import Batches
from orch.contracts import Rejected, canonical, now
from orch.engine import Engine
from orch.execution import Executor

WORKER = ("import sys\n"
          "from orch.engine import Engine\n"
          "from orch.execution import Executor\n"
          "engine = Engine(sys.argv[1], Executor('trusted-fixture'))\n"
          "print(engine.run(sys.argv[2])['state']['state'])\n"
          "engine.close()\n")


def parallel(engine, store):
    """Two workers, two tasks, one bound: real processes, not a simulation."""
    tasks = [engine.create_task('Parallel worker acceptance ' + str(i)) for i in range(2)]
    workers = [subprocess.Popen([sys.executable, '-c', WORKER, str(store), task], cwd=ROOT,
                                env={**os.environ, 'PYTHONPATH': str(ROOT)},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
               for task in tasks]
    for worker, task in zip(workers, tasks):
        out, err = worker.communicate(timeout=300)
        if worker.returncode != 0 or out.strip() != 'AWAITING_PLAN_APPROVAL':
            raise RuntimeError('Parallel worker did not finish its task: ' + err[-400:])
    if engine.store.owners():
        raise RuntimeError('A finished worker left a claim behind')
    for task in tasks:
        if engine.task(task)['state']['state'] != 'AWAITING_PLAN_APPROVAL':
            raise RuntimeError('Parallel work did not reach the first approval')

    # The bound refuses a third task before anything is claimed or changed.
    occupied, waiting = tasks[0], engine.create_task('Parallel worker acceptance refused')
    before = engine.task(waiting)
    with engine.store.exclusive(occupied):
        with engine.store.transaction():
            engine.store.claim(occupied, engine.owner, 0, 1)
        with patch('orch.engine.MAX_ACTIVE_TASKS', 1):
            try:
                engine.advance(waiting)
                raise RuntimeError('The admission bound must refuse a third task')
            except Rejected as exc:
                if 'Too many tasks' not in str(exc):
                    raise RuntimeError('Refused for the wrong reason: ' + str(exc))
        if engine.store.owner(waiting) or engine.task(waiting) != before:
            raise RuntimeError('A refused task must be left untouched')
        with engine.store.transaction():
            engine.store.disclaim(occupied, engine.owner)
    return {'tasks': tasks, 'states': [engine.task(task)['state']['state'] for task in tasks],
            'refused_at_bound': True, 'claims_remaining': len(engine.store.owners())}


def agents(engine):
    """One task held by an agent the way a container holds it, start to finish."""
    task = engine.create_task('Agent session acceptance')
    role = agent_sessions.role_for(engine.task(task)['state']['state'])
    if role != 'lead_planner':
        raise RuntimeError('The fixture workflow should start with the lead planner')
    try:
        agent_sessions.claim(engine, task, 'junior-devops', 'junior')
        raise RuntimeError('An agent must not take work belonging to another role')
    except Rejected:
        pass
    claimed = agent_sessions.claim(engine, task, 'lead-devops', role, 900)
    if claimed['status'] != 'claimed' or claimed['role'] != role:
        raise RuntimeError('The agent did not take the task')
    try:
        engine.advance(task)
        raise RuntimeError('A worker must not advance a task an agent holds')
    except Rejected:
        pass
    renewed = agent_sessions.renew(engine, task, 'lead-devops', 900)
    if renewed['expires_at'] <= claimed['expires_at']:
        raise RuntimeError('Renewal did not extend the lease')
    listed = agent_sessions.sessions(engine.store)
    if listed['held'] != 1 or listed['sessions'][0]['kind'] != 'agent':
        raise RuntimeError('The session should be visible while it is held')

    # An abandoned container leaves a lease that simply expires; the task comes back.
    with patch('orch.storage.now',
               return_value=(datetime.fromisoformat(now()) + timedelta(hours=2)).isoformat().replace('+00:00', 'Z')):
        if engine.store.live(engine.store.owner(task)):
            raise RuntimeError('An expired lease must not still hold the task')
    # A container that is powered off cleanly releases instead, which is immediate.
    if agent_sessions.release(engine, task, 'lead-devops')['status'] != 'released':
        raise RuntimeError('The agent did not release the task')
    if engine.run(task)['state']['state'] != 'AWAITING_PLAN_APPROVAL':
        raise RuntimeError('A released task should advance normally')
    if agent_sessions.role_for(engine.task(task)['state']['state']) != 'human':
        raise RuntimeError('An approval pause belongs to a human')
    try:
        agent_sessions.claim(engine, task, 'lead-devops', 'lead_planner')
        raise RuntimeError('No agent may take a task waiting on a human')
    except Rejected:
        pass
    return {'task': task, 'claimed_as': role, 'renewed': True, 'released': True,
            'human_pause_refused': True, 'sessions_held_after': agent_sessions.sessions(engine.store)['held']}


def specified(engine):
    """A task with no specification, drafted and questioned, then confirmed by a person."""
    task = engine.create_task('Draft specification acceptance', draft=True)
    if engine.task(task)['state']['spec'] is not None:
        raise RuntimeError('A drafted task starts with no specification')
    draft = {'title': 'Add a rate limit to the public API',
             'request': 'Requests from one client should be limited.',
             'acceptance_criteria': ['A client over the limit receives 429'],
             'constraints': ['No new runtime dependency'], 'allowed_paths': ['api/limits.py']}
    try:
        task_drafts.confirm(engine, task, '0' * 64, 'local-operator')
        raise RuntimeError('There is nothing to confirm before anything is drafted')
    except Rejected:
        pass
    task_drafts.draft(engine, task, draft)
    task_drafts.ask(engine, task, [{'question_id': 'window', 'question': 'Over what window?'}])
    if engine.task(task)['state']['state'] != 'AWAITING_CLARIFICATION':
        raise RuntimeError('A question should stop the work')
    try:
        task_drafts.draft(engine, task, draft)
        raise RuntimeError('Nothing may be drafted while a question is outstanding')
    except Rejected:
        pass
    task_drafts.answer(engine, task, [{'question_id': 'window', 'answer': 'A rolling minute.'}], 'local-operator')

    # A human reads one specification; the project manager then revises it. The hash the
    # human holds no longer names the specification, so their confirmation cannot apply.
    read = task_drafts.show(engine, task)['spec_sha256']
    task_drafts.draft(engine, task, dict(draft, request='Limited over a rolling minute.'))
    try:
        task_drafts.confirm(engine, task, read, 'local-operator')
        raise RuntimeError('A specification revised after it was read must not be confirmable')
    except Rejected:
        pass
    current = task_drafts.show(engine, task)['spec_sha256']
    confirmed = task_drafts.confirm(engine, task, current, 'local-operator')
    if confirmed['state'] != 'SPEC_READY' or not confirmed['confirmed']:
        raise RuntimeError('Confirming the current specification should reach SPEC_READY')
    specs = engine.store.db.execute(
        "SELECT payload FROM records WHERE task_id=? AND kind='TaskSpec'", (task,)).fetchall()
    signed = [json.loads(row[0])['confirmed_by'] for row in specs]
    if sorted(x or '' for x in signed) != ['', '', 'local-operator']:
        raise RuntimeError('Every draft should be kept, and only the confirmed one signed')
    return {'task': task, 'drafts_kept': len(specs), 'clarified': True,
            'stale_confirmation_refused': True, 'confirmed_by': 'local-operator',
            'state': confirmed['state']}


def through_the_api(engine, store, root):
    """The same session, but taken the way a container takes it: a secret and a socket."""
    agents = root / 'agents'
    for name, role in (('lead-devops', 'lead_planner'), ('junior-devops', 'junior')):
        folder = agents / name
        folder.mkdir(parents=True)
        (folder / 'role').write_text(role + "\n", encoding='ascii')
        secret = folder / 'secret'
        secret.write_text(randomness.token_hex(32) + "\n", encoding='ascii')
        os.chmod(secret, 0o600)     # A world-readable secret is refused on POSIX.
    task = engine.create_task('Agent API acceptance')
    service = subprocess.Popen([sys.executable, '-m', 'orch', 'agent-serve', '--data', str(store),
                                '--agents', str(agents), '--port', '0'], cwd=ROOT,
                               env={**os.environ, 'PYTHONPATH': str(ROOT)},
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    try:
        started = json.loads(service.stdout.readline())
        endpoint = started['endpoint']
        lead = AgentClient(endpoint, agents / 'lead-devops' / 'secret')
        junior = AgentClient(endpoint, agents / 'junior-devops' / 'secret')
        if lead.whoami()['agent'] != 'lead-devops' or junior.whoami()['roles'] != ['junior']:
            raise RuntimeError('The service did not read identity from the secret')
        try:
            junior.claim(task, 'lead_planner')
            raise RuntimeError('An agent must not claim a role it was not enrolled for')
        except Rejected:
            pass
        claimed = lead.claim(task, 'lead_planner', 300)
        if claimed['session']['agent'] != 'lead-devops':
            raise RuntimeError('The claim did not name the authenticated agent')
        if lead.renew(task, 600)['status'] != 'renewed':
            raise RuntimeError('The lease did not renew over the wire')
        try:
            junior.release(task)
            raise RuntimeError('One agent must not release another agent\'s session')
        except Rejected:
            pass
        if lead.release(task)['status'] != 'released':
            raise RuntimeError('The agent did not release over the wire')
        held = junior.sessions()['held']
    finally:
        service.terminate()          # Never leave a service holding the store behind.
        try:
            service.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            service.kill()
            service.communicate(timeout=30)
    return {'task': task, 'endpoint_served': True, 'identity_from_secret': True,
            'unenrolled_role_refused': True, 'sessions_held_after': held}


def managed(engine, store, root):
    """A project manager container doing its step over a socket: claim, draft, ask, stop."""
    agents = root / 'manager-agents'
    folder = agents / 'project-manager'
    folder.mkdir(parents=True)
    (folder / 'role').write_text('project_manager\n', encoding='ascii')
    secret = folder / 'secret'
    secret.write_text(randomness.token_hex(32) + '\n', encoding='ascii')
    os.chmod(secret, 0o600)

    task = engine.create_task('Draft specification by agent', draft=True)
    service = subprocess.Popen([sys.executable, '-m', 'orch', 'agent-serve', '--data', str(store),
                                '--agents', str(agents), '--port', '0'],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                               cwd=str(Path(__file__).resolve().parents[1]))
    try:
        started = json.loads(service.stdout.readline())
        manager = AgentClient(started['endpoint'], str(secret))

        # Nothing may be written to a task this agent has not claimed.
        try:
            manager.draft(task, {'title': 'x', 'request': 'y', 'acceptance_criteria': ['z']})
            raise RuntimeError('An agent must not write to a task it is not holding')
        except Rejected:
            pass

        manager.claim(task, 'project_manager', 900)
        if manager.specification(task)['draft']['spec'] is not None:
            raise RuntimeError('A drafted task starts with no specification')
        written = manager.draft(task, {
            'title': 'Add a rate limit to the public API',
            'request': 'Requests from one client should be limited.',
            'acceptance_criteria': ['A client over the limit receives 429'],
            'constraints': ['No new runtime dependency'], 'allowed_paths': ['api/limits.py']})['draft']
        if written['spec_revision'] != 0 or written['confirmed']:
            raise RuntimeError('The agent did not write an unconfirmed first draft')

        asked = manager.ask(task, [{'question_id': 'window', 'question': 'Over what window?'}])['draft']
        if asked['state'] != 'AWAITING_CLARIFICATION':
            raise RuntimeError('A question should stop the work')

        # A person answers; no route the agent can reach does.
        task_drafts.answer(engine, task, [{'question_id': 'window', 'answer': 'A rolling minute.'}],
                           'local-operator')
        revised = manager.draft(task, {
            'title': 'Add a rate limit to the public API',
            'request': 'Requests from one client are limited over a rolling minute.',
            'acceptance_criteria': ['A client over the limit receives 429'],
            'constraints': ['No new runtime dependency'], 'allowed_paths': ['api/limits.py']})['draft']

        # And a person confirms, after which the agent that wrote it cannot change a word.
        confirmed = task_drafts.confirm(engine, task, revised['spec_sha256'], 'local-operator')
        if confirmed['state'] != 'SPEC_READY':
            raise RuntimeError('Confirmation should reach SPEC_READY')
        try:
            manager.draft(task, {'title': 'after', 'request': 'after', 'acceptance_criteria': ['after']})
            raise RuntimeError('A confirmed specification must be beyond the agent that wrote it')
        except Rejected:
            pass
    finally:
        service.terminate()
        try:
            service.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            service.kill()
            service.communicate(timeout=30)
    return {'task': task, 'unheld_write_refused': True, 'drafted_over_the_wire': True,
            'clarification_stopped_the_work': True, 'confirmed_by': 'local-operator',
            'agent_locked_out_after_confirmation': True, 'state': confirmed['state']}


def run(destination):
    destination = Path(destination)
    destination.mkdir()
    engine = Engine(destination/'store', Executor('trusted-fixture'))
    try:
        tasks = [engine.create_task('Serial batch acceptance '+str(i)) for i in range(2)]
        batches, rounds = Batches(engine), []
        for expected in ('AWAITING_PLAN_APPROVAL', 'AWAITING_ACTION_APPROVAL', 'COMPLETED'):
            batch = batches.create({'tasks':[{'task_id':task, 'expected_revision':engine.task(task)['state']['revision']} for task in tasks]})
            result = batches.run(batch['id'], batch['scope_sha256'], batch['revision'])
            if any(e['status'] != 'stopped' or e['result'] != expected for e in result['entries']):
                raise RuntimeError('Batch acceptance did not reach the expected boundary')
            rounds.append({'batch_id':batch['id'], 'scope_sha256':batch['scope_sha256'], 'state':expected})
            if expected != 'COMPLETED':
                # This explicitly invoked fixture script represents each human decision.
                # The batch runner itself has no approval operation.
                for task in tasks:
                    state = engine.task(task)['state']
                    engine.approve(task, state['pending_approval']['id'], 'approve', state['revision'])
        # The same kernel, without a batch: two workers advancing two tasks at once.
        concurrent = parallel(engine, destination/'store')
        held = agents(engine)
        served = through_the_api(engine, destination/'store', destination)
        drafted = specified(engine)
        by_agent = managed(engine, destination/'store', destination)
        report = {'kind':'SerialBatchAcceptance', 'status':'passed', 'tasks':tasks, 'rounds':rounds,
                  'parallel_workers':concurrent, 'agent_session':held, 'agent_api':served, 'draft_specification':drafted, 'agent_drafting':by_agent,
                  'fixture_only':True, 'live_authorized':False}
        (destination/'acceptance.json').write_bytes(canonical(report))
        return report
    finally: engine.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', help='New directory for retained synthetic evidence')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='orchd-batch-acceptance-') as temporary:
        print(canonical(run(args.destination or Path(temporary)/'run')).decode())


if __name__ == '__main__': main()
