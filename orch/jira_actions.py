"""Bounded Jira REST v2 action codecs; request data only, never live dispatch."""
from datetime import datetime
from functools import wraps
import re
from urllib.parse import urlencode
from jsonschema import ValidationError

from .contracts import Rejected, canonical, digest, redact
from .jira_preview import _target, _text

FLAGS = {'live_authorized': False, 'retry_allowed': False, 'remote_effect_confirmed': False}
ACTIONS = ('create_issue', 'transition', 'issue_link', 'github_link', 'epic_link')
MAX_BYTES = 65536


def guarded(function):
    @wraps(function)
    def run(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (ValidationError, KeyError, TypeError, ValueError, AttributeError, OverflowError, RecursionError) as exc:
            raise Rejected('Invalid Jira action inputs') from exc
    return run


def closed(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise Rejected('Unsupported Jira object shape')


def identifier(value, pattern='[1-9][0-9]{0,14}'):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise Rejected('Invalid Jira identifier')
    return value


def text_value(value, size=256, multiline=False):
    _text(value, size, multiline)
    if not value.strip() or redact(value) != value:
        raise Rejected('Invalid Jira proposal text')
    return value


@guarded
def validate_profile(profile):
    closed(profile, ('schema_version', 'codec', 'base_url', 'project_id', 'project_key',
                     'issue_types', 'epic_name_field', 'epic_link_field', 'github_repository'))
    if profile['schema_version'] != '1.0.0' or profile['codec'] != 'jira-rest-8.5':
        raise Rejected('Unsupported Jira codec profile')
    target = {key: profile[key] for key in ('base_url', 'project_id', 'project_key')}
    _target({**target, 'issue_id': '1', 'issue_key': profile['project_key']+'-1'})
    closed(profile['issue_types'], ('epic', 'story', 'task'))
    for value in profile['issue_types'].values(): identifier(value)
    if len(set(profile['issue_types'].values())) != 3:
        raise Rejected('Issue type mappings must be distinct')
    for key in ('epic_name_field', 'epic_link_field'):
        identifier(profile[key], 'customfield_[1-9][0-9]{0,14}')
    if profile['epic_name_field'] == profile['epic_link_field']:
        raise Rejected('Epic field mappings must be distinct')
    identifier(profile['github_repository'], r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}')
    if len(canonical(profile)) > 4096:
        raise Rejected('Jira profile exceeds budget')
    return target


def issue_target(profile, issue):
    closed(issue, ('id', 'key'))
    target = {key: profile[key] for key in ('base_url', 'project_id', 'project_key')}
    target.update(issue_id=issue['id'], issue_key=issue['key'])
    _target(target)
    return target


def validate_intent(intent, profile):
    validate_profile(profile)
    closed(intent, ('schema_version', 'task_id', 'operation_id', 'action', 'issue', 'parameters'))
    if intent['schema_version'] != '1.0.0' or intent['action'] not in ACTIONS or len(canonical(intent)) > 16384:
        raise Rejected('Unsupported Jira action intent')
    for key in ('task_id', 'operation_id'): identifier(intent[key], '[a-f0-9]{32}')
    action, params = intent['action'], intent['parameters']
    if action == 'create_issue':
        if intent['issue'] is not None:
            raise Rejected('Creation cannot target an existing issue')
        closed(params, ('issue_type', 'summary', 'description', 'epic_name', 'epic'))
        if params['issue_type'] not in profile['issue_types']:
            raise Rejected('Unsupported issue type')
        text_value(params['summary'], 255)
        text_value(params['description'], 4096, True)
        if params['issue_type'] == 'epic':
            text_value(params['epic_name'], 255)
            if params['epic'] is not None: raise Rejected('Nested Epics are unsupported')
        elif params['epic_name'] is not None:
            raise Rejected('Epic name applies only to Epics')
        if params['epic'] is not None: issue_target(profile, params['epic'])
    else:
        issue_target(profile, intent['issue'])
        if action == 'transition':
            closed(params, ('transition_id', 'from_status_id', 'to_status_id'))
            for value in params.values(): identifier(value)
            if params['from_status_id'] == params['to_status_id']:
                raise Rejected('Self transitions are unsupported')
        elif action == 'issue_link':
            closed(params, ('other_issue', 'link_type_id', 'direction'))
            issue_target(profile, params['other_issue'])
            identifier(params['link_type_id'])
            if params['direction'] not in ('inward', 'outward'):
                raise Rejected('Explicit link direction required')
            if params['other_issue']['id'] == intent['issue']['id']:
                raise Rejected('Self links are unsupported')
        elif action == 'epic_link':
            closed(params, ('epic', 'expected_epic_key'))
            issue_target(profile, params['epic'])
            if params['epic']['id'] == intent['issue']['id']:
                raise Rejected('Self Epic relationship is unsupported')
            # First attachment only; implicit reparenting would overwrite existing work.
            if params['expected_epic_key'] is not None:
                raise Rejected('Epic reparenting is unsupported')
        elif action == 'github_link':
            closed(params, ('kind', 'number', 'title'))
            if params['kind'] not in ('pull', 'issues') or type(params['number']) is not int or not 0 < params['number'] <= 2147483647:
                raise Rejected('Explicit GitHub issue or PR required')
            text_value(params['title'], 255)
    return intent


def request(url, method='GET', body=None):
    result = {'method': method, 'url': url, 'headers': {'Accept': 'application/json'}}
    if body is not None:
        result['headers']['Content-Type'] = 'application/json'
        result['body'] = body
    return result


def issue_request(profile, issue):
    url = _target(issue_target(profile, issue))
    fields = 'project,issuetype,summary,description,status,updated,issuelinks,labels,'+profile['epic_link_field']+','+profile['epic_name_field']
    return request(url+'?fields='+fields)


@guarded
def read_plan(intent, profile):
    validate_intent(intent, profile)
    action, params, base = intent['action'], intent['parameters'], profile['base_url']+'/rest/api/2'
    reads = {}
    if intent['issue'] is not None:
        reads['issue'] = issue_request(profile, intent['issue'])
    if action == 'create_issue':
        reads['metadata'] = request(base+'/issue/createmeta?projectIds='+profile['project_id']+'&issuetypeIds='+
                                    profile['issue_types'][params['issue_type']]+'&expand=projects.issuetypes.fields')
    if action in ('create_issue', 'epic_link') and params.get('epic') is not None:
        reads['epic'] = issue_request(profile, params['epic'])
    if action == 'transition':
        reads['transitions'] = request(_target(issue_target(profile, intent['issue']))+'/transitions?expand=transitions.fields')
    if action == 'epic_link':
        reads['edit_metadata'] = request(_target(issue_target(profile, intent['issue']))+'/editmeta')
    if action == 'issue_link':
        reads['other_issue'] = issue_request(profile, params['other_issue'])
        reads['link_types'] = request(base+'/issueLinkType')
    if action == 'github_link':
        reads['remote_links'] = request(_target(issue_target(profile, intent['issue']))+'/remotelink')
    binding = {'schema_version': '1.0.0', 'intent_sha256': digest(intent), 'profile_sha256': digest(profile), 'requests': reads}
    return {'kind': 'JiraActionReadPlan', 'binding': binding, 'sha256': digest(binding), **FLAGS}


def responses(plan, capture):
    closed(capture, ('schema_version', 'plan_sha256', 'responses'))
    if capture['schema_version'] != '1.0.0' or capture['plan_sha256'] != plan['sha256'] or len(canonical(capture)) > MAX_BYTES:
        raise Rejected('Jira capture binding or budget mismatch')
    closed(capture['responses'], plan['binding']['requests'])
    bodies = {}
    for key, read in plan['binding']['requests'].items():
        response = capture['responses'][key]
        closed(response, ('url', 'redirected', 'status', 'body'))
        if response['url'] != read['url'] or response['redirected'] is not False or type(response['status']) is not int or response['status'] != 200:
            raise Rejected('Jira captured read unavailable or origin changed')
        bodies[key] = response['body']
    return bodies


def checked_issue(profile, issue, body):
    target = issue_target(profile, issue)
    if not isinstance(body, dict) or (body.get('id'), body.get('key'), body.get('self')) != (issue['id'], issue['key'], _target(target)):
        raise Rejected('Jira issue identity mismatch')
    fields = body.get('fields')
    if not isinstance(fields, dict) or not isinstance(fields.get('project'), dict):
        raise Rejected('Jira issue fields missing')
    project = fields['project']
    if (project.get('id'), project.get('key'), project.get('self')) != (profile['project_id'], profile['project_key'], profile['base_url']+'/rest/api/2/project/'+profile['project_id']):
        raise Rejected('Jira project identity mismatch')
    if not isinstance(fields.get('issuetype'), dict) or not isinstance(fields.get('status'), dict):
        raise Rejected('Jira issue type/status missing')
    identifier(fields['issuetype'].get('id'))
    identifier(fields['status'].get('id'))
    updated = _text(fields.get('updated'), 40)
    if datetime.fromisoformat(updated).utcoffset() is None:
        raise Rejected('Jira update time requires timezone')
    _text(fields.get('summary'), 1024)
    _text('' if fields.get('description') is None else fields['description'], 16384, True)
    return fields


def _epic_field(meta, field, link):
    schema = meta.get('schema', {})
    expected = 'com.pyxis.greenhopper.jira:'+('gh-epic-link' if link else 'gh-epic-label')
    if (schema.get('custom') != expected or schema.get('type') not in (('any', 'string') if link else ('string',))
            or ('customId' in schema and (type(schema['customId']) is not int or schema['customId'] != int(field.split('_')[1])))):
        raise Rejected('Configured Epic field does not match the expected Jira field type')


def _metadata(profile, params, body, payload):
    if not isinstance(body, dict) or not isinstance(body.get('projects'), list) or len(body['projects']) != 1:
        raise Rejected('Exact Jira create metadata required')
    project = body['projects'][0]
    if (project.get('id'), project.get('key')) != (profile['project_id'], profile['project_key']):
        raise Rejected('Jira metadata project mismatch')
    types = project.get('issuetypes')
    if not isinstance(types, list) or len(types) != 1 or types[0].get('id') != profile['issue_types'][params['issue_type']] or types[0].get('subtask') is not False:
        raise Rejected('Jira create issue type mismatch')
    fields = types[0].get('fields')
    if not isinstance(fields, dict) or not set(payload) <= set(fields):
        raise Rejected('Jira create fields unavailable')
    for name, meta in fields.items():
        if not isinstance(meta, dict) or type(meta.get('required')) is not bool:
            raise Rejected('Jira create field metadata invalid')
        if meta['required'] and name not in payload:
            raise Rejected('Unsupported required Jira create field')
        if name in payload and (not isinstance(meta.get('operations'), list) or 'set' not in meta['operations']):
            raise Rejected('Jira create field cannot be set')
        if name in payload and name in (profile['epic_name_field'], profile['epic_link_field']):
            _epic_field(meta, name, name == profile['epic_link_field'])
        elif name in payload and isinstance(payload[name], str):
            if meta.get('schema', {}).get('type') != 'string':
                raise Rejected('Configured Jira string field has another type')
        if name == 'labels' and (meta.get('schema', {}).get('type') != 'array' or meta.get('schema', {}).get('items') != 'string'):
            raise Rejected('Jira operation marker requires a string label array')
        if name in ('project', 'issuetype') and meta.get('schema', {}).get('type') != name:
            raise Rejected('Jira metadata identity field type mismatch')


def _links(value):
    if not isinstance(value, list) or len(value) > 100:
        raise Rejected('Jira links exceed supported shape/budget')
    if any(not isinstance(row, dict) for row in value):
        raise Rejected('Invalid Jira link record')
    ids = [row.get('id') for row in value]
    if (any(not isinstance(value, (str, int)) or isinstance(value, bool) or not re.fullmatch('[1-9][0-9]{0,14}', str(value)) for value in ids)
            or len({str(value) for value in ids}) != len(ids)):
        raise Rejected('Invalid or duplicate Jira link IDs')
    return value


def preview_action(intent, profile, capture):
    try:
        plan = read_plan(intent, profile)
        bodies = responses(plan, capture)
        params, action = intent['parameters'], intent['action']
        base = profile['base_url']+'/rest/api/2'
        target = validate_profile(profile)
        fields = None
        if intent['issue'] is not None:
            target = issue_target(profile, intent['issue'])
            fields = checked_issue(profile, intent['issue'], bodies['issue'])
        if 'epic' in bodies:
            epic = checked_issue(profile, params['epic'], bodies['epic'])
            if epic['issuetype']['id'] != profile['issue_types']['epic']:
                raise Rejected('Explicit parent is not the configured Epic type')
        if action == 'create_issue':
            payload = {'project': {'id': profile['project_id']}, 'issuetype': {'id': profile['issue_types'][params['issue_type']]},
                       'summary': params['summary'], 'description': params['description'], 'labels': ['orchd-op-'+intent['operation_id']]}
            if params['epic_name'] is not None: payload[profile['epic_name_field']] = params['epic_name']
            if params['epic'] is not None: payload[profile['epic_link_field']] = params['epic']['key']
            _metadata(profile, params, bodies['metadata'], payload)
            write = request(base+'/issue', 'POST', {'fields': payload})
        elif action == 'transition':
            if fields['status']['id'] != params['from_status_id']:
                raise Rejected('Jira source status changed')
            transitions = bodies['transitions'].get('transitions')
            if not isinstance(transitions, list) or len(transitions) > 100:
                raise Rejected('Invalid Jira transition list')
            matches = [row for row in transitions if isinstance(row, dict) and row.get('id') == params['transition_id']]
            if len(matches) != 1 or matches[0].get('to', {}).get('id') != params['to_status_id']:
                raise Rejected('Expected transition/destination unavailable')
            transition_fields = matches[0].get('fields')
            if not isinstance(transition_fields, dict) or any(not isinstance(meta, dict) or meta.get('required') is not False for meta in transition_fields.values()):
                raise Rejected('Transition requires unsupported fields')
            write = request(_target(target)+'/transitions', 'POST', {'transition': {'id': params['transition_id']}})
        elif action == 'epic_link':
            if fields['issuetype']['id'] not in (profile['issue_types']['story'], profile['issue_types']['task']):
                raise Rejected('Only configured Story/Task types can attach to an Epic')
            if profile['epic_link_field'] not in fields or fields[profile['epic_link_field']] is not None:
                raise Rejected('Epic relationship missing or already populated')
            edit = bodies['edit_metadata'].get('fields', {}).get(profile['epic_link_field'])
            if not isinstance(edit, dict) or 'set' not in edit.get('operations', []):
                raise Rejected('Configured Epic field cannot be edited')
            _epic_field(edit, profile['epic_link_field'], True)
            write = request(_target(target), 'PUT', {'fields': {profile['epic_link_field']: params['epic']['key']}})
        elif action == 'issue_link':
            checked_issue(profile, params['other_issue'], bodies['other_issue'])
            types = bodies['link_types'].get('issueLinkTypes')
            if not isinstance(types, list) or len(types) > 100:
                raise Rejected('Invalid Jira link types')
            matches = [row for row in types if isinstance(row, dict) and row.get('id') == params['link_type_id']]
            if len(matches) != 1: raise Rejected('Link type unavailable or ambiguous')
            for key in ('name', 'inward', 'outward'): text_value(matches[0].get(key))
            links = _links(fields.get('issuelinks'))
            if any(row.get('type', {}).get('id') == params['link_type_id'] and
                   any(row.get(key, {}).get('id') == params['other_issue']['id'] for key in ('inwardIssue', 'outwardIssue')) for row in links):
                raise Rejected('Relationship already exists; use recovery')
            source, other = {'id': intent['issue']['id']}, {'id': params['other_issue']['id']}
            inward, outward = (source, other) if params['direction'] == 'outward' else (other, source)
            write = request(base+'/issueLink', 'POST', {'type': {'id': params['link_type_id']}, 'inwardIssue': inward, 'outwardIssue': outward})
        else:
            links = _links(bodies['remote_links'])
            url = 'https://github.com/'+profile['github_repository']+'/'+params['kind']+'/'+str(params['number'])
            global_id = 'orchd:'+intent['operation_id']
            if any(row.get('globalId') == global_id or row.get('object', {}).get('url') == url for row in links):
                raise Rejected('Remote association already exists; no implicit update')
            write = request(_target(target)+'/remotelink', 'POST', {'globalId': global_id,
                            'relationship': 'relates to', 'object': {'url': url, 'title': params['title']}})
        binding = {'schema_version': '1.0.0', 'task_id': intent['task_id'], 'operation_id': intent['operation_id'],
                   'action': action, 'target': target, 'intent_sha256': digest(intent), 'profile_sha256': digest(profile),
                   'capture_sha256': digest(capture), 'read_plan_sha256': plan['sha256'],
                   'issue_updated': fields['updated'] if fields else None, 'request': write}
        return {'kind': 'JiraActionPreview', 'binding': binding, 'sha256': digest(binding), **FLAGS}
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError) as exc:
        raise Rejected('Invalid Jira action preparation') from exc


def action_scope(intent, profile, capture, expected_sha256):
    preview = preview_action(intent, profile, capture)
    if preview['sha256'] != expected_sha256:
        raise Rejected('Expected Jira action preview required')
    return {'kind': 'JiraActionScope', 'schema_version': '1.0.0', 'intent': intent, 'profile': profile,
            'capture': capture, 'preview': preview, 'preview_sha256': expected_sha256,
            'target': preview['binding']['target'], 'read_plan': read_plan(intent, profile)}


@guarded
def recovery_plan(scope):
    expected = action_scope(scope['intent'], scope['profile'], scope['capture'], scope['preview_sha256'])
    if canonical(scope) != canonical(expected):
        raise Rejected('Invalid retained Jira action scope')
    intent, profile, preview = scope['intent'], scope['profile'], scope['preview']
    if intent['action'] == 'create_issue':
        query = 'project = '+profile['project_id']+' AND labels = "orchd-op-'+intent['operation_id']+'"'
        fields = 'project,issuetype,summary,description,status,updated,labels,'+profile['epic_link_field']+','+profile['epic_name_field']
        reads = {'search': request(profile['base_url']+'/rest/api/2/search?'+urlencode({'jql': query, 'fields': fields, 'startAt': 0, 'maxResults': 2}))}
    elif intent['action'] == 'github_link':
        reads = {'issue': issue_request(profile, intent['issue']),
                 'remote_links': request(_target(issue_target(profile, intent['issue']))+'/remotelink')}
    else:
        reads = {'issue': issue_request(profile, intent['issue'])}
    binding = {'schema_version': '1.0.0', 'scope_sha256': digest(scope), 'preview_sha256': preview['sha256'], 'requests': reads}
    return {'kind': 'JiraActionRecoveryPlan', 'binding': binding, 'sha256': digest(binding), **FLAGS}


def reconcile_action(scope, capture):
    try:
        plan = recovery_plan(scope)
        bodies = responses(plan, capture)
        intent, profile = scope['intent'], scope['profile']
        params, action = intent['parameters'], intent['action']
        candidates, blockers = [], []
        if action == 'create_issue':
            search = bodies['search']
            if (not isinstance(search, dict) or any(type(search.get(k)) is not int for k in ('startAt', 'maxResults', 'total'))
                    or search['startAt'] != 0 or not 1 <= search['maxResults'] <= 2 or not isinstance(search.get('issues'), list)
                    or search['total'] < 0 or len(search['issues']) > search['maxResults']):
                raise Rejected('Invalid Jira creation recovery search')
            if search['total'] != len(search['issues']): blockers.append('search_incomplete')
            seen = set()
            for body in search['issues']:
                issue = {'id': body['id'], 'key': body['key']}
                fields = checked_issue(profile, issue, body)
                if issue['id'] in seen: blockers.append('duplicate_issue_id')
                seen.add(issue['id'])
                expected = scope['preview']['binding']['request']['body']['fields']
                if (fields['issuetype']['id'] == expected['issuetype']['id'] and
                        all(fields.get(k) == v for k, v in expected.items() if k not in ('project', 'issuetype'))):
                    candidates.append(issue)
        else:
            fields = checked_issue(profile, intent['issue'], bodies['issue'])
            if action == 'transition' and fields['status']['id'] == params['to_status_id']:
                candidates.append(intent['issue'])
            elif action == 'epic_link' and fields.get(profile['epic_link_field']) == params['epic']['key']:
                candidates.append(intent['issue'])
            elif action == 'issue_link':
                key = 'outwardIssue' if params['direction'] == 'outward' else 'inwardIssue'
                for row in _links(fields.get('issuelinks')):
                    if row.get('type', {}).get('id') == params['link_type_id'] and all(row.get(key, {}).get(k) == v for k, v in params['other_issue'].items()):
                        candidates.append({'link_id': str(row['id'])})
            elif action == 'github_link':
                expected = scope['preview']['binding']['request']['body']
                for row in _links(bodies['remote_links']):
                    if all(row.get(key) == value for key, value in expected.items()):
                        candidates.append({'link_id': str(row['id'])})
        if len(candidates) != 1: blockers.append('no_unique_candidate')
        binding = {'schema_version': '1.0.0', 'scope_sha256': digest(scope), 'plan_sha256': plan['sha256'],
                   'capture_sha256': digest(capture), 'blockers': blockers, 'candidate': None if blockers else candidates[0]}
        return {'kind': 'JiraActionReconciliation', 'binding': binding, 'sha256': digest(binding),
                'status': 'unresolved' if blockers else 'candidate_observed', **FLAGS}
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError) as exc:
        raise Rejected('Invalid Jira action recovery') from exc
