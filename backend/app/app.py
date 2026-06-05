from datetime import datetime, timedelta
from flask import Flask, request
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from core.site_config import SERVICE_DATABASE_URL, FAILURE_VALIDITY_PERIOD_IN_DAYS
from core.models import Session

app = Flask(__name__)

engine = create_async_engine(SERVICE_DATABASE_URL, echo=True, pool_pre_ping=True)
async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@app.route('/')
def index():
    return 'Flask service is running'


@app.route('/api/health')
def health():
    return {'status': 'ok'}


@app.route('/api/new_consent', methods=['POST'])
async def new_consent():
    async with async_session_factory() as db:
        new_session = Session(
            token_expiry=datetime.now() + timedelta(days=FAILURE_VALIDITY_PERIOD_IN_DAYS),
            consented=True,
            client_ip=request.remote_addr,
            user_agent=request.headers.get('User-Agent', ''),
        )
        db.add(new_session)
        await db.commit()
        await db.refresh(new_session)
        return {'token': str(new_session.token)}
