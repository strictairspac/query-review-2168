// 前端渲染冒烟测试：用最小 DOM/fetch 桩验证待核、结论、失败、差异首客户展示。
import assert from 'node:assert/strict';

const batches=[
  {id:4,name:'待核批次',sample_id:1,attribution:true,status:'queued',created_at:'2026-11-19'},
  {id:3,name:'换单批次',sample_id:1,attribution:true,status:'different',created_at:'2026-11-19'},
  {id:2,name:'失败批次',sample_id:1,attribution:true,status:'failed',created_at:'2026-11-19'},
  {id:1,name:'一致批次',sample_id:1,attribution:false,status:'passed',created_at:'2026-11-19'},
];
const evidence=[
  {id:1,batch_id:3,batch:{name:'换单批次',status:'different',attribution:true},result:{
    equivalent:false,attributed:true,comparison:'忽略行顺序',note:'说明',
    original:{columns:['customer','total'],rows:[['北湾冷运','12145.00']],row_count:1,elapsed_ms:1,plan:[1]},
    candidate:{columns:['customer','total'],rows:[['北湾冷运','12145.00']],row_count:1,elapsed_ms:1,plan:[1]},
    original_checksum:'a',candidate_checksum:'b',missing_rows:[],extra_rows:[],
    attribution:{enabled:true,checked:true,consistent:false,customers_checked:3,isolation:'同一快照',comparison:'按客户核对'},
    customer_differences:[],
    first_customer_difference:{customer:'北湾冷运',original_rows:[],candidate_rows:[],reason:'客户小计相同，但两侧实际计入的账单不同',
      bills:[{kind:'仅原始计入',invoice_id:'2',customer:'北湾冷运',original:{measures:['49.00']},candidate:null},
             {kind:'仅候选计入',invoice_id:'53',customer:'北湾冷运',original:null,candidate:{measures:['49.00']}}]},
    bill_attribution:{original_bill_count:468,candidate_bill_count:468,original_only_count:1,candidate_only_count:1,changed_count:0,original_only:[],candidate_only:[],changed_measures:[]}}},
  {id:2,batch_id:2,batch:{name:'失败批次',status:'failed',attribution:true},result:{
    error:'归因超时，未作结论：执行超时',error_kind:'timeout',error_stage:'original_bills',timed_out:true,attributed:false}},
  {id:3,batch_id:1,batch:{name:'一致批次',status:'passed',attribution:true},result:{
    equivalent:true,attributed:true,comparison:'忽略行顺序',note:'说明',
    original:{columns:['customer','total'],rows:[['北湾冷运','12145.00']],row_count:1,elapsed_ms:1,plan:[1]},
    candidate:{columns:['customer','total'],rows:[['北湾冷运','12145.00']],row_count:1,elapsed_ms:1,plan:[1]},
    original_checksum:'a',candidate_checksum:'a',missing_rows:[],extra_rows:[],
    attribution:{enabled:true,checked:true,consistent:true,customers_checked:3,isolation:'同一快照',comparison:'按客户核对',summary:'总额与客户小计、账单集合一致'},
    customer_differences:[],first_customer_difference:null,
    bill_attribution:{original_bill_count:468,candidate_bill_count:468,original_only_count:0,candidate_only_count:0,changed_count:0,original_only:[],candidate_only:[],changed_measures:[]}}},
];
const samples=[{id:1,name:'低温结算汇总',description:'d',original_sql:'SELECT customer, SUM(amount) FROM invoices GROUP BY customer'}];

const routes={'/api/batches':batches,'/api/evidence':evidence,'/api/samples':samples};
globalThis.location={hash:'#evidence/3'};
globalThis.localStorage={getItem:()=>JSON.stringify({token:'t',email:'operator@chenggang.test',role:'operator'}),setItem(){},removeItem(){}};
globalThis.fetch=async path=>{
  const key=path.replace(/^\/api/,'');
  let body=routes[path];
  if(!body&&/^\/api\/evidence\/\d+$/.test(path)){const id=Number(path.split('/').pop());body=evidence.find(x=>x.batch_id===id)||null}
  return {ok:!!body,status:body?200:404,json:async()=>body};
};
class El{constructor(){this._html=''}set innerHTML(v){this._html=v}get innerHTML(){return this._html}
  querySelector(){return new Proxy({},{get:(t,k)=>k in t?t[k]:(t[k]={value:'',textContent:'',checked:false}),set:(t,k,v)=>(t[k]=v,true)})}}
const el=new El();

const {evidencePage}=await import('../../frontend/src/pages/evidence.js');
await evidencePage(el);
assert.match(el.innerHTML,/首个不一致客户：北湾冷运/,'差异页列出首个不一致客户');
assert.match(el.innerHTML,/#2/);assert.match(el.innerHTML,/#53/);
assert.match(el.innerHTML,/按客户核对与账单归因/);
assert.match(el.innerHTML,/客户小计相同，但两侧实际计入的账单不同/,'小计相同时以换单原因说明');
assert.match(el.innerHTML,/差异定位/);

globalThis.location.hash='#evidence/1';const pending=new El();await evidencePage(pending);
assert.match(pending.innerHTML,/待核/);
assert.match(pending.innerHTML,/批次 #1/);

globalThis.location.hash='#evidence/2';const failed=new El();await evidencePage(failed);
assert.match(failed.innerHTML,/失败原因/);
assert.match(failed.innerHTML,/归因超时，未作结论/);
assert.doesNotMatch(failed.innerHTML,/差异定位/,'失败时不显示结论');

globalThis.location.hash='#evidence/1';const passed=new El();await evidencePage(passed);
assert.match(passed.innerHTML,/总额与客户小计、账单集合一致/);

const {reviewPage}=await import('../../frontend/src/pages/review.js');
globalThis.location.hash='#review/1';const review=new El();await reviewPage(review);
assert.match(review.innerHTML,/name="attribution"/,'复核页提供归因开关');
assert.match(review.innerHTML,/按客户核对单项加总/);
assert.match(review.innerHTML,/REPEATABLE READ/);
console.log('frontend smoke OK');
