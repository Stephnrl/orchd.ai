"""Synthetic REST fixtures; no corporate configuration or credentials."""
import copy

from orch.jira_actions import read_plan, recovery_plan


def profile():
    return {'schema_version': '1.0.0', 'codec': 'jira-rest-8.5', 'base_url': 'https://jira.example.test/jira',
            'project_id': '100', 'project_key': 'ENG', 'issue_types': {'epic': '10', 'story': '11', 'task': '12'},
            'epic_name_field': 'customfield_10001', 'epic_link_field': 'customfield_10002', 'github_repository': 'example/project'}


def intent(action='create_issue', number=1):
    parameters = {
        'create_issue': {'issue_type': 'story', 'summary': 'Fixture story', 'description': 'Synthetic Jira notes.', 'epic_name': None, 'epic': {'id': '300', 'key': 'ENG-9'}},
        'transition': {'transition_id': '20', 'from_status_id': '1', 'to_status_id': '2'},
        'issue_link': {'other_issue': {'id': '400', 'key': 'ENG-10'}, 'link_type_id': '30', 'direction': 'outward'},
        'epic_link': {'epic': {'id': '300', 'key': 'ENG-9'}, 'expected_epic_key': None},
        'github_link': {'kind': 'pull', 'number': 5, 'title': 'Review fixture PR'}}[action]
    return {'schema_version': '1.0.0', 'task_id': format(number, '032x'), 'operation_id': format(number+100, '032x'),
            'action': action, 'issue': None if action == 'create_issue' else {'id': '200', 'key': 'ENG-7'}, 'parameters': parameters}


def issue_body(config, issue, issue_type=None):
    base = config['base_url']+'/rest/api/2'
    return {**issue, 'self': base+'/issue/'+issue['id'], 'fields': {
        'project': {'id': config['project_id'], 'key': config['project_key'], 'self': base+'/project/'+config['project_id']},
        'issuetype': {'id': issue_type or config['issue_types']['story']}, 'status': {'id': '1'},
        'summary': 'Fixture story', 'description': 'Synthetic Jira notes.', 'updated': '2026-09-14T12:00:00Z',
        'issuelinks': [], 'labels': [], config['epic_link_field']: None, config['epic_name_field']: None}}


def envelope(read, body):
    return {'url': read['url'], 'redirected': False, 'status': 200, 'body': body}


def capture_from(plan, bodies):
    return {'schema_version': '1.0.0', 'plan_sha256': plan['sha256'],
            'responses': {key: envelope(read, bodies[key]) for key, read in plan['binding']['requests'].items()}}


def capture(proposal, config):
    plan = read_plan(proposal, config)
    params = proposal['parameters']
    bodies = {}
    for key in plan['binding']['requests']:
        if key == 'issue': bodies[key] = issue_body(config, proposal['issue'])
        elif key == 'epic': bodies[key] = issue_body(config, params['epic'], config['issue_types']['epic'])
        elif key == 'other_issue': bodies[key] = issue_body(config, params['other_issue'])
        elif key == 'transitions': bodies[key] = {'transitions': [{'id': '20', 'to': {'id': '2'}, 'fields': {}}]}
        elif key == 'link_types': bodies[key] = {'issueLinkTypes': [{'id': '30', 'name': 'Relates', 'inward': 'relates to', 'outward': 'relates to'}]}
        elif key == 'remote_links': bodies[key] = []
        elif key == 'edit_metadata': bodies[key] = {'fields': {config['epic_link_field']: {'operations': ['set'], 'schema': {'type': 'any', 'custom': 'com.pyxis.greenhopper.jira:gh-epic-link'}}}}
        elif key == 'metadata':
            fields = {name: {'required': name in ('project', 'issuetype', 'summary'), 'operations': ['set'],
                             'schema': {'type': 'string'}} for name in ('project', 'issuetype', 'summary', 'description', 'labels', config['epic_name_field'], config['epic_link_field'])}
            fields['labels']['schema'] = {'type': 'array', 'items': 'string'}
            fields['project']['schema'] = {'type': 'project'}
            fields['issuetype']['schema'] = {'type': 'issuetype'}
            fields[config['epic_name_field']]['schema'] = {'type': 'string', 'custom': 'com.pyxis.greenhopper.jira:gh-epic-label'}
            fields[config['epic_link_field']]['schema'] = {'type': 'any', 'custom': 'com.pyxis.greenhopper.jira:gh-epic-link'}
            bodies[key] = {'projects': [{'id': config['project_id'], 'key': config['project_key'], 'issuetypes': [
                {'id': config['issue_types'][params['issue_type']], 'subtask': False, 'fields': fields}]}]}
    return capture_from(plan, bodies)


def recovery_capture(scope):
    plan = recovery_plan(scope)
    proposal, config = scope['intent'], scope['profile']
    action, params = proposal['action'], proposal['parameters']
    if action == 'create_issue':
        body = issue_body(config, {'id': '500', 'key': 'ENG-11'}, config['issue_types'][params['issue_type']])
        expected = copy.deepcopy(scope['preview']['binding']['request']['body']['fields'])
        body['fields'].update({key: value for key, value in expected.items() if key not in ('project', 'issuetype')})
        bodies = {'search': {'startAt': 0, 'maxResults': 2, 'total': 1, 'issues': [body]}}
    else:
        body = issue_body(config, proposal['issue'])
        bodies = {'issue': body}
        if action == 'transition': body['fields']['status']['id'] = params['to_status_id']
        elif action == 'epic_link': body['fields'][config['epic_link_field']] = params['epic']['key']
        elif action == 'issue_link':
            key = 'outwardIssue' if params['direction'] == 'outward' else 'inwardIssue'
            body['fields']['issuelinks'] = [{'id': '88', 'type': {'id': params['link_type_id']}, key: params['other_issue']}]
        elif action == 'github_link':
            bodies['remote_links'] = [{'id': 89, **scope['preview']['binding']['request']['body']}]
    return capture_from(plan, bodies)


def preflight_capture(plan, scope):
    source = scope['capture']['responses'] if scope.get('kind') == 'JiraActionScope' else {'issue': scope['issue_observations']}
    bodies = {key: copy.deepcopy(value['body']) for key, value in source.items()}
    bodies['identity'] = {'key': 'author-key', 'active': True}
    bodies['permissions'] = {'permissions': {key: {'key': key, 'havePermission': True} for key in plan['binding']['required_permissions']}}
    return capture_from(plan, bodies)
