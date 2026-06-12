import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, request
from git import Actor, InvalidGitRepositoryError, Repo
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from core.models import (
    Session,
    SessionStatusCode,
    TargetDepsysCode,
    UploadFile,
    Workflow,
    WfStatusCode,
    WfTaskCode,
)
from core.site_config import (
    ARCHIVE_BASE_PATH,
    CONV_ID_RANGE_BEGIN,
    CONV_ID_RANGE_END,
    FAILURE_VALIDITY_PERIOD_IN_DAYS,
    SERVICE_ADMIN_EMAIL,
    SERVICE_DATABASE_URL,
    SERVICE_HOST,
    WORKSPACE_BASE_PATH,
)

app = Flask(__name__)

# NullPool prevents "RuntimeError: Event loop is closed".
# Flask's async support (asgiref) creates a temporary event loop per request
# and closes it on completion. SQLAlchemy's default pool holds connections
# across requests and tries to clean them up via asyncio.shield() after the
# loop is gone. NullPool opens and closes each connection entirely within the
# request's own event loop, eliminating the cross-loop cleanup.
engine = create_async_engine(SERVICE_DATABASE_URL, echo=True, poolclass=NullPool)
async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

_GIT_ACTOR = Actor(SERVICE_HOST, SERVICE_ADMIN_EMAIL)


def _open_repo(session_dir: Path) -> Repo:
    """Return the git Repo for a session directory, initialising it on first use."""
    try:
        return Repo(str(session_dir))
    except InvalidGitRepositoryError:
        repo = Repo.init(str(session_dir))
        return repo


def _commit_run(repo: Repo, run_number: int) -> str:
    """Stage all changes and create a commit for one processing run."""
    repo.git.add(A=True)
    commit = repo.index.commit(
        f'Run #{run_number:04d} — {datetime.now().isoformat(timespec="seconds")}',
        author=_GIT_ACTOR,
        committer=_GIT_ACTOR,
    )
    return commit.hexsha


# ── Health ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return 'Flask service is running'


@app.route('/api/health')
def health():
    return {'status': 'ok'}


# ── Session ───────────────────────────────────────────────────────────────────

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


# ── Consent ───────────────────────────────────────────────────────────────────

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


# ── File upload ───────────────────────────────────────────────────────────────

@app.route('/api/upload', methods=['POST'])
async def upload():
    """Store an uploaded file in the session archive and create a DB record.

    Multipart form fields: token, file_type, file (binary).
    On the first upload for a session, git init is called for the session directory.
    Returns: { ordinal, stored_path, file_size }
    """
    token = request.form.get('token')
    file_type = request.form.get('file_type')
    f = request.files.get('file')

    if not all([token, file_type, f]):
        return {'error': 'token, file_type, and file are required'}, 400

    original_name = f.filename or 'unnamed'

    async with async_session_factory() as db:
        result = await db.execute(select(Session).where(Session.token == token))
        session_row = result.scalar_one_or_none()
        if session_row is None:
            return {'error': 'session not found'}, 404
        if session_row.token_expiry < datetime.now():
            return {'error': 'session expired'}, 410
        if session_row.downloaded:
            return {'error': 'session is locked after download'}, 409

        # Assign next ordinal for this session
        result = await db.execute(
            select(func.max(UploadFile.ordinal)).where(UploadFile.token == token)
        )
        max_ordinal = result.scalar_one_or_none() or 0
        ordinal = max_ordinal + 1

        # The draft (not-yet-processed) run is always latest_run_number + 1.
        draft_run = session_row.latest_run_number + 1

        # Build archive path
        session_dir = Path(ARCHIVE_BASE_PATH) / str(token)
        session_dir.mkdir(parents=True, exist_ok=True)
        stored_path = str(session_dir / f'{ordinal}_{original_name}')

        # Save file and initialise git repo (idempotent)
        f.save(stored_path)
        _open_repo(session_dir)

        file_size = os.path.getsize(stored_path)

        # Persist upload_file record
        db.add(UploadFile(
            token=token,
            ordinal=ordinal,
            run_number=draft_run,
            original_name=original_name,
            stored_path=stored_path,
            file_size=file_size,
            file_type=file_type,
            selected=True,
        ))

        await db.execute(
            update(Session)
            .where(Session.token == token)
            .values(status=SessionStatusCode.uploading)
        )
        await db.commit()

    return {'ordinal': ordinal, 'stored_path': stored_path, 'file_size': file_size}, 201


@app.route('/api/upload', methods=['DELETE'])
async def delete_upload():
    """Remove a file from the working set without losing already-processed files.

    JSON body: { token, ordinal }

    - If the file belongs to the current un-processed draft run
      (run_number > session.latest_run_number), it is hard-deleted from disk and DB
      (mistake correction before the file has ever participated in a run).
    - Otherwise the file has been part of a committed run: it is soft-deselected
      (selected = False), keeping the file on disk and the row in the DB.
    Returns: { action: 'deleted' | 'deselected' }
    """
    body = request.get_json(silent=True) or {}
    token = body.get('token')
    ordinal = body.get('ordinal')

    if not token or ordinal is None:
        return {'error': 'token and ordinal are required'}, 400

    async with async_session_factory() as db:
        session_row = (
            await db.execute(select(Session).where(Session.token == token))
        ).scalar_one_or_none()
        if session_row is None:
            return {'error': 'session not found'}, 404

        upload_row = (
            await db.execute(
                select(UploadFile).where(
                    UploadFile.token == token,
                    UploadFile.ordinal == ordinal,
                )
            )
        ).scalar_one_or_none()
        if upload_row is None:
            return {'error': 'upload file not found'}, 404

        if upload_row.run_number > session_row.latest_run_number:
            # Current draft, never processed — safe to remove entirely.
            try:
                os.remove(upload_row.stored_path)
            except FileNotFoundError:
                pass
            await db.delete(upload_row)
            action = 'deleted'
        else:
            # File has participated in a committed run — preserve it, just deselect.
            upload_row.selected = False
            action = 'deselected'

        await db.commit()

    return {'action': action}, 200


# ── Process ───────────────────────────────────────────────────────────────────

@app.route('/api/process', methods=['POST'])
async def process():
    """Commit the current upload state and trigger the conversion workflow.

    JSON body: { token }
    - Issues a conversion_id on first call.
    - Writes manifest.json into the session archive directory.
    - Creates a git commit (one per processing run).
    - Inserts Workflow task rows and updates session status to 'processing'.
    Returns: { conversion_id, run_number, commit_sha }
    """
    body = request.get_json(silent=True) or {}
    token = body.get('token')

    if not token:
        return {'error': 'token is required'}, 400

    async with async_session_factory() as db:
        result = await db.execute(select(Session).where(Session.token == token))
        session_row = result.scalar_one_or_none()
        if session_row is None:
            return {'error': 'session not found'}, 404
        if session_row.token_expiry < datetime.now():
            return {'error': 'session expired'}, 410
        if session_row.status == SessionStatusCode.processing:
            return {'error': 'already processing'}, 409
        if session_row.downloaded:
            return {'error': 'session is locked after download'}, 409

        # Require at least one selected file
        result = await db.execute(
            select(UploadFile).where(
                UploadFile.token == token,
                UploadFile.selected == True,
            )
        )
        selected_files = result.scalars().all()
        if not selected_files:
            return {'error': 'no files selected'}, 400

        # Issue conversion_id on first processing run
        conversion_id = session_row.conversion_id
        if conversion_id is None:
            result = await db.execute(
                select(func.max(Session.conversion_id)).where(
                    Session.conversion_id.between(CONV_ID_RANGE_BEGIN, CONV_ID_RANGE_END)
                )
            )
            max_id = result.scalar_one_or_none()
            conversion_id = (max_id if max_id is not None else CONV_ID_RANGE_BEGIN - 1) + 1
            if conversion_id > CONV_ID_RANGE_END:
                return {'error': 'conversion ID range exhausted'}, 503
            session_row.conversion_id = conversion_id

        # Link any not-yet-linked upload rows to the conversion_id (covers files
        # uploaded for later draft runs after the ID was first issued).
        await db.execute(
            update(UploadFile)
            .where(UploadFile.token == token, UploadFile.conversion_id.is_(None))
            .values(conversion_id=conversion_id)
        )

        # The run being processed is the current draft = latest_run_number + 1.
        run_number = session_row.latest_run_number + 1
        session_dir = Path(ARCHIVE_BASE_PATH) / str(token)
        repo = _open_repo(session_dir)

        # Write manifest.json
        manifest = {
            'token': str(token),
            'conversion_id': conversion_id,
            'run_number': run_number,
            'timestamp': datetime.now().isoformat(timespec='seconds'),
            'target_depsys': session_row.target_depsys.value,
            'files': [
                {
                    'ordinal': f.ordinal,
                    'original_name': f.original_name,
                    'stored_path': f.stored_path,
                    'file_type': f.file_type,
                    'selected': f.selected,
                }
                for f in selected_files
            ],
        }
        (session_dir / 'manifest.json').write_text(json.dumps(manifest, indent=2))

        # Git commit — captures all uploaded files + manifest for this run —
        # plus a lightweight tag run-<N> for easy per-run lookup / diffing.
        commit_sha = _commit_run(repo, run_number)
        repo.create_tag(f'run-{run_number}', ref=commit_sha)

        # Insert Workflow task records for this run. Ordinals reset to 1..3 per run;
        # the composite PK (conversion_id, run_number, ordinal) keeps re-runs distinct.
        # Logs live in the per-run conversion workspace (kept out of the git archive);
        # the workspace dirs themselves are created by the flow's issue_conversion task.
        # This path scheme mirrors prefect/flows/core/workspace.py — keep them in sync.
        log_dir = Path(WORKSPACE_BASE_PATH) / str(conversion_id) / str(run_number) / 'log'
        for i, task_code in enumerate(
            [WfTaskCode.issue_conversion, WfTaskCode.convert_model, WfTaskCode.convert_nmr_data],
            start=1,
        ):
            await db.execute(
                Workflow.__table__.insert().values(
                    conversion_id=conversion_id,
                    run_number=run_number,
                    ordinal=i,
                    task=task_code,
                    status=WfStatusCode.pending,
                    log_path=str(log_dir / f'{task_code.value}.log'),
                )
            )

        session_row.status = SessionStatusCode.processing
        session_row.started_at = datetime.now()
        session_row.latest_run_number = run_number
        await db.commit()

    # TODO: trigger Prefect flow run via Prefect API
    # from prefect.client.orchestration import get_client
    # async with get_client() as client:
    #     await client.create_flow_run_from_deployment(
    #         deployment_name='process-session/default',
    #         parameters={
    #             'token': token,
    #             'conversion_id': conversion_id,
    #             'run_number': run_number,
    #         },
    #     )

    return {'conversion_id': conversion_id, 'run_number': run_number, 'commit_sha': commit_sha}, 202
