"""Synthetic two-task batch walkthrough; no live providers or external actions."""
import argparse
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orch.batches import Batches
from orch.contracts import canonical
from orch.engine import Engine
from orch.execution import Executor


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
        report = {'kind':'SerialBatchAcceptance', 'status':'passed', 'tasks':tasks, 'rounds':rounds,
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
