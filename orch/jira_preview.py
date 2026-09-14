"""Offline Jira Data Center issue review and explicit comment preparation."""
from datetime import datetime
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, ValidationError

from .contracts import Rejected, canonical, digest, redact

SCHEMA = json.loads((Path(__file__).resolve().parents[1] / 'contracts/jira-comment-intent-v1.schema.json').read_text())
INTENT = Draft202012Validator(SCHEMA)
TARGET = Draft202012Validator(SCHEMA['$defs']['target'])


def _target(value):
    TARGET.validate(value)
    for key in ('project_id', 'issue_id', 'project_key', 'issue_key'):
        if not re.fullmatch(SCHEMA['$defs']['target']['properties'][key]['pattern'], value[key]):
            raise Rejected('Invalid Jira target identity')
    base = value['base_url']
    url = urlsplit(base)
    # Conservative configured HTTPS instance/context path; never resolve or discover it.
    if (url.scheme != 'https' or url.query or url.fragment or url.username or url.password
            or not url.hostname or not re.fullmatch(r'[a-z0-9]+(?:[.-][a-z0-9]+)*', url.hostname)
            or url.netloc != url.hostname + (':' + str(url.port) if url.port is not None else '')
            or (url.port is not None and not 1 <= url.port <= 65535)
            or not re.fullmatch(r'(?:/[A-Za-z0-9_-]+)*', url.path)
            or base != 'https://' + url.netloc + url.path
            or value['issue_key'].rsplit('-', 1)[0] != value['project_key']):
        raise Rejected('Unsupported Jira target')
    return base + '/rest/api/2/issue/' + value['issue_id']


def _text(value, maximum, multiline=False):
    if (not isinstance(value, str) or len(value.encode('utf-8')) > maximum
            or any(ord(c) < 32 and (not multiline or c not in '\n\t') or ord(c) == 127 for c in value)):
        raise Rejected('Invalid Jira text')
    return value


def review_issue(target, observations):
    try:
        issue_url = _target(target)
        read_request = {'method': 'GET', 'url': issue_url + '?fields=project,summary,description,updated',
                        'headers': {'Accept': 'application/json'}}
        if (not isinstance(observations, dict) or set(observations) != {'url', 'redirected', 'status', 'body'}
                or type(observations['status']) is not int or not 200 <= observations['status'] <= 599
                or type(observations['redirected']) is not bool or len(canonical(observations)) > 65536):
            raise Rejected('Invalid Jira observation envelope')
        blockers, issue = [], None
        if observations['url'] != read_request['url'] or observations['redirected']:
            blockers.append('response_origin_mismatch')
        if observations['status'] != 200:
            blockers.append('issue_unavailable')
        if not blockers:
            body = observations['body']
            if not isinstance(body, dict) or not isinstance(body.get('fields'), dict):
                raise Rejected('Invalid Jira issue response')
            fields = body['fields']
            project = fields.get('project')
            if body.get('id') != target['issue_id'] or body.get('key') != target['issue_key'] or body.get('self') != issue_url:
                blockers.append('issue_identity_mismatch')
            if (not isinstance(project, dict) or project.get('id') != target['project_id']
                    or project.get('key') != target['project_key']
                    or project.get('self') != target['base_url'] + '/rest/api/2/project/' + target['project_id']):
                blockers.append('project_identity_mismatch')
            if not blockers:
                summary = _text(fields['summary'], 1024)
                description = '' if fields['description'] is None else _text(fields['description'], 16384, True)
                updated = _text(fields['updated'], 40)
                if not summary.strip() or datetime.fromisoformat(updated).utcoffset() is None:
                    raise Rejected('Invalid Jira summary or update time')
                issue = {'summary': redact(summary), 'description': redact(description), 'updated': updated,
                         'provenance': 'untrusted_external_issue',
                         'recognized_secrets_redacted': redact(summary) != summary or redact(description) != description}
        binding = {'schema_version': '1.0.0', 'target': target, 'observations_sha256': digest(observations),
                   'read_request': read_request, 'issue': issue, 'blockers': blockers}
        return {'kind': 'JiraIssueSnapshot', 'binding': binding, 'sha256': digest(binding),
                'status': 'blocked' if blockers else 'matches', 'remote_issue_verified': False, 'live_authorized': False}
    except (ValidationError, ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        raise Rejected('Invalid Jira issue review') from exc


def prepare_comment(intent, allowed_target, observations):
    try:
        INTENT.validate(intent)
        _target(allowed_target)
        if canonical(intent['target']) != canonical(allowed_target) or len(canonical(intent)) > 16384:
            raise Rejected('Jira comment target mismatch or budget exceeded')
        # JSON Schema anchors alone permit a final newline in some regular-expression engines.
        for key, size in (('task_id', 32), ('operation_id', 32), ('snapshot_sha256', 64)):
            if not re.fullmatch('[a-f0-9]{' + str(size) + '}', intent[key]):
                raise Rejected('Invalid Jira comment identity')
        snapshot = review_issue(allowed_target, observations)
        if snapshot['status'] != 'matches' or snapshot['sha256'] != intent['snapshot_sha256']:
            raise Rejected('Jira comment requires the exact matching issue snapshot')
        message = _text(intent['body'], 4096, True)
        if not message.strip() or redact(message) != message:
            raise Rejected('Comment is empty or contains recognized secrets')
        body = {'body': message}
        if intent['visibility'] is not None:
            name = _text(intent['visibility']['value'], 128)
            if not name.strip() or redact(name) != name:
                raise Rejected('Invalid visibility')
            body['visibility'] = intent['visibility']
        request = {'method': 'POST', 'url': _target(allowed_target) + '/comment',
                   'headers': {'Accept': 'application/json', 'Content-Type': 'application/json'}, 'body': body}
        binding = {'schema_version': '1.0.0', 'task_id': intent['task_id'], 'operation_id': intent['operation_id'],
                   'target': allowed_target, 'snapshot_sha256': snapshot['sha256'],
                   'issue_updated': snapshot['binding']['issue']['updated'], 'request': request}
        return {'kind': 'JiraCommentPreview', 'binding': binding, 'sha256': digest(binding),
                'remote_issue_verified': False, 'live_authorized': False, 'retry_allowed': False}
    except (ValidationError, ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        raise Rejected('Invalid Jira comment preview') from exc
