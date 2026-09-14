"""Offline comment recovery assessment; never adopt a comment or authorize a retry."""
import re

from .contracts import Rejected, canonical, digest, redact
from .jira_preview import prepare_comment, _text

MAX_PAGES = 10
PAGE_SIZE = 50
MAX_CAPTURE_BYTES = 65536


def prepare_comment_read_plan(intent, target, issue_observations, expected_preview_sha256, author_key):
    preview = prepare_comment(intent, target, issue_observations)
    if preview['sha256'] != expected_preview_sha256:
        raise Rejected('Expected Jira comment preview required')
    _text(author_key, 256)
    if not author_key.strip() or redact(author_key) != author_key:
        raise Rejected('Explicit Jira author key required')
    binding = {'schema_version': '1.0.0', 'preview_sha256': preview['sha256'], 'target': target,
               'task_id': intent['task_id'], 'operation_id': intent['operation_id'], 'author_key': author_key,
               'first_request': {'method': 'GET', 'url': preview['binding']['request']['url'] + '?startAt=0&maxResults=50&orderBy=created',
                                 'headers': {'Accept': 'application/json'}},
               'limits': {'pages': MAX_PAGES, 'page_size': PAGE_SIZE, 'capture_bytes': MAX_CAPTURE_BYTES}}
    return {'kind': 'JiraCommentReadPlan', 'binding': binding, 'sha256': digest(binding),
            'remote_effect_confirmed': False, 'live_authorized': False, 'retry_allowed': False}


def _request(plan, offset):
    first = plan['binding']['first_request']
    return {**first, 'url': first['url'].split('?', 1)[0] + f'?startAt={offset}&maxResults=50&orderBy=created'}


def assess_comments(intent, target, issue_observations, expected_preview_sha256, author_key, capture):
    try:
        plan = prepare_comment_read_plan(intent, target, issue_observations, expected_preview_sha256, author_key)
        if (not isinstance(capture, dict) or set(capture) != {'kind', 'schema_version', 'plan_sha256', 'pages'}
                or capture['kind'] != 'JiraCommentCapture' or capture['schema_version'] != '1.0.0'
                or capture['plan_sha256'] != plan['sha256'] or len(canonical(capture)) > MAX_CAPTURE_BYTES
                or not isinstance(capture['pages'], list) or not 1 <= len(capture['pages']) <= MAX_PAGES):
            raise Rejected('Invalid Jira comment capture')
        blockers, candidates, ids, requests = [], [], set(), []
        offset, total, finished = 0, None, False
        for page in capture['pages']:
            if finished:
                raise Rejected('Read beyond the end of comment pagination')
            request = _request(plan, offset)
            requests.append(request)
            if (not isinstance(page, dict) or set(page) != {'url', 'redirected', 'status', 'body'}
                    or page['url'] != request['url'] or page['redirected'] is not False
                    or type(page['status']) is not int or not 200 <= page['status'] <= 599):
                raise Rejected('Invalid Jira comment response identity')
            if page['status'] != 200:
                blockers.append('page_unavailable')
                finished = True
                continue
            body = page['body']
            if (not isinstance(body, dict) or not {'startAt','maxResults','total','comments'} <= set(body)
                    or any(type(body[k]) is not int for k in ('startAt','maxResults','total'))
                    or body['startAt'] != offset or not 1 <= body['maxResults'] <= PAGE_SIZE
                    or not 0 <= body['total'] <= 1000000 or not isinstance(body['comments'], list)
                    or len(body['comments']) > body['maxResults']):
                raise Rejected('Invalid Jira comment pagination')
            if total is None:
                total = body['total']
            elif total != body['total']:
                blockers.append('comment_total_changed')
            rows = body['comments']
            if offset + len(rows) > body['total']:
                blockers.append('pagination_inconsistent')
            for comment in rows:
                if not isinstance(comment, dict):
                    raise Rejected('Invalid comment')
                comment_id, author = comment.get('id'), comment.get('author')
                if (not isinstance(comment_id, str) or not re.fullmatch('[1-9][0-9]{0,14}', comment_id)
                        or comment.get('self') != request['url'].split('?',1)[0] + '/' + comment_id
                        or not isinstance(author, dict) or not isinstance(author.get('key'), str)
                        or not author['key'].strip() or not isinstance(comment.get('body'), str)):
                    blockers.append('comment_identity_or_content_malformed')
                    continue
                if comment_id in ids:
                    blockers.append('duplicate_comment_id')
                ids.add(comment_id)
                visibility = comment.get('visibility')
                if (author['key'] == author_key and comment['body'] == intent['body']
                        and canonical(visibility) == canonical(intent['visibility'])):
                    candidates.append({'id':comment_id, 'url':comment['self']})
            offset += len(rows)
            finished = offset >= body['total'] or not rows
        if total is None or offset != total or not finished:
            blockers.append('pagination_incomplete')
        if len(candidates) != 1:
            blockers.append('no_matching_comment' if not candidates else 'multiple_matching_comments')
        binding = {'schema_version': '1.0.0', 'plan_sha256': plan['sha256'], 'preview_sha256': expected_preview_sha256,
                   'capture_sha256': digest(capture), 'target': target, 'author_key': author_key,
                   'pages_checked': len(requests), 'comments_checked': offset, 'declared_total': total,
                   'read_requests': requests, 'blockers': sorted(set(blockers)), 'candidate': None if blockers else candidates[0]}
        return {'kind': 'JiraCommentReconciliation', 'binding': binding, 'sha256': digest(binding),
                'status': 'unresolved' if blockers else 'candidate_observed', 'remote_effect_confirmed': False,
                'live_authorized': False, 'retry_allowed': False}
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        raise Rejected('Invalid Jira comment reconciliation') from exc
