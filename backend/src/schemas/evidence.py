from typing import Optional
from pydantic import BaseModel
class BatchSummary(BaseModel):
    name:str
    status:str
    attribution:bool
class EvidenceOutput(BaseModel):
    id:int
    batch_id:int
    result:dict
    batch:Optional[BatchSummary]=None
