"""Offline contract for a future GitHub reader. No sockets, credentials or retries."""
import base64
import binascii
import json
import re

from .contracts import Rejected, canonical, digest
from .github_journal import validated_intent
from .github_preflight import prepare_observation_snapshot

MAX_TRANSCRIPT_BYTES = 65536
MAX_BODY_BYTES = 12288
ROLES = ('repository', 'base', 'head')


def prepare_read_plan(journal, operation, expected_scope_sha256):
    journal.db.execute('BEGIN')
    try:
        journal._check_schema()
        record = journal.get(operation)
        if record['state'] != 'prepared' or record['sha256'] != expected_scope_sha256:
            raise Rejected('Read plan requires the expected prepared scope')
        scope = record['scope']
        intent = validated_intent(scope)
        root = 'https://api.github.com/repos/' + intent['repository']
        urls = {'repository': root, 'base': root + '/git/ref/heads/' + intent['base'],
                'head': root + '/git/ref/heads/' + intent['head']}
        headers = {**scope['preview']['binding']['request']['headers'], 'Accept-Encoding': 'identity'}
        binding = {'schema_version': '1.0.0', 'operation_id': operation, 'scope_sha256': record['sha256'],
                   'preview_sha256': scope['preview']['sha256'], 'repository_id': scope['repository_id'],
                   'requests': {role: {'method': 'GET', 'url': urls[role], 'headers': headers} for role in ROLES},
                   'limits': {'response_body_bytes': MAX_BODY_BYTES, 'transcript_bytes': MAX_TRANSCRIPT_BYTES,
                              'follow_redirects': False, 'retries': 0}}
        result = {'kind': 'GitHubRefReadPlan', 'binding': binding, 'sha256': digest(binding),
                  'remote_refs_verified': False, 'live_authorized': False}
        journal.db.execute('COMMIT')
        return result
    except BaseException:
        journal.db.execute('ROLLBACK')
        raise


def _keys(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise Rejected('Invalid read transcript shape')


def _headers(pairs):
    if not isinstance(pairs, list) or len(pairs) > 32 or len(canonical(pairs)) > 8192:
        raise Rejected('Response header budget exceeded')
    headers = {}
    for pair in pairs:
        if (not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(v, str) for v in pair)
                or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", pair[0])
                or any(ord(c) < 32 or ord(c) > 126 for c in pair[1])):
            raise Rejected('Invalid response header')
        name = pair[0].lower()
        if name in headers:
            raise Rejected('Duplicate response header')
        headers[name] = pair[1]
    if headers.get('content-encoding', 'identity').lower() != 'identity':
        raise Rejected('Encoded response bodies are not supported')
    return headers


def _json(raw):
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise Rejected('Duplicate response JSON key')
            obj[key] = value
        return obj

    def invalid(_):
        raise Rejected('Non-JSON response number')

    # UTF-8 only: no implicit UTF-16/32 or BOM decoding.
    return json.loads(raw.decode('utf-8'), object_pairs_hook=unique, parse_constant=invalid)


def decode_transcript(plan, transcript):
    """Normalize untrusted saved exchanges; a matching transcript is not a network attestation."""
    try:
        if len(canonical(transcript)) > MAX_TRANSCRIPT_BYTES:
            raise Rejected('Read transcript budget exceeded')
        _keys(transcript, ('kind', 'schema_version', 'plan_sha256', 'responses'))
        if (transcript['kind'] != 'GitHubRefReadTranscript' or transcript['schema_version'] != '1.0.0'
                or transcript['plan_sha256'] != plan['sha256']):
            raise Rejected('Read transcript plan mismatch')
        _keys(transcript['responses'], ROLES)
        observations = {'preview_sha256': plan['binding']['preview_sha256']}
        for role in ROLES:
            exchange = transcript['responses'][role]
            _keys(exchange, ('request', 'error', 'response'))
            if canonical(exchange['request']) != canonical(plan['binding']['requests'][role]):
                raise Rejected('Unexpected transcript request')
            if exchange['error'] is not None:
                if exchange['error'] not in ('timeout', 'connection_failed', 'tls_failed') or exchange['response'] is not None:
                    raise Rejected('Invalid transport failure')
                observations[role] = {'status': 0, 'body': None}
                continue
            response = exchange['response']
            _keys(response, ('url', 'redirected', 'status', 'headers', 'body_base64'))
            if response['url'] != exchange['request']['url'] or response['redirected'] is not False:
                raise Rejected('Redirected or foreign response')
            status = response['status']
            if type(status) is not int or not 200 <= status <= 599:
                raise Rejected('Invalid HTTP response status')
            headers = _headers(response['headers'])
            encoded = response['body_base64']
            if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_BODY_BYTES + 2) // 3):
                raise Rejected('Response body budget exceeded')
            raw = base64.b64decode(encoded, validate=True)
            if len(raw) > MAX_BODY_BYTES or base64.b64encode(raw).decode('ascii') != encoded:
                raise Rejected('Invalid response body encoding')
            if 'content-length' in headers:
                length = headers['content-length']
                if not re.fullmatch('[0-9]{1,8}', length) or int(length) != len(raw):
                    raise Rejected('Response content length mismatch')
            body = None
            if status == 200:
                if 'location' in headers or not re.fullmatch(r'application/(?:json|vnd\.github\+json)(?:;\s*charset=utf-8)?', headers.get('content-type', ''), re.I):
                    raise Rejected('Unsupported successful response type or redirect')
                body = _json(raw)
                # Also rejects escaped lone surrogates and non-finite exponent overflow.
                canonical(body)
            observations[role] = {'status': status, 'body': body}
        return observations
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, binascii.Error) as exc:
        raise Rejected('Invalid read transcript') from exc


def snapshot_from_transcript(journal, operation, expected_scope_sha256, transcript, observed_at):
    plan = prepare_read_plan(journal, operation, expected_scope_sha256)
    observations = decode_transcript(plan, transcript)
    # Fresh transaction rechecks schema, scope and prepared state after parsing.
    return prepare_observation_snapshot(journal, operation, expected_scope_sha256, observations, observed_at)
