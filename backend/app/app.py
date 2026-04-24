from datetime import datetime
from flask import Flask, request
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import sessionmaker,

from core/site_config import SERVICE_DATABASE_URL, FAILURE_VALIDITY_PERIOD_IN_DAYS
from core/models import Session

app = Flask(__name__)

# Create the asynchronous engine
engine = create_async_engine(SERVICE_DATABASE_URL, echo=True, pool_pre_ping=True)

# Configure the asynchronous session factory
async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

@app.route("/")
def index():
    return "Flask service is running"

@app.route("/health")
def health():
    return {"status": "ok"}

@app.route("/new_consent")
def new_consent():
    token = None

    async with async_session_factory() as session:

        new_session = Session(token_expiry = datetime.now() + timedelta(days=FAILURE_VALIDITY_PERIOD_IN_DAYS),
                              terms_consent = True,
                              client_ip = request.remote_addr
                              user_agent = request.user_agent)

        await session.add(new_session)

        await session.commit()

        token = new_session.token

    return {"token": token}
