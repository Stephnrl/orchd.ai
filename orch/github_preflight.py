"""Compose local review and supplied ref observations; never contact GitHub."""
from datetime import datetime, timedelta
import re

from .contracts import Rejected, canonical, digest
from .github_approval import check_approval
from .github_bundle_approval import assess_bundle_approval, _policy_current
from .github_journal import validated_intent
from .github_preview import load_intent
from .github_refs import assess_refs

MAX_SNAPSHOT_BYTES = 65536
OBSERVATION_LIFETIME_SECONDS = 300


def _instant(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z', value):
        raise Rejected('Observation time must be a UTC instant')
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise Rejected('Invalid observation time') from exc


def _refs(scope, observations):
    intent = validated_intent(scope)
    return assess_refs(intent, intent['repository'], scope['repository_id'], observations)


def prepare_observation_snapshot(journal, operation, expected_scope_sha256, observations, observed_at):
    _instant(observed_at)
    journal.db.execute('BEGIN')
    try:
        journal._check_schema()
        record = journal.get(operation)
        if record['state'] != 'prepared' or record['sha256'] != expected_scope_sha256:
            raise Rejected('Observations require the expected prepared scope')
        _refs(record['scope'], observations)  # Validate shape/budgets, retain mismatching observations for review.
        binding = {'schema_version': '1.0.0', 'scope_sha256': record['sha256'],
                   'observed_at': observed_at, 'observations': observations}
        snapshot = {'kind': 'GitHubRefObservationSnapshot', 'binding': binding, 'sha256': digest(binding),
                    'remote_refs_verified': False, 'live_authorized': False}
        if len(canonical(snapshot)) + 1 > MAX_SNAPSHOT_BYTES:
            raise Rejected('Observation snapshot byte budget exceeded')
        journal.db.execute('COMMIT')
        return snapshot
    except BaseException:
        journal.db.execute('ROLLBACK')
        raise


def _check_snapshot(scope, snapshot, expected_sha256):
    try:
        if len(canonical(snapshot)) > MAX_SNAPSHOT_BYTES:
            raise Rejected('Observation snapshot byte budget exceeded')
        binding = snapshot['binding']
        _instant(binding['observed_at'])
        expected_binding = {'schema_version': '1.0.0', 'scope_sha256': binding['scope_sha256'],
                            'observed_at': binding['observed_at'], 'observations': binding['observations']}
        expected = {'kind': 'GitHubRefObservationSnapshot', 'binding': expected_binding,
                    'sha256': digest(expected_binding), 'remote_refs_verified': False, 'live_authorized': False}
        if canonical(expected) != canonical(snapshot) or expected['sha256'] != expected_sha256:
            raise Rejected('Observation snapshot binding mismatch')
        if not isinstance(binding['scope_sha256'], str) or not re.fullmatch('[a-f0-9]{64}', binding['scope_sha256']):
            raise Rejected('Invalid observation scope digest')
        refs = _refs(scope, binding['observations'])
        blockers = list(refs['blockers'])
        if binding['scope_sha256'] != digest(scope):
            blockers.append('scope_mismatch')
        return refs, blockers
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise Rejected('Invalid observation snapshot') from exc


def _observation_report(snapshot, refs, blockers, issued_at, checked_at):
    observed = _instant(snapshot['binding']['observed_at'])
    checked, issued = datetime.fromisoformat(checked_at), datetime.fromisoformat(issued_at)
    if observed < issued:
        blockers.append('predates_approval_preview')
    if observed > checked:
        blockers.append('from_future')
    try:
        expires = observed + timedelta(seconds=OBSERVATION_LIFETIME_SECONDS)
    except OverflowError as exc:
        raise Rejected('Observation expiry is out of range') from exc
    if checked >= expires:
        blockers.append('stale')
    binding = {'schema_version': '1.0.0', 'snapshot_sha256': snapshot['sha256'],
               'scope_sha256': snapshot['binding']['scope_sha256'], 'observed_at': snapshot['binding']['observed_at'],
               'expires_at': expires.isoformat().replace('+00:00', 'Z'), 'checked_at': checked_at,
               'max_age_seconds': OBSERVATION_LIFETIME_SECONDS, 'refs': refs, 'blockers': sorted(set(blockers))}
    return {'kind': 'GitHubObservationAssessment', 'binding': binding, 'sha256': digest(binding),
            'status': 'blocked' if blockers else 'matches', 'remote_refs_verified': False, 'live_authorized': False}


def assess_preflight(journal, preview, expected_preview_sha256, bundle_source, expected_bundle_sha256,
                     observation_source, expected_observation_sha256):
    review = assess_bundle_approval(journal, preview, expected_preview_sha256, bundle_source, expected_bundle_sha256)
    observations = final_approval = None
    blockers = list(review['binding']['blockers'])
    if review['status'] == 'current':
        approval = preview['binding']['approval']
        snapshot = load_intent(observation_source)
        refs, observation_blockers = _check_snapshot(approval['binding']['scope'], snapshot, expected_observation_sha256)
        final_approval = check_approval(journal, approval, approval['sha256'], approval['binding']['evidence'])
        checked = final_approval['binding']['checked_at']
        observations = _observation_report(snapshot, refs, observation_blockers, approval['binding']['issued_at'], checked)
        blockers.extend('refs:' + reason for reason in observations['binding']['blockers'])
        blockers.extend('approval:' + reason for reason in final_approval['binding']['blockers'])
        if not _policy_current(review['binding']['bundle'], checked):
            blockers.append('evidence:policy_not_current')
    binding = {'schema_version': '1.0.0', 'review': review, 'observations': observations,
               'final_approval': final_approval, 'blockers': sorted(set(blockers))}
    return {'kind': 'GitHubOfflinePreflight', 'binding': binding, 'sha256': digest(binding),
            'status': 'blocked' if blockers else 'current',
            'bundle_bytes_verified': review['bundle_bytes_verified'], 'observations_checked': observations is not None,
            'remote_refs_verified': False, 'evidence_verified': False, 'live_authorized': False, 'retry_allowed': False}
