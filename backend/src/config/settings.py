import os
DATABASE_URL=os.environ['DATABASE_URL']
REDIS_URL=os.getenv('REDIS_URL','redis://redis:6379/0')
JWT_SECRET=os.environ['JWT_SECRET']
STATEMENT_TIMEOUT_MS=int(os.getenv('QUERY_TIMEOUT_MS','2500'))
QUERY_READER_ROLE=os.getenv('QUERY_READER_ROLE','query_reader')
