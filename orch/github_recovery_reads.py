"""Offline paginated recovery captures, bound to a retained uncertain operation."""
import re
from urllib.parse import parse_qs, urlencode, urlsplit

from .contracts import Rejected, canonical, digest
from .github_journal import validated_intent
from .github_reads import MAX_BODY_BYTES, MAX_TRANSCRIPT_BYTES, _decode_exchange, _keys
from .github_reconcile import reconcile_pull_request

MAX_PAGES = 10


def prepare_recovery_plan(journal, operation, expected_sha256):
    journal.db.execute('BEGIN')
    try:
        journal._check_schema()
        record = journal.get(operation)
        if record['state'] != 'uncertain' or record['sha256'] != expected_sha256:
            raise Rejected('Recovery reads require the expected uncertain reservation')
        scope = record['scope']
        intent = validated_intent(scope)
        lookup = reconcile_pull_request(intent, intent['repository'], scope['repository_id'],
                                        {'preview_sha256': scope['preview']['sha256'], 'pages': []})['read_request']
        lookup['headers'] = {**scope['preview']['binding']['request']['headers'], 'Accept-Encoding': 'identity'}
        binding = {'schema_version': '1.0.0', 'operation_id': operation, 'scope_sha256': record['sha256'],
                   'reserved_at': record['reserved_at'], 'repository_id': scope['repository_id'],
                   'preview_sha256': scope['preview']['sha256'], 'first_request': lookup,
                   'limits': {'pages': MAX_PAGES, 'response_body_bytes': MAX_BODY_BYTES,
                              'transcript_bytes': MAX_TRANSCRIPT_BYTES, 'follow_redirects': False, 'retries': 0}}
        result = {'kind': 'GitHubRecoveryReadPlan', 'binding': binding, 'sha256': digest(binding),
                  'retry_allowed': False, 'remote_effect_confirmed': False, 'live_authorized': False}
        journal.db.execute('COMMIT')
        return result
    except BaseException:
        journal.db.execute('ROLLBACK')
        raise


def _page_request(plan, page):
    first = plan['binding']['first_request']
    url = urlsplit(first['url'])
    query = {key: values[0] for key, values in parse_qs(url.query).items()}
    query['page'] = page
    return {**first, 'url': f'{url.scheme}://{url.netloc}{url.path}?' + urlencode(query)}


def _links(plan, value, page):
    if value is None:
        return {}
    parts = value.split(',')
    if not 1 <= len(parts) <= 4:
        raise Rejected('Invalid pagination links')
    first = urlsplit(plan['binding']['first_request']['url'])
    expected = parse_qs(first.query)
    expected.pop('page')
    relations = {}
    for part in parts:
        match = re.fullmatch(r'\s*<([^<>]+)>;\s*rel="(next|prev|first|last)"\s*', part)
        if not match or match[2] in relations:
            raise Rejected('Ambiguous pagination relation')
        target = urlsplit(match[1])
        query = parse_qs(target.query, keep_blank_values=True, strict_parsing=True, max_num_fields=4)
        number = query.pop('page', [])
        if (target.scheme != 'https' or target.netloc != 'api.github.com' or target.fragment
                or target.path not in (first.path, f"/repositories/{plan['binding']['repository_id']}/pulls")
                or query != expected or len(number) != 1 or not re.fullmatch('[1-9][0-9]{0,8}', number[0])):
            raise Rejected('Pagination changed destination or scope')
        relations[match[2]] = int(number[0])
    if (('next' in relations and relations['next'] != page + 1)
            or ('prev' in relations and (page == 1 or relations['prev'] != page - 1))
            or ('first' in relations and relations['first'] != 1)
            or ('last' in relations and relations['last'] < page)
            or ('last' in relations and ((relations['last'] > page) != ('next' in relations)))):
        raise Rejected('Inconsistent pagination links')
    return relations


def decode_recovery_transcript(plan, transcript):
    try:
        if len(canonical(transcript)) > MAX_TRANSCRIPT_BYTES:
            raise Rejected('Recovery transcript budget exceeded')
        _keys(transcript, ('kind', 'schema_version', 'plan_sha256', 'pages'))
        if (transcript['kind'] != 'GitHubRecoveryReadTranscript' or transcript['schema_version'] != '1.0.0'
                or transcript['plan_sha256'] != plan['sha256']):
            raise Rejected('Recovery transcript plan mismatch')
        if not isinstance(transcript['pages'], list) or not 1 <= len(transcript['pages']) <= MAX_PAGES:
            raise Rejected('Recovery page budget exceeded')
        pages, furthest = [], 1
        for number, exchange in enumerate(transcript['pages'], 1):
            if pages and not pages[-1]['has_next']:
                raise Rejected('Read beyond the declared end of pagination')
            response, headers = _decode_exchange(_page_request(plan, number), exchange)
            links = _links(plan, headers.get('link'), number) if response['status'] == 200 else {}
            furthest = max(furthest, links.get('last', number), links.get('next', number))
            pages.append({'page': number, 'has_next': 'next' in links, **response})
        # A later page cannot erase an earlier claim that more pages existed.
        if furthest > len(pages):
            pages[-1]['has_next'] = True
        return {'preview_sha256': plan['binding']['preview_sha256'], 'pages': pages}
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        raise Rejected('Invalid recovery transcript') from exc


def reconcile_transcript(journal, operation, expected_sha256, transcript):
    plan = prepare_recovery_plan(journal, operation, expected_sha256)
    observations = decode_recovery_transcript(plan, transcript)
    journal.db.execute('BEGIN')
    try:
        journal._check_schema()
        report = journal.reconcile(operation, expected_sha256, observations)
        if report['binding']['reserved_at'] != plan['binding']['reserved_at']:
            raise Rejected('Reservation changed during capture assessment')
        binding = {'schema_version': '1.0.0', 'plan_sha256': plan['sha256'],
                   'transcript_sha256': digest(transcript), 'pages_checked': len(observations['pages']),
                   'reconciliation': report}
        result = {'kind': 'GitHubRecoveryTranscriptAssessment', 'binding': binding, 'sha256': digest(binding),
                  'status': report['status'], 'retry_allowed': False, 'remote_effect_confirmed': False, 'live_authorized': False}
        journal.db.execute('COMMIT')
        return result
    except BaseException:
        journal.db.execute('ROLLBACK')
        raise
