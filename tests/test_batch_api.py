import tempfile
import unittest

from orch.api import Application
from orch.engine import Engine
from orch.execution import Executor


class BatchAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = Engine(self.tmp.name, Executor('trusted-fixture'))
        self.app = Application(self.engine)
        self.tasks = [self.engine.create_task() for _ in range(2)]

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, token=None):
        return self.app.dispatch(method,path,body or {},self.app.session if token is None else token)

    def create(self):
        code, batch = self.request('POST','/batches',{'tasks':[{'task_id':t,'expected_revision':1} for t in self.tasks]})
        self.assertEqual(code,201)
        return batch

    def test_complete_route_flow_preserves_approval_boundaries(self):
        batch = self.create()
        self.assertTrue(all(self.engine.task(t)['state']['state']=='SPEC_READY' for t in self.tasks))
        code, listing = self.request('GET','/batches')
        self.assertEqual(code,200)
        self.assertEqual(listing['items'][0]['id'],batch['id'])
        code, inspection = self.request('GET','/batches/'+batch['id'])
        self.assertEqual(code,200)
        self.assertFalse(inspection['execution_authorized'])
        payload = {'scope_sha256':batch['scope_sha256'],'expected_revision':0}
        code, result = self.request('POST','/batches/'+batch['id']+'/run',payload)
        self.assertEqual(code,200)
        self.assertEqual([e['result'] for e in result['entries']],['AWAITING_PLAN_APPROVAL']*2)
        self.assertEqual(self.request('POST','/batches/'+batch['id']+'/run',payload)[0],409)

    def test_authentication_precedes_every_batch_route(self):
        for method,path,body in [('GET','/batches',{}),('POST','/batches',{'tasks':[]}),
                                 ('GET','/batches/'+'a'*32,{}),('POST','/batches/'+'a'*32+'/run',{}),
                                 ('POST','/batches/'+'a'*32+'/abandon',{})]:
            self.assertEqual(self.request(method,path,body,'invalid')[0],401)
        self.assertFalse((self.engine.store.root/'batches').exists())

    def test_strict_fields_queries_and_unknown_methods(self):
        batch = self.create()
        path = '/batches/'+batch['id']
        for query in ('?after=', '?after=a&after=b', '?limit=10', '?after=../escape'):
            self.assertEqual(self.request('GET','/batches'+query)[0],409)
        self.assertEqual(self.request('GET',path+'?after=a')[0],409)
        self.assertEqual(self.request('POST',path+'/run',{'scope_sha256':batch['scope_sha256'],'expected_revision':0,'approve':True})[0],409)
        self.assertEqual(self.request('POST',path+'/run',{'scope_sha256':batch['scope_sha256'],'expected_revision':True})[0],409)
        self.assertEqual(self.request('GET',path+'/run')[0],405)
        self.assertEqual(self.request('POST',path+'/approve')[0],405)
        self.assertEqual(self.request('GET',path,{'ignored':True})[0],409)

    def test_abandonment_is_snapshot_bound_and_only_changes_tracking(self):
        batch = self.create()
        path = '/batches/'+batch['id']
        _, inspection = self.request('GET',path)
        before = self.engine.task(self.tasks[0])
        payload = {'scope_sha256':batch['scope_sha256'],'expected_revision':0,'task_id':self.tasks[0],
                   'expected_snapshot_sha256':'a'*64}
        self.assertEqual(self.request('POST',path+'/abandon',payload)[0],409)
        payload['expected_snapshot_sha256'] = inspection['observations'][0]['snapshot_sha256']
        code,result = self.request('POST',path+'/abandon',payload)
        self.assertEqual(code,200)
        self.assertEqual(result['entries'][0]['status'],'abandoned')
        self.assertEqual(self.engine.task(self.tasks[0]),before)

    def test_stale_task_returns_reserved_entry_without_retry(self):
        batch = self.create()
        self.engine.advance(self.tasks[0])
        path = '/batches/'+batch['id']+'/run'
        code, result = self.request('POST',path,{'scope_sha256':batch['scope_sha256'],'expected_revision':0})
        self.assertEqual(code,200)
        self.assertEqual(result['entries'][0]['status'],'running')
        self.assertEqual(self.engine.task(self.tasks[1])['state']['state'],'SPEC_READY')
        self.assertEqual(self.request('POST',path,{'scope_sha256':batch['scope_sha256'],'expected_revision':result['revision']})[0],409)
