from datetime import datetime, timedelta
from flask import Flask, request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import NullPool

from core.site_config import SERVICE_DATABASE_URL, FAILURE_VALIDITY_PERIOD_IN_DAYS
from core.models import Session, TargetDepsysCode

app = Flask(__name__)

# NullPool prevents "RuntimeError: Event loop is closed".
# Flask's async support (asgiref) creates a temporary event loop per request
# and closes it on completion. SQLAlchemy's default pool holds connections
# across requests and tries to clean them up via asyncio.shield() after the
# loop is gone. NullPool opens and closes each connection entirely within the
# request's own event loop, eliminating the cross-loop cleanup.
engine = create_async_engine(SERVICE_DATABASE_URL, echo=True, poolclass=NullPool)
async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@app.route('/')
def index():
    return 'Flask service is running'


@app.route('/api/health')
def health():
    return {'status': 'ok'}


@app.route('/api/session', methods=['GET'])
async def get_session():
    token = request.args.get('token')
    if not token:
        return {'error': 'token is required'}, 400
    async with async_session_factory() as db:
        result = await db.execute(select(Session).where(Session.token == token))
        session_row = result.scalar_one_or_none()
        if session_row is None:
            return {'error': 'session not found'}, 404
        expired = session_row.token_expiry < datetime.now()
        return {
            'conversion_id': session_row.conversion_id,
            'expired': expired,
            'target_depsys': session_row.target_depsys.value,
            'related_bmrb_id': session_row.related_bmrb_id,
        }


@app.route('/api/session', methods=['PATCH'])
async def update_session():
    body = request.get_json(silent=True) or {}
    token = body.get('token')
    target_depsys_str = body.get('target_depsys')
    related_bmrb_id = body.get('related_bmrb_id')  # int or None

    if not token:
        return {'error': 'token is required'}, 400
    try:
        target_depsys = TargetDepsysCode(target_depsys_str)
    except (ValueError, TypeError):
        return {'error': 'invalid target_depsys'}, 400

    async with async_session_factory() as db:
        result = await db.execute(select(Session).where(Session.token == token))
        session_row = result.scalar_one_or_none()
        if session_row is None:
            return {'error': 'session not found'}, 404
        if session_row.conversion_id is not None:
            return {'error': 'session already submitted'}, 409
        session_row.target_depsys = target_depsys
        session_row.related_bmrb_id = related_bmrb_id
        await db.commit()
    return {}, 200


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
