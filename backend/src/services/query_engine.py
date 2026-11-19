"""Strict read-only bounded SQL execution against a single seeded business table.

Both sides of a comparison, plus every derived per-bill attribution statement,
run inside one REPEATABLE READ, READ ONLY transaction. The reported totals and
the bills they are traced back to therefore come from the same snapshot, even
when another session commits a correction while the check is running.
"""
import json,hashlib,time
from collections import Counter,defaultdict
from decimal import Decimal,InvalidOperation,ROUND_HALF_UP
import sqlglot
from sqlglot import exp
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from ..config.database import engine
from ..config.settings import STATEMENT_TIMEOUT_MS,QUERY_READER_ROLE
ALLOWED_FUNCTIONS={'SUM','COUNT','AVG','MIN','MAX','ROUND','COALESCE','CAST'}
ALLOWED_COLUMNS={'customer','route','amount','temperature','invoice_id','status'}
AGGREGATES=(exp.Sum,exp.Count,exp.Avg,exp.Min,exp.Max)
MAX_ROWS=1000
STAGE_LABELS={'original':'原始查询','candidate':'候选查询','original_bills':'原始账单归因查询','candidate_bills':'候选账单归因查询'}
BILL_LIMIT=10


def validate_sql(sql):
    try: trees=sqlglot.parse(sql,read='postgres')
    except Exception as exc: raise ValueError('SQL 无法解析：'+str(exc)[:180])
    if len(trees)!=1 or not isinstance(trees[0],exp.Select): raise ValueError('只允许单条 SELECT')
    tree=trees[0]
    if tree.args.get('into') or tree.args.get('locks') or tree.find(exp.With): raise ValueError('不允许 INTO、锁或 CTE')
    tables=list(tree.find_all(exp.Table))
    if not tables or any(t.name!='invoices' or t.db or t.catalog for t in tables): raise ValueError('只允许访问 invoices 表')
    for col in tree.find_all(exp.Column):
        if col.name not in ALLOWED_COLUMNS and col.name!='*': raise ValueError('未知业务字段：'+col.name)
    for f in tree.find_all(exp.Func):
        if f.sql_name() not in ALLOWED_FUNCTIONS: raise ValueError('不允许函数：'+f.sql_name())
    if len(list(tree.walk()))>250: raise ValueError('SQL 结构过大')
    return tree.sql(dialect='postgres')


class QueryFailure(Exception):
    """A labelled statement failed inside the bounded snapshot."""
    def __init__(self,stage,message,timed_out=False):
        self.stage=stage;self.message=message;self.timed_out=timed_out
        super().__init__(message)
    def __str__(self):return self.message


class AttributionFailure(ValueError):
    """The bills behind a result cannot be identified, so no conclusion is allowed."""


def _prepare_transaction(conn):
    conn.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
    conn.execute(text("SET LOCAL statement_timeout='%dms'"%STATEMENT_TIMEOUT_MS))
    conn.execute(text('SET LOCAL ROLE '+QUERY_READER_ROLE))


def _run(conn,stage,sql):
    try:
        started=time.perf_counter()
        cursor=conn.execute(text(sql)); columns=list(cursor.keys()); rows=cursor.fetchmany(MAX_ROWS+1)
        if len(rows)>MAX_ROWS: raise QueryFailure(stage,'结果超过 %d 行，无法完成核对'%MAX_ROWS)
        rows=[[str(v) if v is not None else None for v in row] for row in rows]
        return {'columns':columns,'rows':rows,'row_count':len(rows),'elapsed_ms':round((time.perf_counter()-started)*1000,3)}
    except DBAPIError as exc:
        timed_out=getattr(exc.orig,'pgcode',None)=='57014'
        message='执行超时' if timed_out else '执行失败：'+str(exc.orig)[:300]
        raise QueryFailure(stage,message,timed_out) from exc


def execute_query(conn,stage,sql,with_plan=True):
    """Validate and run one statement on the caller's pinned snapshot connection."""
    sql=validate_sql(sql)
    result={'columns':[],'rows':[],'row_count':0,'elapsed_ms':0.0}
    if with_plan:
        try: result['plan']=conn.execute(text('EXPLAIN (ANALYZE, FORMAT JSON, BUFFERS) '+sql)).scalar()
        except DBAPIError as exc:
            timed_out=getattr(exc.orig,'pgcode',None)=='57014'
            raise QueryFailure(stage,'执行超时' if timed_out else '执行失败：'+str(exc.orig)[:300],timed_out) from exc
    result.update(_run(conn,stage,sql))
    return result


# ---------------------------------------------------------------- plan building

def _projection_core(projection):
    return projection.this if isinstance(projection,exp.Alias) else projection


def _aggregate_measure(core,table_alias):
    """(aggregate kind, per-bill expression, ROUND digits) for one output column."""
    aggregates=list(core.find_all(*AGGREGATES))
    if len(aggregates)!=1: raise AttributionFailure('账单归属不明：每个输出列必须恰好包含一个聚合')
    aggregate=aggregates[0]
    digits=None
    if core is not aggregate:
        if isinstance(core,exp.Round) and core.this is aggregate and isinstance(core.args.get('decimals'),exp.Literal):
            try: digits=int(core.args['decimals'].this)
            except (TypeError,ValueError): digits=None
        else:
            raise AttributionFailure('账单归属不明：输出列不是可直接复算的聚合')
        if digits is None: raise AttributionFailure('账单归属不明：只能复算 ROUND 聚合的固定小数位')
    if isinstance(aggregate,exp.Count) and isinstance(aggregate.this,exp.Star):
        return {'agg':'count','count_star':True,'expr':exp.Literal.number(1),'round':digits}
    if any(isinstance(x,exp.Distinct) for x in aggregate.find_all(exp.Distinct)):
        raise AttributionFailure('账单归属不明：不支持聚合内 DISTINCT')
    expression=aggregate.this.copy()
    for column in expression.find_all(exp.Column):
        if column.name not in ALLOWED_COLUMNS: raise AttributionFailure('账单归属不明：聚合引用了未知字段')
        if column.table not in (None,'',table_alias): raise AttributionFailure('账单归属不明：聚合引用了其他表别名')
    return {'agg':aggregate.sql_name().lower(),'count_star':False,'expr':expression,'round':digits}


def _bill_select(customer_expr,invoice_expr,measures,from_expr,where_expr):
    values=[exp.alias_(customer_expr,'customer'),exp.alias_(invoice_expr,'invoice_id')]
    for index,measure in enumerate(measures): values.append(exp.alias_(measure['expr'].copy(),'measure_%d'%index))
    query=exp.select(*values).from_(from_expr)
    if where_expr is not None: query=query.where(where_expr.copy())
    return query.order_by(exp.column('customer'),exp.column('invoice_id')).sql(dialect='postgres')


def _grouped_plan(tree):
    """GROUP BY customer over a single invoices scan."""
    frm=tree.args.get('from')
    tables=list(tree.find_all(exp.Table))
    group=tree.args.get('group')
    if not frm or len(tables)!=1 or list(tree.find_all(exp.Join)): return None
    if not group or len(group.expressions)!=1: return None
    key=group.expressions[0]
    if not isinstance(key,exp.Column) or key.name!='customer': return None
    table=tables[0];alias=table.alias
    if key.table not in (None,'',alias): return None
    customer_index=None;measures=[]
    for index,projection in enumerate(tree.expressions):
        core=_projection_core(projection)
        if isinstance(core,exp.Star): raise AttributionFailure('账单归属不明：不支持 SELECT *')
        if isinstance(core,exp.Column) and core.name=='customer' and core.table in (None,'',alias):
            if customer_index is not None: raise AttributionFailure('账单归属不明：客户列重复输出')
            customer_index=index;continue
        if not list(core.find_all(*AGGREGATES)):
            raise AttributionFailure('账单归属不明：按客户分组时，非客户输出列必须是单个聚合')
        measure=_aggregate_measure(core,alias);measure['index']=index;measures.append(measure)
    if customer_index is None: raise AttributionFailure('账单归属不明：输出缺少客户列')
    if not measures: raise AttributionFailure('账单归属不明：输出缺少可复算的聚合列')
    if tree.args.get('limit') or tree.args.get('offset'):
        raise AttributionFailure('账单归属不明：归因查询不支持 LIMIT/OFFSET，账单集合会被截断')
    invoice=exp.column('invoice_id',table=alias) if alias else exp.column('invoice_id')
    sql=_bill_select(key.copy(),invoice,measures,frm.this.copy(),tree.args['where'].this if tree.args.get('where') else None)
    validate_sql(sql)
    return {'shape':'grouped','customer_index':customer_index,'measures':measures,'bill_sql':sql}


def _is_correlation(condition,inner_alias,outer_alias):
    if not isinstance(condition,exp.EQ): return False
    parts=[condition.this,condition.expression]
    if not all(isinstance(x,exp.Column) and x.name=='customer' for x in parts): return False
    if not inner_alias or not outer_alias: return False
    inner=[x for x in parts if x.table==inner_alias]
    outer=[x for x in parts if x.table==outer_alias]
    return len(inner)==1 and len(outer)==1


def _references_alias(node,alias):
    return any(column.table==alias for column in node.find_all(exp.Column))


def _correlated_plan(tree):
    """SELECT customer, (SELECT SUM(amount) FROM invoices j WHERE j.customer=i.customer AND ...)
    FROM invoices i ... — the seeded baseline shape."""
    frm=tree.args.get('from')
    if not frm or tree.args.get('group'): return None
    outer=frm.this
    if not isinstance(outer,exp.Table) or outer.name!='invoices' or list(tree.find_all(exp.Join)): return None
    if len(list(tree.find_all(exp.Table)))<1: return None
    outer_alias=outer.alias
    customer_index=None;measures=[];components=[];filters=[]
    for index,projection in enumerate(tree.expressions):
        core=_projection_core(projection)
        if isinstance(core,exp.Star): raise AttributionFailure('账单归属不明：不支持 SELECT *')
        if isinstance(core,exp.Column) and core.name=='customer' and core.table in (None,'',outer_alias):
            if customer_index is not None: raise AttributionFailure('账单归属不明：客户列重复输出')
            customer_index=index;continue
        if not isinstance(core,exp.Subquery) or not isinstance(core.this,exp.Select):
            raise AttributionFailure('账单归属不明：输出列既不是客户也不是标量聚合子查询')
        inner=core.this
        inner_tables=list(inner.find_all(exp.Table))
        if (len(inner_tables)!=1 or inner_tables[0].name!='invoices' or list(inner.find_all(exp.Join))
                or inner.args.get('group') or inner.args.get('having') or inner.args.get('limit')):
            raise AttributionFailure('账单归属不明：仅支持对 invoices 的单层标量聚合')
        inner_alias=inner_tables[0].alias
        if len(inner.expressions)!=1: raise AttributionFailure('账单归属不明：标量子查询必须只有一个聚合')
        aggregate_core=_projection_core(inner.expressions[0])
        if not list(aggregate_core.find_all(*AGGREGATES)):
            raise AttributionFailure('账单归属不明：标量子查询缺少聚合')
        measure=_aggregate_measure(aggregate_core,inner_alias)
        if _references_alias(measure['expr'],outer_alias):
            raise AttributionFailure('账单归属不明：聚合表达式引用了外层账单，无法按账单归属')
        where=inner.args.get('where')
        if not where: raise AttributionFailure('账单归属不明：标量聚合缺少客户关联条件')
        conjuncts=list(where.this.flatten()) if isinstance(where.this,exp.And) else [where.this]
        correlation=[c for c in conjuncts if _is_correlation(c,inner_alias,outer_alias)]
        if len(correlation)!=1: raise AttributionFailure('账单归属不明：无法识别唯一的客户关联条件')
        rest=[c for c in conjuncts if c is not correlation[0]]
        if any(_references_alias(c,outer_alias) for c in rest):
            raise AttributionFailure('账单归属不明：筛选条件引用了外层账单，无法按账单归属')
        filter_node=exp.and_(*[c.copy() for c in rest]) if rest else None
        filters.append(filter_node.sql(dialect='postgres') if filter_node is not None else None)
        measure['index']=index;measures.append(measure);components.append(inner_tables[0])
    if customer_index is None or not measures: return None
    if tree.args.get('limit') or tree.args.get('offset'):
        raise AttributionFailure('账单归属不明：归因查询不支持 LIMIT/OFFSET，账单集合会被截断')
    if len(set(filters))!=1: raise AttributionFailure('账单归属不明：各聚合项的筛选条件不一致')
    from_expr=components[0].copy()
    alias=components[0].alias
    customer=exp.column('customer',table=alias) if alias else exp.column('customer')
    invoice=exp.column('invoice_id',table=alias) if alias else exp.column('invoice_id')
    filter_expr=sqlglot.parse_one('SELECT 1 WHERE '+filters[0],read='postgres').args['where'].this if filters[0] else None
    sql=_bill_select(customer,invoice,measures,from_expr,filter_expr)
    validate_sql(sql)
    return {'shape':'correlated','customer_index':customer_index,'measures':measures,'bill_sql':sql}


def build_attribution_plan(sql):
    """Derive a per-bill query that uses each side's own expressions and filters."""
    tree=sqlglot.parse_one(sql,read='postgres')
    plan=_grouped_plan(tree)
    if plan is None: plan=_correlated_plan(tree)
    if plan is None: raise AttributionFailure('账单归属不明：只支持按客户分组的单表聚合，或按 client 关联的标量聚合查询')
    return plan


# ------------------------------------------------------------------ attribution

def _decimal(value):
    if value is None: return None
    try:
        number=Decimal(str(value))
    except (InvalidOperation,TypeError,ValueError): return None
    return number if number.is_finite() else None


def _close(left,right):
    if left is None or right is None: return left is None and right is None
    difference=abs(left-right)
    return difference<=Decimal('0.000001') or difference<=abs(right)*Decimal('1e-9')


def _equal_measures(left,right):
    if left is None or right is None: return left==right
    a=_decimal(left);b=_decimal(right)
    if a is not None and b is not None: return _close(a,b)
    return left==right


def _recompute(measure,values):
    if measure['count_star']:
        value=Decimal(len(values))
    else:
        present=[v for v in (_decimal(x) for x in values) if v is not None]
        if measure['agg']=='count': value=Decimal(len(present))
        elif not present: value=None
        elif measure['agg']=='sum': value=sum(present,Decimal(0))
        elif measure['agg']=='avg': value=sum(present,Decimal(0))/Decimal(len(present))
        elif measure['agg']=='min': value=min(present)
        elif measure['agg']=='max': value=max(present)
        else: value=None
    if value is not None and measure['round'] is not None:
        try: value=value.quantize(Decimal(1).scaleb(-measure['round']),rounding=ROUND_HALF_UP)
        except Exception: value=None
    return value


def _rows_by_customer(rows,customer_index):
    grouped=defaultdict(list)
    for row in rows: grouped[row[customer_index]].append(row)
    return grouped


def _bills_by_customer(bill_rows,measure_count):
    grouped=defaultdict(list)
    for row in bill_rows: grouped[row[0]].append({'invoice_id':row[1],'measures':row[2:2+measure_count]})
    return grouped


def _check_side(plan,output,bills,stage):
    """Confirm every reported customer subtotal equals the sum of the bills we
    traced for it. Any mismatch means we cannot claim bill ownership."""
    output_groups=_rows_by_customer(output['rows'],plan['customer_index'])
    bills_by_customer=_bills_by_customer(bills['rows'],len(plan['measures']))
    labels=STAGE_LABELS[stage]
    if not output_groups and any(bills_by_customer.values()):
        raise AttributionFailure('账单归属不明：%s 结果为空，但归因查询仍计入账单，无法核对'%labels)
    for customer,rows in output_groups.items():
        if len(rows)!=1:
            raise AttributionFailure('账单归属不明：%s 中客户 %s 出现 %d 行，无法按客户核对'%(labels,customer,len(rows)))
        customer_bills=bills_by_customer.get(customer,[])
        for measure_index,measure in enumerate(plan['measures']):
            reported=_decimal(rows[0][measure['index']])
            recomputed=_recompute(measure,[bill['measures'][measure_index] for bill in customer_bills])
            if not _close(reported,recomputed):
                raise AttributionFailure('账单归属不明：%s 客户 %s 的汇总无法由其账单复算（结果 %s，账单合计 %s）'%(
                    labels,customer,rows[0][measure['index']],'无' if recomputed is None else str(recomputed)))
    return output_groups,bills_by_customer


def _bill_index(bills_by_customer,customers):
    index={}
    for customer,rows in bills_by_customer.items():
        if customer not in customers: continue
        for bill in rows:
            invoice_id=bill['invoice_id']
            if invoice_id in index: raise AttributionFailure('账单归属不明：账单 %s 被重复计入'%invoice_id)
            index[invoice_id]={'customer':customer,'measures':bill['measures']}
    return index


def _invoice_order(identifier):
    return (0,int(identifier)) if str(identifier).isdigit() else (1,str(identifier))


def _bill_differences(original_index,candidate_index):
    only_original=sorted(set(original_index)-set(candidate_index),key=_invoice_order)
    only_candidate=sorted(set(candidate_index)-set(original_index),key=_invoice_order)
    changed=sorted([i for i in set(original_index)&set(candidate_index)
                    if not all(_equal_measures(a,b) for a,b in zip(original_index[i]['measures'],candidate_index[i]['measures']))],key=_invoice_order)
    first=None
    for invoice_id in only_original:
        first={'invoice_id':invoice_id,'customer':original_index[invoice_id]['customer'],'kind':'仅原始计入','original':original_index[invoice_id],'candidate':None};break
    if first is None:
        for invoice_id in only_candidate:
            first={'invoice_id':invoice_id,'customer':candidate_index[invoice_id]['customer'],'kind':'仅候选计入','original':None,'candidate':candidate_index[invoice_id]};break
    if first is None and changed:
        invoice_id=changed[0]
        first={'invoice_id':invoice_id,'customer':original_index[invoice_id]['customer'],'kind':'计入金额不同','original':original_index[invoice_id],'candidate':candidate_index[invoice_id]}
    return {'original_bill_count':len(original_index),'candidate_bill_count':len(candidate_index),
            'original_only_count':len(only_original),'candidate_only_count':len(only_candidate),'changed_count':len(changed),
            'original_only':[{'invoice_id':i,**original_index[i]} for i in only_original[:BILL_LIMIT]],
            'candidate_only':[{'invoice_id':i,**candidate_index[i]} for i in only_candidate[:BILL_LIMIT]],
            'changed_measures':[{'invoice_id':i,'customer':original_index[i]['customer'],
                                 'original_measures':original_index[i]['measures'],'candidate_measures':candidate_index[i]['measures']}
                                for i in changed[:BILL_LIMIT]],
            'first_difference':first}


def _customer_differences(original_groups,candidate_groups):
    differences=[]
    for customer in sorted(set(original_groups)|set(candidate_groups)):
        left=sorted(original_groups.get(customer,[]));right=sorted(candidate_groups.get(customer,[]))
        if left!=right:
            differences.append({'customer':customer,'original_rows':left,'candidate_rows':right,
                                'reason':'客户小计或结果行不一致'})
    return differences


def _first_customer_difference(differences,bill_diff):
    """First customer whose subtotal differs, else the customer of the first
    bill-set difference, so a same-total swap is still pinned to a customer."""
    if differences:
        customer=differences[0]['customer']
        entry={'customer':customer,
               'original_rows':differences[0]['original_rows'],
               'candidate_rows':differences[0]['candidate_rows'],
               'reason':differences[0]['reason']}
    else:
        first=bill_diff['first_difference']
        if not first: return None
        customer=first['customer']
        entry={'customer':customer,'original_rows':[],'candidate_rows':[],
               'reason':'客户小计相同，但两侧实际计入的账单不同'}
    bills=[]
    for bill in bill_diff['original_only']:
        if bill['customer']==customer: bills.append({'kind':'仅原始计入','invoice_id':bill['invoice_id'],'customer':customer,'original':{'measures':bill['measures']},'candidate':None})
    for bill in bill_diff['candidate_only']:
        if bill['customer']==customer: bills.append({'kind':'仅候选计入','invoice_id':bill['invoice_id'],'customer':customer,'original':None,'candidate':{'measures':bill['measures']}})
    for bill in bill_diff['changed_measures']:
        if bill['customer']==customer:
            bills.append({'kind':'计入金额不同','invoice_id':bill['invoice_id'],'customer':customer,
                          'original':{'measures':bill['original_measures']},'candidate':{'measures':bill['candidate_measures']}})
    entry['bills']=bills[:BILL_LIMIT]
    entry['first_bill']=bill_diff['first_difference']
    return entry


# --------------------------------------------------------------------- compare

def compare(original_sql,candidate_sql,attribution=False,on_snapshot=None):
    """on_snapshot is an optional hook invoked after both sides have been read
    but before attribution reads, so a concurrent commit can be exercised."""
    left_sql=validate_sql(original_sql);right_sql=validate_sql(candidate_sql)
    original_plan=candidate_plan=None
    if attribution:
        original_plan=build_attribution_plan(left_sql)
        candidate_plan=build_attribution_plan(right_sql)
    with engine.connect() as conn:
        with conn.begin():
            _prepare_transaction(conn)
            original=execute_query(conn,'original',left_sql)
            candidate=execute_query(conn,'candidate',right_sql)
            if on_snapshot is not None: on_snapshot()
            original_bills=candidate_bills=None
            if attribution:
                original_bills=execute_query(conn,'original_bills',original_plan['bill_sql'],with_plan=False)
                candidate_bills=execute_query(conn,'candidate_bills',candidate_plan['bill_sql'],with_plan=False)
    canon=lambda data:Counter(json.dumps(row,ensure_ascii=False) for row in data['rows'])
    a,b=canon(original),canon(candidate)
    digest=lambda x:hashlib.sha256(json.dumps(sorted(canon(x).items()),ensure_ascii=False).encode()).hexdigest()
    equivalent=len(original['columns'])==len(candidate['columns']) and a==b
    result={'equivalent':equivalent,
            'comparison':'忽略行顺序、保留重复行；按输出列位置比较，数字按数据库文本表示',
            'original':original,'candidate':candidate,
            'original_checksum':digest(original),'candidate_checksum':digest(candidate),
            'missing_rows':list((a-b).elements())[:BILL_LIMIT],'extra_rows':list((b-a).elements())[:BILL_LIMIT],
            'attribution':{'enabled':bool(attribution)},
            'note':'执行时间受缓存与机器负载影响；不据一次耗时断言优化收益'}
    if attribution:
        original_groups,original_bills_by_customer=_check_side(original_plan,original,original_bills,'original')
        candidate_groups,candidate_bills_by_customer=_check_side(candidate_plan,candidate,candidate_bills,'candidate')
        customers=set(original_groups)|set(candidate_groups)
        original_index=_bill_index(original_bills_by_customer,customers)
        candidate_index=_bill_index(candidate_bills_by_customer,customers)
        bill_diff=_bill_differences(original_index,candidate_index)
        customer_diff=_customer_differences(original_groups,candidate_groups)
        bill_difference=bill_diff['original_only_count'] or bill_diff['candidate_only_count'] or bill_diff['changed_count']
        consistent=not customer_diff and not bill_difference
        equivalent=equivalent and consistent
        if consistent: summary='总额与客户小计、账单集合一致'
        elif customer_diff: summary='客户小计存在差异'
        else: summary='总额相同但两侧实际计入的账单不同'
        result.update({'equivalent':equivalent,'attributed':True,
            'customer_differences':customer_diff,
            'first_customer_difference':_first_customer_difference(customer_diff,bill_diff),
            'bill_attribution':bill_diff,
            'attribution':{'enabled':True,'checked':True,'consistent':consistent,
                'customers_checked':len(customers),
                'original_shape':original_plan['shape'],'candidate_shape':candidate_plan['shape'],
                'isolation':'REPEATABLE READ，结果、归因与账单在同一事务快照内读取',
                'comparison':'按客户核对每一项聚合，并追到两侧实际计入的账单；客户小计或账单集合任一不同即判差异',
                'summary':summary}})
    return result
