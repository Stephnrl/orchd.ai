import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from orch.contracts import Rejected, canonical, digest
from orch.jira_preview import review_issue, prepare_comment
from orch.jira_reconcile import prepare_comment_read_plan, assess_comments, _request
from test_jira_preview import target, observation, intent


class JiraCommentReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.target, self.issue = target(), observation()
        self.intent = intent(review_issue(self.target,self.issue))
        self.preview = prepare_comment(self.intent,self.target,self.issue)
        self.args = (self.intent,self.target,self.issue,self.preview['sha256'],'author-key')
        self.plan = prepare_comment_read_plan(*self.args)

    def comment(self, number='301', **fields):
        return {'id':number, 'self':self.preview['binding']['request']['url']+'/'+number,
                'author':{'key':'author-key'}, 'body':self.intent['body'], **fields}

    def page(self, rows=None, offset=0, total=None, **fields):
        rows = [self.comment()] if rows is None else rows
        return {'url':_request(self.plan,offset)['url'],'redirected':False,'status':200,
                'body':{'startAt':offset,'maxResults':50,'total':len(rows) if total is None else total,'comments':rows},**fields}

    def capture(self,*pages):
        return {'kind':'JiraCommentCapture','schema_version':'1.0.0','plan_sha256':self.plan['sha256'],'pages':list(pages) or [self.page()]}

    def assess(self,capture=None):
        return assess_comments(*self.args,self.capture() if capture is None else capture)

    def test_bound_read_plan_and_candidate_without_execution(self):
        original = copy.deepcopy(self.args)
        with patch('socket.socket',side_effect=AssertionError('No network')),patch('subprocess.Popen',side_effect=AssertionError('No process')):
            result = self.assess()
        self.assertEqual(self.args,original)
        self.assertEqual(result['status'],'candidate_observed')
        self.assertEqual(result['binding']['candidate']['id'],'301')
        self.assertEqual(result['binding']['capture_sha256'],digest(self.capture()))
        self.assertEqual(result['sha256'],digest(result['binding']))
        self.assertEqual(self.plan['binding']['first_request']['method'],'GET')
        self.assertNotIn('Authorization',self.plan['binding']['first_request']['headers'])
        for flag in ('retry_allowed','live_authorized','remote_effect_confirmed'): self.assertFalse(result[flag])
        self.assertNotIn(self.intent['body'],json.dumps(result))

    def test_unrelated_comments_are_ignored_but_all_pages_are_checked(self):
        rows = [self.comment('300',body='Unrelated note',author={'key':'other'})]
        result = self.assess(self.capture(self.page(rows,total=2),self.page(offset=1,total=2)))
        self.assertEqual(result['status'],'candidate_observed')
        self.assertEqual(result['binding']['pages_checked'],2)
        self.assertEqual(result['binding']['comments_checked'],2)
        self.assertIn('startAt=1',result['binding']['read_requests'][1]['url'])

    def test_empty_different_author_body_and_visibility_do_not_match(self):
        for rows in ([],[self.comment(author={'key':'other'})],[self.comment(body='different')],
                     [self.comment(visibility={'type':'role','value':'Reviewers'})]):
            result = self.assess(self.capture(self.page(rows)))
            self.assertEqual(result['status'],'unresolved')
            self.assertIsNone(result['binding']['candidate'])
            self.assertIn('no_matching_comment',result['binding']['blockers'])

    def test_restricted_visibility_requires_exact_match(self):
        self.intent['visibility'] = {'type':'role','value':'Reviewers'}
        preview = prepare_comment(self.intent,self.target,self.issue)
        self.args = (self.intent,self.target,self.issue,preview['sha256'],'author-key')
        self.plan = prepare_comment_read_plan(*self.args)
        self.assertEqual(self.assess()['status'],'unresolved')
        result = self.assess(self.capture(self.page([self.comment(visibility=self.intent['visibility'])])))
        self.assertEqual(result['status'],'candidate_observed')

    def test_multiple_matches_and_overlapping_ids_are_unresolved(self):
        for rows,reason in (([self.comment(),self.comment('302')],'multiple_matching_comments'),
                            ([self.comment(),self.comment(body='changed')],'duplicate_comment_id')):
            result = self.assess(self.capture(self.page(rows)))
            self.assertIn(reason,result['binding']['blockers'])
            self.assertIsNone(result['binding']['candidate'])

    def test_total_drift_truncation_and_empty_middle_page_remain_unresolved(self):
        captures = [self.capture(self.page(total=2)),
                    self.capture(self.page(total=2),self.page([],offset=1,total=2)),
                    self.capture(self.page(total=2),self.page([self.comment('302',body='other')],offset=1,total=3)),
                    self.capture(*[self.page([self.comment(str(301+n),body='other')],offset=n,total=11) for n in range(10)])]
        for capture in captures:
            result = self.assess(capture)
            self.assertEqual(result['status'],'unresolved')
            self.assertIn('pagination_incomplete',result['binding']['blockers'])

    def test_origin_redirects_offsets_and_extra_pages_reject(self):
        for page in (self.page(url='https://evil.example'),self.page(redirected=True),self.page(redirected=0),self.page(offset=1),self.page(status=True)):
            with self.assertRaises(Rejected): self.assess(self.capture(page))
        with self.assertRaises(Rejected): self.assess(self.capture(self.page(),self.page(offset=1,total=2)))
        for name,value in (('startAt',True),('maxResults',0),('maxResults',51),('total',-1),('total',1000001),('comments',{})):
            page = self.page(); page['body'][name] = value
            with self.assertRaises(Rejected): self.assess(self.capture(page))

    def test_malformed_identity_and_foreign_comment_urls_block(self):
        for comment in (self.comment(id='bad'),{**self.comment(),'self':'https://evil.example'},self.comment(author={}),self.comment(body=None)):
            result = self.assess(self.capture(self.page([comment])))
            self.assertIn('comment_identity_or_content_malformed',result['binding']['blockers'])

    def test_failed_pages_do_not_echo_errors_or_allow_retry(self):
        for status in (302,403,404,429,503):
            result = self.assess(self.capture(self.page(status=status,body={'error':'private response'})))
            self.assertEqual(result['status'],'unresolved')
            self.assertIn('page_unavailable',result['binding']['blockers'])
            self.assertNotIn('private response',json.dumps(result))
            self.assertFalse(result['retry_allowed'])

    def test_closed_capture_budgets_and_plan_binding(self):
        for key,value in (('kind','other'),('schema_version','2'),('plan_sha256','f'*64),('pages',[]),
                          ('pages',[self.page()]*11),('extra','x'*65536)):
            capture = self.capture(); capture[key] = value
            with self.assertRaises(Rejected): self.assess(capture)
        for author in ('',' ','Bearer forbidden','a'*257,'a\n'):
            with self.assertRaises(Rejected): prepare_comment_read_plan(*self.args[:-1],author)
        oversized = self.capture(self.page([self.comment(body='x'*65536)]))
        with self.assertRaises(Rejected): self.assess(oversized)
        with self.assertRaises(Rejected): prepare_comment_read_plan(*self.args[:3],'f'*64,'author-key')

    def test_source_snapshot_drift_cannot_change_recovery_scope(self):
        issue = copy.deepcopy(self.issue); issue['body']['fields']['summary'] = 'changed'
        with self.assertRaises(Rejected): assess_comments(self.intent,self.target,issue,self.preview['sha256'],'author-key',self.capture())
        alternate = prepare_comment_read_plan(*self.args[:-1],'other-key')
        self.assertNotEqual(alternate['sha256'],self.plan['sha256'])
        with self.assertRaises(Rejected): assess_comments(*self.args[:-1],'other-key',self.capture())

    def test_cli_plan_recovery_and_unresolved_results(self):
        from orch.__main__ import main
        with tempfile.TemporaryDirectory() as root:
            paths = {name:Path(root)/(name+'.json') for name in ('intent','target','issue','capture')}
            for name,value in (('intent',self.intent),('target',self.target),('issue',self.issue),('capture',self.capture())): paths[name].write_bytes(canonical(value))
            common = ['--intent',str(paths['intent']),'--jira-target',str(paths['target']),'--observations',str(paths['issue']),
                      '--expected-sha256',self.preview['sha256'],'--jira-author-key','author-key']
            for command,extra,kind in (('jira-comment-read-plan',[],'JiraCommentReadPlan'),
                                      ('jira-reconcile-comments',['--transcript',str(paths['capture'])],'JiraCommentReconciliation')):
                out = io.StringIO()
                with patch.object(sys,'argv',['orch',command,*common,*extra]),redirect_stdout(out): main()
                self.assertEqual(json.loads(out.getvalue())['kind'],kind)
            paths['capture'].write_bytes(canonical(self.capture(self.page([]))))
            out = io.StringIO()
            with patch.object(sys,'argv',['orch','jira-reconcile-comments',*common,'--transcript',str(paths['capture'])]),redirect_stdout(out),self.assertRaises(SystemExit) as raised: main()
            self.assertEqual(raised.exception.code,2)
            self.assertEqual(json.loads(out.getvalue())['status'],'unresolved')
            paths['capture'].write_text('{"kind":1,"kind":2}')
            out = io.StringIO()
            with patch.object(sys,'argv',['orch','jira-reconcile-comments',*common,'--transcript',str(paths['capture'])]),redirect_stdout(out),redirect_stderr(io.StringIO()),self.assertRaises(SystemExit): main()
            self.assertEqual(out.getvalue(),'')
