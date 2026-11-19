from sqlalchemy import select
from fastapi import HTTPException
from ..models.evidence import Evidence
from ..models.batch import Batch
def serialize(x,batch=None):
    if batch is None:return {'id':x.id,'batch_id':x.batch_id,'result':x.result}
    return {'id':x.id,'batch_id':x.batch_id,'result':x.result,
            'batch':{'name':batch.name,'status':batch.status,'attribution':bool(batch.attribution)}}
def list_evidence(db):
    batches={b.id:b for b in db.scalars(select(Batch))}
    return [serialize(x,batches.get(x.batch_id)) for x in db.scalars(select(Evidence).order_by(Evidence.id.desc()))]
def get_evidence(db,batch_id):
    x=db.scalar(select(Evidence).where(Evidence.batch_id==batch_id))
    if not x:raise HTTPException(404,'证据尚未生成，请等待 worker 完成')
    return serialize(x,db.get(Batch,x.batch_id))
