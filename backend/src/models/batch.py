from sqlalchemy.orm import Mapped,mapped_column
from sqlalchemy import String,Text,ForeignKey,DateTime,Boolean,func,false
from datetime import datetime
from ..config.database import Base
class Batch(Base):
    __tablename__='batches'
    id:Mapped[int]=mapped_column(primary_key=True)
    name:Mapped[str]=mapped_column(String(120),unique=True)
    sample_id:Mapped[int]=mapped_column(ForeignKey('samples.id'))
    candidate_sql:Mapped[str]=mapped_column(Text)
    attribution:Mapped[bool]=mapped_column(Boolean,default=False,server_default=false(),nullable=False)
    status:Mapped[str]=mapped_column(String(20),default='queued')
    created_at:Mapped[datetime]=mapped_column(DateTime,server_default=func.now())
