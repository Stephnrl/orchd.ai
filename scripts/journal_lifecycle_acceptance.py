"""Walk both operation journals through their whole lifecycle, through the real CLI.

A full journal retires into a successor that refuses a second attempt for any task its
chain retains, and a prepared operation whose scope went stale is withdrawn so its task can
be prepared again. Neither deletes a record, and neither releases an attempt: a consumed
one is refused here as part of the walkthrough. No server, credentials or network calls are
involved, and nothing authorizes dispatch or retry.
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
REVISION_REASON = 'The base branch moved before dispatch'


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


def github_stage(journal, number, character=None):
    source = proposal(number)
    if character:                       # The same task under a new operation identifier.
        source['operation_id'] = character * 32
        source['head'] = 'orchd/' + source['task_id'] + '/' + character * 32
    saved = journal.stage(source, 'example/project', 123)
    return saved['operation_id'], saved['sha256']


def jira_stage(journal, number, character=None):
    config = profile()
    proposed = jira_intent('create_issue', number)
    if character:                       # The same task under a new operation identifier.
        proposed['operation_id'] = character * 32
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
    revision = revise(family, opener, stage, successor, root)
    return {'family': family, 'journal': str(first), 'successor': str(successor),
            'retirement': retired, 'chain': walked, 'stale_comparison': stale['status'],
            'audit_sha256': before['sha256'], 'revision': revision, 'records_deleted': 0}


def revise(family, opener, stage, journal_path, root):
    """Withdraw a prepared operation in the successor and prepare its task again."""
    snapshot, after = root / 'successor-snapshot', root / 'successor-after'
    journal = opener(journal_path)
    try:
        live = journal.db.execute("SELECT operation_id,sha256 FROM intents WHERE state='prepared'").fetchone()
        operation, scope = live['operation_id'], live['sha256']
        task = journal.db.execute("SELECT task_id FROM intents WHERE operation_id=?", (operation,)).fetchone()[0]
        reserved = stage(journal, 5)                 # A second task, whose attempt is consumed.
        journal.reserve(*reserved)
    finally:
        journal.close()
    # The snapshot has to describe the journal as the upgrade will find it.
    created = cli(family + '-journal-backup', '--journal', str(journal_path), '--destination', str(snapshot))

    assert refused(family + '-journal-withdraw', '--journal', str(journal_path), '--operation-id',
                   operation, '--expected-sha256', scope, '--reason', REVISION_REASON), 'not upgraded yet'
    upgraded = cli(family + '-journal-upgrade', '--journal', str(journal_path),
                   '--destination', str(snapshot), '--expected-sha256', created['sha256'])
    assert upgraded['status'] == 'upgraded' and upgraded['records_changed'] == 0
    assert upgraded['records_deleted'] == 0 and upgraded['audit_sha256'] == created['audit_sha256']

    assert refused(family + '-journal-withdraw', '--journal', str(journal_path), '--operation-id',
                   reserved[0], '--expected-sha256', reserved[1], '--reason', REVISION_REASON), \
        'a consumed attempt must never be withdrawn'
    withdrawn = cli(family + '-journal-withdraw', '--journal', str(journal_path), '--operation-id',
                    operation, '--expected-sha256', scope, '--reason', REVISION_REASON)
    assert withdrawn['status'] == 'withdrawn' and not withdrawn['attempt_released']
    assert withdrawn['records_deleted'] == 0 and withdrawn['task_id'] == task

    journal = opener(journal_path)
    try:
        for identifier in (None, 'f'):               # The spent identifier stays spent.
            try:
                stage(journal, 9, identifier) if identifier else stage(journal, 9)
                if identifier is None:
                    raise AssertionError('a withdrawn operation identifier must not be reused')
            except Rejected:
                if identifier is not None:
                    raise AssertionError('the task must be free under a new identifier')
        try:
            stage(journal, 9, 'e')
            raise AssertionError('one live operation per task')
        except Rejected:
            pass
        try:
            journal.reserve(operation, scope)
            raise AssertionError('a withdrawn operation must never be reserved')
        except Rejected:
            pass
    finally:
        journal.close()

    usage = cli(family + '-journal-usage', '--journal', str(journal_path))
    # The withdrawn original, the reserved second task and the replacement: nothing left.
    assert usage['withdrawn_records'] == 1 and usage['records'] == 3
    stale = cli_different(family + '-compare-journal-backup', '--journal', str(journal_path),
                          '--destination', str(snapshot), '--expected-sha256', created['sha256'])
    assert any('withdrawal_mismatch' in entry['reasons'] for entry in stale['binding']['differences'])
    fresh = cli(family + '-journal-backup', '--journal', str(journal_path), '--destination', str(after))
    assert cli(family + '-compare-journal-backup', '--journal', str(journal_path), '--destination',
               str(after), '--expected-sha256', fresh['sha256'])['status'] == 'matches'
    drill = cli(family + '-journal-recovery-drill', '--destination', str(after), '--expected-sha256', fresh['sha256'])
    assert drill['status'] == 'passed' and drill['binding']['withdrawn_checked'] == 1
    return {'upgrade': upgraded, 'withdrawal': withdrawn, 'withdrawn_records': usage['withdrawn_records'],
            'records_deleted': 0, 'attempt_released': False}


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
        scratch = real_path(stack.enter_context(tempfile.TemporaryDirectory(prefix='orchd-lifecycle-')))
        families = []
        for family, opener, stage in FAMILIES:
            root = scratch / family
            root.mkdir()
            families.append(run(family, opener, stage, root))
    report = {'kind': 'JournalLifecycleAcceptance', 'status': 'passed', 'families': families,
              'records_deleted': 0, 'restore_allowed': False, 'retry_allowed': False,
              'live_authorized': False, 'remote_effect_confirmed': False}
    if destination is not None:
        destination.mkdir(parents=True)
        (destination / 'acceptance.json').write_bytes(canonical(report))
    print(canonical(report).decode())
    print('PASS: both operation journals retire into a successor that refuses a second attempt, '
          'withdraw a stale prepared operation so its task can be prepared again, and refuse to '
          'withdraw a consumed attempt; no record is deleted')
    return 0


if __name__ == '__main__':
    sys.exit(main())
