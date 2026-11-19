import{evidence}from'../stores/evidence.js';
import{batches}from'../stores/batch.js';
import{getEvidence,exportEvidence}from'../api/evidence.js';
import{header}from'../components/common/header.js';
import{table}from'../components/common/table.js';
import{badge}from'../components/common/badge.js';
import{STATUSES}from'../constants/enums.js';
import{esc,json}from'../utils/html.js';
import{notify}from'../hooks/feedback.js';

function statusOf(batch,record){
  if(record?.result?.error)return 'failed';
  if(batch?.status)return batch.status;
  return record?'different':'queued';
}

function formatMeasures(values){
  if(!values||!values.length)return '—';
  return values.map(esc).join(' / ');
}

function billRow(kind,entry){
  const original=entry.original||{};
  const candidate=entry.candidate||{};
  return [esc(kind),'<span class="mono">#'+esc(entry.invoice_id)+'</span>',esc(entry.customer||original.customer||candidate.customer||''),
          formatMeasures(original.measures),formatMeasures(candidate.measures)];
}

function billDiffTable(bills){
  const rows=[];
  (bills.original_only||[]).forEach(x=>rows.push(billRow('仅原始计入',{original:x})));
  (bills.candidate_only||[]).forEach(x=>rows.push(billRow('仅候选计入',{candidate:x})));
  (bills.changed_measures||[]).forEach(x=>rows.push(billRow('计入金额不同',{invoice_id:x.invoice_id,customer:x.customer,original:{measures:x.original_measures},candidate:{measures:x.candidate_measures}})));
  return rows.length?table(['差异类型','账单号','客户','原始计入','候选计入'],rows)
    :'<p class="helper">两侧计入的账单集合与金额一致。</p>';
}

function attributionPanel(r){
  const attribution=r.attribution||{};
  const diff=r.first_customer_difference;
  const bills=r.bill_attribution||{};
  return `<section class="panel"><h2>按客户核对与账单归因</h2>
    <p class="helper">${esc(attribution.isolation||'')}<br>${esc(attribution.comparison||'')}</p>
    <div class="metrics">
      <div><small>核对客户</small><strong>${attribution.customers_checked??'—'}</strong></div>
      <div><small>原始 / 候选计入账单</small><strong>${bills.original_bill_count??'—'} / ${bills.candidate_bill_count??'—'}</strong></div>
      <div><small>仅原始 / 仅候选 / 金额不同</small><strong>${bills.original_only_count??0} / ${bills.candidate_only_count??0} / ${bills.changed_count??0}</strong></div>
    </div>
    ${diff?`<div class="diffbox"><h3>首个不一致客户：${esc(diff.customer)}</h3>
      ${diff.original_rows?.length||diff.candidate_rows?.length?`<p class="helper">原始结果</p>${table(['客户','小计'],diff.original_rows.map(row=>row.map(esc)))}
      <p class="helper">候选结果</p>${table(['客户','小计'],diff.candidate_rows.map(row=>row.map(esc)))}`:''}
      <p class="helper">${esc(diff.reason||'')}</p>
      <p class="helper">该客户两侧实际计入的账单差异</p>
      ${diff.bills&&diff.bills.length?table(['差异类型','账单号','客户','原始计入','候选计入'],diff.bills.map(b=>billRow(b.kind,{invoice_id:b.invoice_id,customer:b.customer,original:b.original,candidate:b.candidate,measures:b.measures}))):'<p class="helper">该客户的账单集合相同。</p>'}
    </div>`:`<p class="helper">${esc(attribution.summary||'')}</p>`}
    <details><summary>全部账单差异（JSON）</summary><pre class="json">${json({original_only:bills.original_only||[],candidate_only:bills.candidate_only||[],changed_measures:bills.changed_measures||[],customer_differences:r.customer_differences||[]})}</pre></details>
  </section>`;
}

function resultPanel(record){
  const r=record.result;
  if(r.error){
    return `<section class="panel"><h2>失败原因</h2>
      <p class="error">${esc(r.error)}</p>
      <p class="helper">执行失败也保留证据；本次没有对结果作出一致或差异结论。${r.error_kind==='attribution'?'账单归属不明时不作结论。':''}</p>
      ${r.error_stage?`<p class="helper">失败环节：${esc(r.error_stage)}${r.timed_out?'（超时）':''}</p>`:''}
      <pre class="json">${json(r)}</pre></section>`;
  }
  return `${r.attribution?.checked?attributionPanel(r):''}
    <section class="panel"><h2>执行结果</h2>
    <div class="metrics">
      <div><small>原始结果行数</small><strong>${r.original.row_count}</strong></div>
      <div><small>候选结果行数</small><strong>${r.candidate.row_count}</strong></div>
      <div><small>原始 / 候选查询耗时 ms</small><strong>${r.original.elapsed_ms} / ${r.candidate.elapsed_ms}</strong></div>
    </div>
    <p class="helper">${esc(r.comparison)}<br>${esc(r.note)}</p>
    <div class="split"><div><h2>候选实际结果</h2>${table(r.candidate.columns,r.candidate.rows.slice(0,20).map(row=>row.map(esc)))}<p class="helper">最多预览20行，完整结果在 JSON 中。</p>
    <h2>差异定位</h2><pre class="json">${json({missing_rows:r.missing_rows,extra_rows:r.extra_rows})}</pre></div>
    <aside><h2>结果摘要 SHA-256</h2><pre class="json">原始：${esc(r.original_checksum)}
候选：${esc(r.candidate_checksum)}</pre><details open><summary>候选 EXPLAIN ANALYZE</summary><pre class="json">${json(r.candidate.plan)}</pre></details><details><summary>原始 EXPLAIN ANALYZE</summary><pre class="json">${json(r.original.plan)}</pre></details></aside></div></section>`;
}

export async function evidencePage(el){
  const [allBatches,records]=await Promise.all([batches.load(),evidence.load()]);
  const evidenceByBatch=new Map(records.map(x=>[x.batch_id,x]));
  const pool=allBatches.length?allBatches:records.map(x=>({id:x.batch_id,status:x.result.error?'failed':undefined}));
  const requested=Number(location.hash.split('/')[1]);
  const ident=requested||pool[0]?.id;
  const batch=allBatches.find(x=>x.id===ident)||null;
  const record=evidenceByBatch.get(ident)||null;
  const detail=ident?await getEvidence(ident).catch(()=>null):null;
  const status=statusOf(batch,detail?{result:detail.result}:record);
  let body;
  if(!pool.length){body='<div class="empty">还没有复核批次。到 SQL 复核页提交一份候选 SQL，worker 完成后在这里查看。</div>'}
  else if(!detail){body=`<section class="panel"><h2>批次 #${ident} · ${badge(status)}</h2><p class="helper">待核：批次已登记，worker 尚未生成证据。请稍后在运行历史刷新，或重新打开本页。</p></section>`}
  else body=`<section class="panel"><div class="actions" style="margin-top:0"><h2 style="margin:0">复核批次 #${ident}${batch?' · '+esc(batch.name):''}</h2>${badge(status)}${batch?.attribution?'<span class="badge">按客户归因</span>':''}<button id="export">下载 JSON 证据 ↓</button></div>${resultPanel(detail)}</section>`;
  el.innerHTML=header('04','从结果一直查到执行计划','证据来源于 PostgreSQL 实际执行。保留两侧行数、摘要、查询计划和差异行，供结算复核人独立检查。')+
    `<section class="panel"><label>选择复核证据</label><select id="choose">
      <option value="">${allBatches.length?'请选择已完成批次':'请选择批次'}</option>
      ${pool.map(x=>{const label=STATUSES[statusOf(x,evidenceByBatch.get(x.id))]||'排队中';return `<option value="${x.id}" ${x.id===ident?'selected':''}>批次 #${x.id} · ${esc(x.name||'')} · ${esc(label)}</option>`}).join('')}
    </select></section>`+body;
  el.querySelector('#choose').onchange=e=>{if(e.target.value)location.hash='evidence/'+e.target.value};
  const button=el.querySelector('#export');if(button)button.onclick=()=>exportEvidence(ident).catch(e=>notify(e.message));
}
