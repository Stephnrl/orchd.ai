"""Retire a full integration operation journal and prove its successor refuses a second attempt.

Runs the whole walkthrough for both operation journals, through the real CLI, without a
server, credentials or network calls. Retirement deletes nothing here and authorizes
nothing: no record changes state, and no dispatch or retry becomes available.
"""
import argparse
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

from orch.contracts import Rejected, canonical
from orch.github_journal import GitHubJournal
from orch.maintenance import real_path
from orch.jira_actions import preview_action
from orch.jira_journal import JiraJournal
from orch.__main__ import main as cli_main

from jira_action_support import profile, intent as jira_intent, capture as jira_capture
from test_github_journal_capacity import proposal

REASON = 'Journal full; acceptance walkthrough continues in the successor'


def cli(*argv):
    """Run one real CLI command in-process and return its report.

    Some commands end in `SystemExit(0)`; anything else is an unexpected refusal.
    """
    output = io.StringIO()
    try:
        with patch.object(sys, 'argv', ['orch', *argv]), redirect_stdout(output):
            cli_main()
    except SystemExit as exit_code:
        assert not exit_code.code, 'unexpected CLI refusal: ' + ' '.join(argv)
    return json.loads(output.getvalue())


def cli_different(*argv):
    """A comparison that reports a difference prints its report and then exits 2."""
    output = io.StringIO()
    try:
        with patch.object(sys, 'argv', ['orch', *argv]), redirect_stdout(output):
            cli_main()
    except SystemExit as exit_code:
        assert exit_code.code == 2
        return json.loads(output.getvalue())
    raise AssertionError('a changed journal must exit 2')


def refused(*argv):
    """A CLI refusal: exit code 2, no report, no partial effect."""
    try:
        with (patch.object(sys, 'argv', ['orch', *argv]), redirect_stdout(io.StringIO()),
              redirect_stderr(io.StringIO())):
            cli_main()
    except SystemExit as exit_code:
        return exit_code.code == 2
    return False


def github_stage(journal, number):
    saved = journal.stage(proposal(number), 'example/project', 123)
    return saved['operation_id'], saved['sha256']


def jira_stage(journal, number):
    config = profile()
    proposed = jira_intent('create_issue', number)
    observed = jira_capture(proposed, config)
    preview = preview_action(proposed, config, observed)
    saved = journal.stage_action(proposed, config, observed, preview['sha256'])
    return saved['binding']['operation_id'], saved['binding']['sha256']


FAMILIES = (('github', GitHubJournal, github_stage), ('jira', JiraJournal, jira_stage))


def run(family, opener, stage, root):
    first, successor = root / 'one.sqlite', root / 'two.sqlite'
    snapshot, later = root / 'snapshot', root / 'snapshot-after'
    journal = opener(first)
    try:
        staged = [stage(journal, number) for number in (1, 2, 3)]
        journal.reserve(*staged[2])                     # One attempt slot is already consumed.
        before = journal.audit()
    finally:
        journal.close()

    created = cli(family + '-journal-backup', '--journal', str(first), '--destination', str(snapshot))
    assert cli(family + '-compare-journal-backup', '--journal', str(first), '--destination', str(snapshot),
               '--expected-sha256', created['sha256'])['status'] == 'matches'
    assert refused(family + '-journal-retire', '--journal', str(first), '--destination', str(snapshot),
                   '--expected-sha256', created['sha256'], '--successor', str(successor)), 'a reason is required'
    retired = cli(family + '-journal-retire', '--journal', str(first), '--destination', str(snapshot),
                  '--expected-sha256', created['sha256'], '--successor', str(successor), '--reason', REASON)
    assert retired['status'] == 'retired' and retired['records_deleted'] == 0
    assert retired['retained_records'] == 3 and retired['uncertain_records'] == 1
    assert not (retired['restore_allowed'] or retired['retry_allowed'] or retired['live_authorized'])

    journal = opener(first)
    try:
        # Retirement moved nothing: the audit binds every record and its digest is unchanged.
        assert journal.audit()['sha256'] == before['sha256']
        try:
            stage(journal, 4)
            raise AssertionError('a retired journal must refuse a new intent')
        except Rejected:
            pass
    finally:
        journal.close()

    # The snapshot taken before retirement still holds every record, so nothing differs
    # row for row; it no longer describes a journal that has closed to new admissions.
    stale = cli_different(family + '-compare-journal-backup', '--journal', str(first),
                          '--destination', str(snapshot), '--expected-sha256', created['sha256'])
    assert stale['status'] == 'different' and stale['binding']['differences'] == []
    assert stale['binding']['succession_changed']
    assert stale['binding']['backup_succession']['status'] == 'active'
    assert stale['binding']['journal_succession']['successor'] == str(successor)

    journal = opener(first)
    try:
        # The attempt slot still belongs to the journal that holds its evidence.
        assert journal.reserve(*staged[0])
        assert journal.db.execute('SELECT count(*) FROM intents').fetchone()[0] == 3
    finally:
        journal.close()

    heir = opener(successor)
    try:
        for number in (1, 2, 3):
            try:
                stage(heir, number)
                raise AssertionError('the successor must refuse an identifier its chain retains')
            except Rejected:
                pass
        assert stage(heir, 9)                            # A genuinely new task is admitted.
    finally:
        heir.close()

    usage = cli(family + '-journal-usage', '--journal', str(first))
    assert usage['journal_status'] == 'retired' and usage['successor'] == str(successor)
    assert cli(family + '-journal-usage', '--journal', str(successor))['predecessor'] == str(first)
    walked = cli(family + '-journal-chain', '--journal', str(successor))
    assert walked['journal_count'] == 2 and walked['records_deleted'] == 0
    assert [entry['status'] for entry in walked['journals']] == ['active', 'retired']
    assert walked['journals'][0]['predecessor'] == str(first) and walked['retained_records'] == 4

    # A snapshot taken after retirement describes the journal as it now stands.
    fresh = cli(family + '-journal-backup', '--journal', str(first), '--destination', str(later))
    assert cli(family + '-compare-journal-backup', '--journal', str(first), '--destination', str(later),
               '--expected-sha256', fresh['sha256'])['status'] == 'matches'
    return {'family': family, 'journal': str(first), 'successor': str(successor),
            'retirement': retired, 'chain': walked, 'stale_comparison': stale['status'],
            'audit_sha256': before['sha256'], 'records_deleted': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', help='New directory for the retained walkthrough report; existing evidence is never overwritten')
    args = parser.parse_args()
    destination = Path(args.destination).absolute() if args.destination else None
    if destination is not None and (destination.exists() or destination.is_symlink()):
        print('FAIL: use a new report path; existing evidence is never overwritten', file=sys.stderr)
        return 2
    with ExitStack() as stack:
        stack.enter_context(patch('socket.socket', side_effect=AssertionError('Fixture must not use network')))
        stack.enter_context(patch('subprocess.Popen', side_effect=AssertionError('Fixture must not launch processes')))
        # Canonical: succession records name journals by path, and a hosted Windows runner
        # hands out an 8.3 short %TEMP% that the journal itself would never store.
        scratch = real_path(stack.enter_context(tempfile.TemporaryDirectory(prefix='orchd-succession-')))
        families = []
        for family, opener, stage in FAMILIES:
            root = scratch / family
            root.mkdir()
            families.append(run(family, opener, stage, root))
    report = {'kind': 'JournalSuccessionAcceptance', 'status': 'passed', 'families': families,
              'records_deleted': 0, 'restore_allowed': False, 'retry_allowed': False,
              'live_authorized': False, 'remote_effect_confirmed': False}
    if destination is not None:
        destination.mkdir(parents=True)
        (destination / 'acceptance.json').write_bytes(canonical(report))
    print(canonical(report).decode())
    print('PASS: both operation journals retire against a verified snapshot, delete nothing, '
          'refuse new admission, and their successors refuse a second attempt for a retained task')
    return 0


if __name__ == '__main__':
    sys.exit(main())
