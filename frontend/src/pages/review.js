import{samples}from'../stores/sample.js';
import{createBatch}from'../api/batch.js';
import{header}from'../components/common/header.js';
import{esc}from'../utils/html.js';
import{submit,notify}from'../hooks/feedback.js';
import{session}from'../stores/session.js';

const ATTRIBUTION_SAMPLE='低温结算汇总';
const ATTRIBUTION_DEFAULT='SELECT customer, SUM(amount) AS total FROM invoices WHERE temperature <= 8 GROUP BY customer HAVING SUM(amount) > 100 ORDER BY customer';

function reviewForm(rows,item){
  const selected=item||rows[0];
  const candidate=selected?.name===ATTRIBUTION_SAMPLE?ATTRIBUTION_DEFAULT:(selected?.original_sql||'');
  return `<form id="review-form">
    <label>复核批次名称</label>
    <input name="name" value="shift-20261117-${Date.now().toString().slice(-5)}" required maxlength="120">
    <label>基准样本</label>
    <select name="sample_id">${rows.map(x=>`<option value="${x.id}" ${x.id===selected?.id?'selected':''}>${esc(x.name)}</option>`).join('')}</select>
    <label>候选 SQL · 可编辑</label>
    <textarea name="candidate_sql" required rows="8">${esc(candidate)}</textarea>
    <label class="check"><input type="checkbox" name="attribution" value="true" ${selected?.name===ATTRIBUTION_SAMPLE?'checked':''}> 按客户核对单项加总，并追到两侧实际计入的账单</label>
    <div class="actions"><button class="primary" type="submit" ${session.get().role==='viewer'?'disabled':''}>提交 worker 执行 →</button><a href="#history">查看运行队列</a></div>
  </form>
  <p class="helper">每个批次独立保存候选 SQL。默认按结果行比较；勾选归因后，再按客户逐项核对汇总并追到账单，总额一致但客户小计或账单集合不同仍判差异。观察员仅可查看。</p>`;
}

export async function reviewPage(el){
  const rows=await samples.load();
  const selectedId=Number(location.hash.split('/')[1]);
  const item=rows.find(x=>x.id===selectedId)||rows[0];
  el.innerHTML=header('02','写一份可比对的候选改写','Python 解析白名单 → 只读事务执行原始与候选 SQL → 真实 EXPLAIN ANALYZE → 比较结果多重集合。结果不会由浏览器推算。')+
  `<div class="split">
    <section class="panel"><h2>新建复核批次</h2>${reviewForm(rows,item)}</section>
    <aside class="panel">
      <div class="eyebrow">REFERENCE SQL</div>
      <pre id="baseline" class="json">${esc(item?.original_sql||'请先在样本库登记 SQL')}</pre>
      <h2>执行保护</h2>
      <p class="helper">只读数据库角色仅拥有 invoices 的 SELECT 权限。数据库事务设置 READ ONLY；每条查询设定超时。即使语法检查漏过写操作，数据库仍会拒绝。</p>
      <p class="helper">归因查询与两侧查询在同一 REPEATABLE READ 快照内读取，途中即使有人更正账单，总额与归因仍来自同一版本。超时或账单归属不明时不作结论，批次记为失败。</p>
      <p class="helper">耗时会受缓存与机器负载影响。一次比对只能证明本数据集的结果是否一致，不能证明所有输入语义等价。</p>
    </aside>
  </div>`;
  const form=el.querySelector('#review-form');
  form.sample_id.onchange=()=>{
    const sample=rows.find(x=>x.id===Number(form.sample_id.value));
    form.candidate_sql.value=sample.original_sql;
    form.attribution.checked=sample.name===ATTRIBUTION_SAMPLE;
    el.querySelector('#baseline').textContent=sample.original_sql;
  };
  form.onsubmit=e=>{
    e.preventDefault();
    submit(form,async()=>{
      const data=Object.fromEntries(new FormData(form));
      data.sample_id=Number(data.sample_id);
      data.attribution=Boolean(data.attribution);
      const result=await createBatch(data);
      notify('批次 #'+result.id+' 已进入后台执行队列');
      location.hash='history';
    });
  };
}
