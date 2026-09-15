const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
const nodes = new Map(), pending = [];
function node(id) {
  if (!nodes.has(id)) nodes.set(id,{textContent:'',value:'',checked:false,hidden:false,disabled:false,children:[],handlers:{},
    replaceChildren(){this.children=[];},append(...items){this.children.push(...items);},setAttribute(){},
    querySelector(){return node(id+'-button');},addEventListener(name,fn){this.handlers[name]=fn;}});
  return nodes.get(id);
}
let next=0;
const context=vm.createContext({document:{getElementById:node,createElement:()=>node('dynamic-'+next++)},Map,Set,Date,setInterval(){}});
const execute=code=>vm.runInContext(code,context);
execute(fs.readFileSync(path.join(__dirname,'../orch/ui/app.js'),'utf8'));
execute(fs.readFileSync(path.join(__dirname,'../orch/ui/batches.js'),'utf8'));
context.request=(url,body)=>new Promise((resolve,reject)=>pending.push({url,body,resolve,reject}));
execute('api=request; state=async()=>{}; records=async()=>{}; tasks=async()=>{}; token="session";');
const task='a'.repeat(32), id='b'.repeat(32), other='c'.repeat(32);
function report(batch=id){return {batch:{id:batch,revision:2,scope_sha256:'d'.repeat(64),scope:{expires_at:'2999-01-01T00:00:00Z',tasks:[{task_id:task}]},entries:[{status:'pending',result:null}]},observations:[{task_id:task,state:'SPEC_READY',snapshot_sha256:'e'.repeat(64),matches_checkpoint:true}]};}
(async()=>{
 const first=execute(`inspectBatch("${id}")`), second=execute(`inspectBatch("${other}")`);
 pending[1].resolve(report(other));await second;pending[0].reject(new Error('old failure'));await first;
 assert.equal(execute('batchView.batch.id'),other);assert.match(node('batch-status').textContent,/Inspection loaded/);
 node('batch-reviewed').checked=true;execute('batchControls()');assert.equal(node('batch-run').disabled,false);
 const refresh=execute(`inspectBatch("${other}")`);assert.equal(node('batch-reviewed').checked,false);assert.equal(node('batch-run').disabled,true);
 pending[2].reject(new Error('unavailable'));await refresh;assert.equal(execute('batchView'),null);
 const load=execute(`inspectBatch("${id}")`);pending[3].resolve(report());await load;
 node('batch-reviewed').checked=true;
 const run=execute('changeBatch("run")');execute('changeBatch("run")');
 assert.equal(pending.length,5);assert.deepEqual(JSON.parse(JSON.stringify(pending[4].body)),{scope_sha256:'d'.repeat(64),expected_revision:2});
 pending[4].reject(new Error('lost response'));await run;
 assert.equal(execute('batchView'),null);assert.equal(node('batch-run').disabled,true);assert.match(node('batch-status').textContent,/uncertain/);
 const late=execute(`inspectBatch("${id}")`);execute('token=""; resetBatches()');pending[5].resolve(report());await late;
 assert.equal(execute('batchView'),null);assert.equal(node('batch-json').textContent,'');
 execute('token="session";');
 const list=execute('listBatches()');pending[6].resolve({items:[],next:id});await list;
 const page=execute('listBatches(true)');execute('listBatches(true)');assert.equal(pending.length,8);
 pending[7].reject(new Error('page failed'));await page;
 assert.equal(execute('batchNext'),id);assert.equal(node('batch-more').disabled,false);
 const abandonedLoad=execute(`inspectBatch("${id}")`);pending[8].resolve(report());await abandonedLoad;
 node('batch-abandon-task').value=task;node('batch-abandon-reviewed').checked=true;
 const abandoning=execute('changeBatch("abandon")');
 assert.deepEqual(JSON.parse(JSON.stringify(pending[9].body)),{scope_sha256:'d'.repeat(64),expected_revision:2,task_id:task,expected_snapshot_sha256:'e'.repeat(64)});
 pending[9].reject(new Error('rejected'));await abandoning;
 assert.equal(node('batch-abandon-reviewed').checked,false);assert.equal(execute('batchView'),null);
 const oldInspection=execute(`inspectBatch("${id}")`), newerList=execute('listBatches()');
 pending[11].resolve({items:[],next:null});await newerList;pending[10].resolve(report());await oldInspection;
 assert.equal(execute('batchView'),null);assert.equal(execute('batchLoading'),false);
 const oldList=execute('listBatches()'), latestInspection=execute(`inspectBatch("${id}")`);
 pending[13].resolve(report());await latestInspection;pending[12].reject(new Error('old listing failed'));await oldList;
 assert.equal(execute('batchView.batch.id'),id);assert.match(node('batch-status').textContent,/Inspection loaded/);
 execute('batchView.batch.scope.expires_at="2000-01-01T00:00:00Z";');node('batch-reviewed').checked=true;execute('batchControls()');
 assert.equal(node('batch-run').disabled,true);await execute('changeBatch("run")');assert.equal(pending.length,14);
 console.log('PASS: batch inspection ordering, cleared reviews, bound single dispatch, uncertain results, auth reset and pagination retry');
})().catch(error=>{console.error(error);process.exitCode=1;});
