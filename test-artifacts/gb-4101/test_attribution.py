"""实库测试：按客户核对单项加总并追到两侧实际计入的账单。

运行前需要可用的 PostgreSQL（含 seed_demo）与 Redis：
  DATABASE_URL=... REDIS_URL=... JWT_SECRET=x pytest test-artifacts/gb-4101 -q
"""
import json
import pytest
from sqlalchemy import text
from src.config.database import engine
from src.services.query_engine import compare, QueryFailure, AttributionFailure, build_attribution_plan, validate_sql
from scripts.seed_demo import ORIGINAL

SAME_DATA_DIFF_PLAN="SELECT customer, SUM(amount) AS total FROM invoices WHERE temperature <= 8 GROUP BY customer HAVING SUM(amount) > 100 ORDER BY customer"
# 同客换单：原计入 invoice 2（北湾，49.00，温度 4），换入 53（北湾，49.00，温度 10），结果行完全相同
SAME_CUSTOMER_SWAP="SELECT customer, SUM(amount) AS total FROM invoices WHERE (temperature <= 8 AND invoice_id <> 2) OR invoice_id = 53 GROUP BY customer HAVING SUM(amount) > 100 ORDER BY customer"
# 跨客差额抵消：去掉南港 invoice 70（49.00），换入北湾 invoice 53（49.00），总额不变、客户小计互换
CROSS_CUSTOMER_OFFSET="SELECT customer, SUM(amount) AS total FROM invoices WHERE (temperature <= 8 AND invoice_id <> 70) OR invoice_id = 53 GROUP BY customer HAVING SUM(amount) > 100 ORDER BY customer"


def test_records_of_attribution_read_a_single_snapshot():
    result=compare(ORIGINAL,SAME_DATA_DIFF_PLAN,attribution=True)
    assert result['attribution']['isolation'].startswith('REPEATABLE READ')
    assert result['attributed'] is True
    # 账单数与两侧小计都需要能在同一版数据里对上
    assert result['bill_attribution']['original_bill_count']>0


def test_equivalent_rewrite_stays_passed():
    result=compare(ORIGINAL,SAME_DATA_DIFF_PLAN,attribution=True)
    assert result['equivalent'] is True
    assert result['attribution']['consistent'] is True
    assert result['bill_attribution']['original_only_count']==0
    assert result['bill_attribution']['candidate_only_count']==0
    assert result['bill_attribution']['original_bill_count']==result['bill_attribution']['candidate_bill_count']==468


def test_same_total_but_customer_subtotals_offset_is_different():
    plain=compare(ORIGINAL,CROSS_CUSTOMER_OFFSET)
    assert plain['equivalent'] is False
    result=compare(ORIGINAL,CROSS_CUSTOMER_OFFSET,attribution=True)
    assert result['equivalent'] is False
    assert result['first_customer_difference']['customer']=='北湾冷运'
    assert result['bill_attribution']['candidate_only_count']==1
    assert result['bill_attribution']['candidate_only'][0]['invoice_id']=='53'


def test_same_customer_bill_swap_with_identical_rows_is_different():
    plain=compare(ORIGINAL,SAME_CUSTOMER_SWAP)
    assert plain['equivalent'] is True  # 旧口径只看结果行，无法发现换单
    result=compare(ORIGINAL,SAME_CUSTOMER_SWAP,attribution=True)
    assert result['equivalent'] is False
    assert result['customer_differences']==[]
    diff=result['first_customer_difference']
    assert diff['customer']=='北湾冷运'
    assert diff['reason']=='客户小计相同，但两侧实际计入的账单不同'
    assert [b['kind'] for b in diff['bills']]==['仅原始计入','仅候选计入']
    assert {b['invoice_id'] for b in diff['bills']}=={'2','53'}


def test_customer_subtotal_difference_is_reported_first():
    shifted="SELECT customer, SUM(amount) AS total FROM invoices WHERE temperature <= 8 AND status='settled' GROUP BY customer HAVING SUM(amount) > 100 ORDER BY customer"
    result=compare(ORIGINAL,shifted,attribution=True)
    assert result['equivalent'] is False
    diff=result['first_customer_difference']
    assert diff['customer']=='北湾冷运'
    assert diff['original_rows']!=diff['candidate_rows']
    assert result['bill_attribution']['original_only_count']>0  # 过滤 status 后少计入的账单
    assert result['bill_attribution']['original_bill_count']>result['bill_attribution']['candidate_bill_count']


def test_bills_are_derived_from_each_sides_own_expression():
    plan=build_attribution_plan(validate_sql("SELECT customer, ROUND(SUM(amount),2) AS total FROM invoices WHERE temperature <= 8 GROUP BY customer"))
    sql=plan['bill_sql']
    assert 'ROUND' not in sql
    assert 'temperature <= 8' in sql
    assert plan['measures'][0]['round']==2


def test_unattributable_result_is_reported_as_failure():
    with pytest.raises(AttributionFailure):
        compare(ORIGINAL,"SELECT SUM(amount) AS total FROM invoices WHERE temperature <= 8",attribution=True)
    with pytest.raises(AttributionFailure):
        compare(ORIGINAL,"SELECT customer, SUM(amount) AS total FROM invoices WHERE temperature <= 8 GROUP BY customer LIMIT 2",attribution=True)
    with pytest.raises(AttributionFailure):
        compare(ORIGINAL,"SELECT customer, SUM(amount) / 2 AS total FROM invoices WHERE temperature <= 8 GROUP BY customer",attribution=True)


def test_statement_timeout_refuses_conclusion(monkeypatch):
    import src.services.query_engine as q
    monkeypatch.setattr(q,'STATEMENT_TIMEOUT_MS',1)
    with pytest.raises(QueryFailure) as error:
        compare(ORIGINAL,SAME_DATA_DIFF_PLAN,attribution=True)
    assert error.value.timed_out is True
    assert error.value.stage in ('original','candidate')


def test_attribution_reads_the_same_snapshot_as_the_totals():
    """演示核对途中改账：结果读出后并发更正账单，归因仍读同一版本。"""
    with engine.begin() as conn:
        original=conn.execute(text('SELECT amount FROM invoices WHERE invoice_id=19')).scalar()
    changed={}
    def correct():
        with engine.begin() as conn:
            conn.execute(text("UPDATE invoices SET amount=amount-100 WHERE invoice_id=19"))
            changed['amount']=conn.execute(text('SELECT amount FROM invoices WHERE invoice_id=19')).scalar()
    try:
        result=compare(ORIGINAL,SAME_DATA_DIFF_PLAN,attribution=True,on_snapshot=correct)
    finally:
        with engine.begin() as conn:
            conn.execute(text('UPDATE invoices SET amount=:amount WHERE invoice_id=19'),{'amount':original})
    assert float(changed['amount'])==float(original)-100
    # 两侧小计仍然来自快照版本；若账单被重新读取，复算会对不上并报账单归属不明
    assert result['equivalent'] is True
    assert result['attribution']['consistent'] is True
    reports=dict((row[0],row[1]) for row in result['original']['rows'])
    assert '南港鲜食' in reports
    # 快照中的小计不会包含并发更正，归因账单也来自同一版本
    assert result['bill_attribution']['original_bill_count']==result['bill_attribution']['candidate_bill_count']
    assert result['bill_attribution']['original_bill_count']==468
    assert float(original)!=float(changed['amount'])


def test_worker_marks_timeout_and_unknown_ownership_as_failed():
    from src.worker import failure_result
    timeout=failure_result(QueryFailure('candidate_bills','执行超时',timed_out=True))
    assert timeout['error_kind']=='timeout' and timeout['timed_out'] is True and timeout['attributed'] is False
    unknown=failure_result(AttributionFailure('账单归属不明：无法识别唯一的客户关联条件'))
    assert unknown['error_kind']=='attribution' and unknown['error']=='账单归属不明：无法识别唯一的客户关联条件'


def test_api_attribution_failure_and_evidence_payload():
    import uuid
    from fastapi.testclient import TestClient
    from src.main import app
    suffix=uuid.uuid4().hex[:8]
    with TestClient(app) as client:
        token=client.post('/auth/login',json={'email':'operator@chenggang.test','password':'ColdChain!17'}).json()['token']
        headers={'Authorization':'Bearer '+token}
        sample_id=client.get('/samples',headers=headers).json()[0]['id']
        # 归属不明不在提交时拒绝，而是由 worker 记失败原因，页面可展示
        bad=client.post('/batches',headers=headers,json={'name':'attr-bad-'+suffix,'sample_id':sample_id,'candidate_sql':'SELECT SUM(amount) AS total FROM invoices', 'attribution':True})
        assert bad.status_code==202 and bad.json()['attribution'] is True
        from src.worker import run_one
        run_one(bad.json()['id'])
        evidence=client.get('/evidence/%d'%bad.json()['id'],headers=headers).json()
        assert evidence['result']['error_kind']=='attribution'
        assert evidence['result']['error'].startswith('账单归属不明')
        assert evidence['result']['attributed'] is False
        created=client.post('/batches',headers=headers,json={'name':'attr-ok-'+suffix,'sample_id':sample_id,'candidate_sql':SAME_DATA_DIFF_PLAN,'attribution':True})
        assert created.status_code==202 and created.json()['attribution'] is True
        from src.worker import run_one
        run_one(created.json()['id'])
        ok=client.get('/evidence/%d'%created.json()['id'],headers=headers).json()
        assert ok['batch']['attribution'] is True and ok['result']['attributed'] is True
        assert client.post('/batches',headers=headers,json={'name':'attr-ok-'+suffix,'sample_id':sample_id,'candidate_sql':SAME_DATA_DIFF_PLAN,'attribution':True}).status_code==409
        viewer=client.post('/auth/login',json={'email':'viewer@chenggang.test','password':'ColdChain!17'}).json()['token']
        assert client.post('/batches',headers={'Authorization':'Bearer '+viewer},json={'name':'attr-viewer-'+suffix,'sample_id':sample_id,'candidate_sql':SAME_DATA_DIFF_PLAN,'attribution':True}).status_code==403
