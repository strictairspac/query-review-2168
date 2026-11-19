import os,time,json,traceback
from pathlib import Path
from sqlalchemy import select
from .config.database import Session
from .models.batch import Batch
from .models.sample import Sample
from .models.evidence import Evidence
from .services.batch import queue
from .services.query_engine import compare,QueryFailure,AttributionFailure
artifacts=Path(os.getenv('ARTIFACTS_DIR','/artifacts'))
heartbeat=artifacts/'worker.heartbeat'
artifacts.mkdir(parents=True,exist_ok=True)
def failure_result(exc):
    """Failure evidence keeps the reason and the stage that failed."""
    if isinstance(exc,QueryFailure):
        kind='timeout' if exc.timed_out else 'query'
        if exc.timed_out:
            message=('归因超时，未作结论' if exc.stage.endswith('_bills') else '查询超时，未作结论')+'：'+exc.message
        else:
            message=('账单归因查询失败' if exc.stage.endswith('_bills') else '查询失败')+'：'+exc.message
        return {'error':message,'error_kind':kind,'error_stage':exc.stage,'timed_out':exc.timed_out,'attributed':False}
    if isinstance(exc,AttributionFailure):
        return {'error':str(exc),'error_kind':'attribution','attributed':False}
    return {'error':str(exc)[:500],'error_kind':'internal','attributed':False}
def run_one(batch_id):
    with Session() as db:
        batch=db.get(Batch,batch_id)
        if not batch or db.scalar(select(Evidence).where(Evidence.batch_id==batch_id)):return
        sample=db.get(Sample,batch.sample_id);batch.status='running';db.commit()
        try:
            result=compare(sample.original_sql,batch.candidate_sql,attribution=bool(batch.attribution))
            batch.status='passed' if result['equivalent'] else 'different'
        except Exception as exc:
            result=failure_result(exc);batch.status='failed'
        db.add(Evidence(batch_id=batch_id,result=result));db.commit()
        (artifacts/f'review-{batch_id}.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
def main():
    while True:
        try:
            heartbeat.write_text(str(time.time()))
            item=queue.blpop('query-review:jobs',timeout=2)
            if item:run_one(int(item[1]))
            else:
                with Session() as db:pending=list(db.scalars(select(Batch.id).where(Batch.status.in_(['queued','running']))))
                for ident in pending:run_one(ident)
        except Exception:traceback.print_exc();time.sleep(2)
if __name__=='__main__':main()
