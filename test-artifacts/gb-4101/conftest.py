import os,sys
from pathlib import Path
BACKEND=Path(__file__).resolve().parents[2]/'backend'
sys.path.insert(0,str(BACKEND))
os.environ.setdefault('DATABASE_URL','postgresql+psycopg2://app@127.0.0.1:17740/query_review')
os.environ.setdefault('REDIS_URL','redis://127.0.0.1:16379/0')
os.environ.setdefault('JWT_SECRET','test-secret')
os.environ.setdefault('SEED_DEMO','false')
os.environ.setdefault('ARTIFACTS_DIR','/tmp/query-review-artifacts')
