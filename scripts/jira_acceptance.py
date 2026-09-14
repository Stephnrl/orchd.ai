"""Run the synthetic Jira lifecycle without a server, credentials or network calls."""
import argparse
from contextlib import ExitStack
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'tests'))

from orch.contracts import canonical, now
from orch.jira_actions import ACTIONS, preview_action, read_plan
from orch.jira_approval import prepare_approval, preflight_plan, assess_preflight, deployment_check
from orch.jira_journal import JiraJournal
from orch.jira_backup import backup_journal, compare_journal_backup, drill_journal_backup
from jira_action_support import profile, intent, capture, recovery_capture, preflight_capture


def run(destination):
    destination = Path(destination)
    destination.mkdir()  # Exclusive; never repurpose an existing output directory.
    config = profile()
    (destination/'profile.json').write_bytes(canonical(config))
    journal = JiraJournal(destination/'jira.sqlite')
    records = []
    try:
        for number, action in enumerate(ACTIONS, 1):
            proposal = intent(action, number)
            observed = capture(proposal, config)
            preview = preview_action(proposal, config, observed)
            staged = journal.stage_action(proposal, config, observed, preview['sha256'])
            operation, scope_sha = proposal['operation_id'], staged['binding']['sha256']
            evidence = {'task_id': proposal['task_id'], 'review_sha256': 'a'*64, 'policy_sha256': 'b'*64}
            approval = prepare_approval(journal, operation, scope_sha, evidence, 'author-key')
            plan = preflight_plan(journal, approval, approval['sha256'])
            preflight_observed = preflight_capture(plan, journal.get(operation)['scope'])
            observed_at = now()
            preflight = assess_preflight(journal, approval, approval['sha256'], evidence, preflight_observed, observed_at)
            if preflight['status'] != 'current': raise RuntimeError('Fixture preflight failed')
            journal.reserve(operation, scope_sha)  # Fixture slot, never a remote request.
            recovered = recovery_capture(journal.get(operation)['scope'])
            reconciliation = journal.reconcile(operation, scope_sha, recovered)
            if reconciliation['status'] != 'candidate_observed': raise RuntimeError('Fixture recovery failed')
            folder = destination/action
            folder.mkdir()
            for name, document in (('intent', proposal), ('capture', observed), ('preview', preview),
                                   ('read-plan', read_plan(proposal, config)), ('evidence', evidence), ('approval', approval),
                                   ('preflight-plan', plan), ('preflight-capture', preflight_observed),
                                   ('preflight', preflight), ('recovery-capture', recovered), ('reconciliation', reconciliation)):
                (folder/(name+'.json')).write_bytes(canonical(document))
            records.append({'action': action, 'operation_id': operation, 'scope_sha256': scope_sha,
                            'preflight': preflight['status'], 'recovery': reconciliation['status']})
        backup = backup_journal(destination/'jira.sqlite', destination/'backup')
        comparison = compare_journal_backup(destination/'jira.sqlite', destination/'backup', backup['sha256'])
        drill = drill_journal_backup(destination/'backup', backup['sha256'])
        if comparison['status'] != 'matches' or drill['status'] != 'passed': raise RuntimeError('Fixture backup recovery failed')
        result = {'kind': 'JiraFixtureAcceptance', 'status': 'passed', 'records': records, 'backup': backup,
                  'comparison': comparison, 'drill': drill, 'deployment': deployment_check(config),
                  'fixture_only': True, 'live_authorized': False, 'retry_allowed': False, 'remote_effect_confirmed': False}
        (destination/'acceptance.json').write_bytes(canonical(result))
        return result
    finally:
        journal.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', help='New directory for retained synthetic evidence; otherwise use temporary storage')
    args = parser.parse_args()
    with ExitStack() as stack:
        stack.enter_context(patch('socket.socket', side_effect=AssertionError('Fixture must not use network')))
        stack.enter_context(patch('subprocess.Popen', side_effect=AssertionError('Fixture must not launch processes')))
        destination = Path(args.destination) if args.destination else Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='orchd-jira-acceptance-')))/'run'
        result = run(destination)
        print(canonical(result).decode())


if __name__ == '__main__':
    main()
