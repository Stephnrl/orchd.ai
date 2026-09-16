"""Synthetic two-task batch walkthrough, serial then parallel; no live providers.

The batch rounds run two tasks strictly in order, pausing at every human decision. The
parallel walkthrough then advances two more tasks with two real worker processes at once,
and shows the admission bound refusing a third before it starts.
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

from orch.batches import Batches
from orch.contracts import Rejected, canonical
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
        report = {'kind':'SerialBatchAcceptance', 'status':'passed', 'tasks':tasks, 'rounds':rounds,
                  'parallel_workers':concurrent, 'fixture_only':True, 'live_authorized':False}
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
