import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from flask import Flask, request, send_file
from git import Actor, InvalidGitRepositoryError, Repo
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from core.models import (
    OutputFile,
    OutputFileType,
    Session,
    SessionStatusCode,
    TargetDepsysCode,
    UploadFile,
    UploadFileSource,
    UploadFileType,
    Workflow,
    WfStatusCode,
    WfTaskCode,
)
from core.site_config import (
    ARCHIVE_BASE_PATH,
    BMRB_ENTRY_DIR_URL,
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

# Prefect REST API (the worker/server image, reached over the internal network).
# PREFECT_API_URL is set in .env (Prefect standard); fall back to the service name.
PREFECT_API_URL = os.environ.get('PREFECT_API_URL', 'http://prefect-server:4200/api')
PREFECT_DEPLOYMENT = 'process-session/default'


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


async def _fetch_bmrb_entry(bmrb_id: int) -> bytes:
    """Download the NMR-STAR V3 entry file (assigned chemical shifts) for a BMRB
    ID. Raises on any network/HTTP error or an empty body."""
    url = f'{BMRB_ENTRY_DIR_URL}/bmr{bmrb_id}/bmr{bmrb_id}_3.str'
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        resp = await client.get(url)
    resp.raise_for_status()
    if not resp.content:
        raise ValueError('empty BMRB entry file')
    return resp.content


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
            'target_depsys': session_row.target_depsys,
            'related_bmrb_id': session_row.related_bmrb_id,
        }


@app.route('/api/files', methods=['GET'])
async def get_files():
    """Return the selected upload files participating in the latest run.

    Query: token

    Files accumulate across runs; `selected` marks the set used by the current
    (latest) conversion. Ordered by upload time so the client renumbers them
    1..N. `uploaded_at` is converted to UTC: the column stores naive local time
    in the DB session timezone, so it is reinterpreted from that zone into UTC
    wall-clock time (site-independent).
    Returns: { files: [{ original_name, file_size, file_type, source, uploaded_at }] }
    """
    token = request.args.get('token')
    if not token:
        return {'error': 'token is required'}, 400

    async with async_session_factory() as db:
        session_row = (
            await db.execute(select(Session).where(Session.token == token))
        ).scalar_one_or_none()
        if session_row is None:
            return {'error': 'session not found'}, 404

        # naive_local AT TIME ZONE <db tz>  -> timestamptz (interpret as local)
        # ... AT TIME ZONE 'UTC'            -> naive UTC wall-clock time
        uploaded_at_utc = func.timezone(
            'UTC', func.timezone(func.current_setting('TIMEZONE'), UploadFile.uploaded_at)
        )
        result = await db.execute(
            select(
                UploadFile.original_name,
                UploadFile.file_size,
                UploadFile.file_type,
                UploadFile.source,
                uploaded_at_utc.label('uploaded_at_utc'),
            )
            .where(UploadFile.token == token, UploadFile.selected == True)  # noqa: E712
            .order_by(UploadFile.uploaded_at.asc(), UploadFile.ordinal.asc())
        )
        files = [
            {
                'original_name': row.original_name,
                'file_size': row.file_size,
                'file_type': row.file_type,
                'source': row.source,
                'uploaded_at': (
                    row.uploaded_at_utc.isoformat(sep=' ', timespec='minutes')
                    if row.uploaded_at_utc is not None
                    else None
                ),
            }
            for row in result.all()
        ]

    return {'files': files}, 200


# ── Processing progress (for the upload "Processing…" dialog) ───────────────────

# Tasks surfaced to the dialog, in display order, with their label and the
# human-readable log file (under the run's log/ dir) shown by "Show log file".
_PROGRESS_TASKS = [
    (WfTaskCode.convert_model, 'Coordinate conversion'),
    (WfTaskCode.convert_nmr_data, 'NMR data conversion'),
]
_TASK_LOG_FILE = {
    'convert_model': 'C_{cid}_model-check.log',
    'convert_nmr_data': 'C_{cid}-nmr-data.stdout.log',
}
_LOG_TAIL_BYTES = 64 * 1024


@app.route('/api/progress', methods=['GET'])
async def get_progress():
    """Per-task status of the session's latest processing run, for the dialog.

    Returns the convert_model / convert_nmr_data workflow statuses (plus the
    NmrDpUtility report_status/report_summary on convert_nmr_data) and a `done`
    flag. Token-scoped to the caller's own conversion.
    """
    token = request.args.get('token')
    if not token:
        return {'error': 'token is required'}, 400
    async with async_session_factory() as db:
        session_row = (
            await db.execute(select(Session).where(Session.token == token))
        ).scalar_one_or_none()
        if session_row is None:
            return {'error': 'session not found'}, 404
        conversion_id = session_row.conversion_id
        run_number = session_row.latest_run_number
        tasks = []
        if conversion_id is not None and run_number > 0:
            rows = {
                w.task: w
                for w in (
                    await db.execute(
                        select(Workflow).where(
                            Workflow.conversion_id == conversion_id,
                            Workflow.run_number == run_number,
                        )
                    )
                ).scalars().all()
            }
            for code, label in _PROGRESS_TASKS:
                w = rows.get(code.value)
                entry = {'task': code.value, 'label': label, 'status': w.status if w else None}
                if code is WfTaskCode.convert_nmr_data and w is not None:
                    entry['report_status'] = w.report_status
                    entry['report_summary'] = w.report_summary
                tasks.append(entry)
        done = bool(tasks) and all(t['status'] in ('completed', 'failed') for t in tasks)
        return {
            'conversion_id': conversion_id,
            'run_number': run_number,
            'session_status': session_row.status,
            'tasks': tasks,
            'done': done,
        }


@app.route('/api/log', methods=['GET'])
async def get_log():
    """Tail (~64 KB) of a task's human-readable log file, for "Show log file".

    `task` is restricted to a fixed allow-list mapped to a fixed filename built
    from the integer conversion_id, so there is no user-controlled path.
    """
    token = request.args.get('token')
    task = request.args.get('task')
    if not token or not task:
        return {'error': 'token and task are required'}, 400
    if task not in _TASK_LOG_FILE:
        return {'error': 'unknown task'}, 400
    async with async_session_factory() as db:
        session_row = (
            await db.execute(select(Session).where(Session.token == token))
        ).scalar_one_or_none()
        if session_row is None:
            return {'error': 'session not found'}, 404
        conversion_id = session_row.conversion_id
        run_number = session_row.latest_run_number

    if conversion_id is None or run_number < 1:
        return {'text': ''}
    log_dir = Path(WORKSPACE_BASE_PATH) / str(conversion_id) / str(run_number) / 'log'
    fpath = log_dir / _TASK_LOG_FILE[task].format(cid=conversion_id)
    text = ''
    try:
        if fpath.is_file():
            size = fpath.stat().st_size
            with open(fpath, 'rb') as fh:
                if size > _LOG_TAIL_BYTES:
                    fh.seek(size - _LOG_TAIL_BYTES)
                data = fh.read()
            text = data.decode('utf-8', errors='ignore')
            if size > _LOG_TAIL_BYTES:
                text = text.split('\n', 1)[-1]  # drop the partial leading line
    except OSError:
        text = ''
    return {'text': text}


@app.route('/api/coordinate', methods=['GET'])
async def get_coordinate():
    """Stream the session's latest-run converted mmCIF (output/C_<cid>_model.cif)
    for in-browser Mol* preview. Token-scoped.

    This is a PREVIEW, not the official download: it is read-only (SELECT +
    send_file) and never mutates output_file.downloaded / downloaded_at /
    client_ip / user_agent, nor locks the session. 404 when there is no pdbx
    output (bmrbdep, or not yet converted).
    """
    token = request.args.get('token')
    if not token:
        return {'error': 'token is required'}, 400

    async with async_session_factory() as db:
        session_row = (
            await db.execute(select(Session).where(Session.token == token))
        ).scalar_one_or_none()
        if session_row is None:
            return {'error': 'session not found'}, 404
        conversion_id = session_row.conversion_id
        run_number = session_row.latest_run_number
        if conversion_id is None or run_number < 1:
            return {'error': 'no coordinate available'}, 404

        output_row = (
            await db.execute(
                select(OutputFile).where(
                    OutputFile.conversion_id == conversion_id,
                    OutputFile.run_number == run_number,
                    OutputFile.file_type == OutputFileType.pdbx.value,
                )
            )
        ).scalar_one_or_none()

    if output_row is None:
        return {'error': 'no coordinate available'}, 404
    fpath = Path(output_row.stored_path)
    if not fpath.is_file():
        return {'error': 'coordinate file missing'}, 404

    # Inline (not as_attachment) — Mol* fetches it; conditional adds ETag/Range so
    # the browser caches the multi-MB file across summary re-entry.
    return send_file(
        str(fpath),
        mimetype='chemical/x-mmcif',
        as_attachment=False,
        download_name=f'C_{conversion_id}_model.cif',
        conditional=True,
    )


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
        selected_files = list(result.scalars().all())
        if not selected_files:
            return {'error': 'no files selected'}, 400

        # OneDep conventional mode: when a valid related BMRB ID is provided and
        # no user chemical-shift file is selected, fetch the assigned chemical
        # shifts (NMR-STAR V3 entry) from BMRB and add it to the archive as a
        # selected file so it participates in the run like any uploaded file.
        # Combined mode (an nm-uni-* file is selected) ignores the BMRB ID here.
        bmrb_id = session_row.related_bmrb_id
        has_uni = any(f.file_type.startswith('nm-uni-') for f in selected_files)
        has_user_shifts = any(
            f.file_type.startswith('nm-shi') and f.source != UploadFileSource.bmrb.value
            for f in selected_files
        )
        use_bmrb = (
            session_row.target_depsys == TargetDepsysCode.onedep
            and bmrb_id is not None
            and not has_uni
            and not has_user_shifts
        )
        bmrb_name = f'bmr{bmrb_id}_3.str' if bmrb_id is not None else None

        # Invariant: a BMRB-derived shift file is selected iff this run uses BMRB
        # shifts. Files accumulate across runs, so reuse a matching one and
        # deselect any stale BMRB file (wrong ID, or superseded by user-uploaded
        # shifts / combined mode). Reused/deselected rows are also synced into
        # the in-memory selected_files used to build the manifest below.
        existing_bmrb = (
            await db.execute(
                select(UploadFile).where(
                    UploadFile.token == token,
                    UploadFile.source == UploadFileSource.bmrb.value,
                )
            )
        ).scalars().all()
        reuse = None
        for row in existing_bmrb:
            if use_bmrb and row.original_name == bmrb_name and os.path.exists(row.stored_path):
                reuse = row
            elif row.selected:
                row.selected = False
                if row in selected_files:
                    selected_files.remove(row)

        if use_bmrb and reuse is not None:
            reuse.selected = True
            if reuse not in selected_files:
                selected_files.append(reuse)
        elif use_bmrb:
            try:
                content = await _fetch_bmrb_entry(bmrb_id)
            except Exception as exc:
                return {'error': f'failed to download BMRB entry {bmrb_id}: {exc}'}, 502
            session_dir = Path(ARCHIVE_BASE_PATH) / str(token)
            session_dir.mkdir(parents=True, exist_ok=True)
            result = await db.execute(
                select(func.max(UploadFile.ordinal)).where(UploadFile.token == token)
            )
            ordinal = (result.scalar_one_or_none() or 0) + 1
            stored_path = str(session_dir / f'{ordinal}_{bmrb_name}')
            Path(stored_path).write_bytes(content)
            _open_repo(session_dir)
            bmrb_row = UploadFile(
                token=token,
                ordinal=ordinal,
                run_number=session_row.latest_run_number + 1,
                original_name=bmrb_name,
                stored_path=stored_path,
                file_size=len(content),
                checksum=hashlib.sha256(content).hexdigest(),
                file_type=UploadFileType.nm_shi.value,
                selected=True,
                source=UploadFileSource.bmrb.value,
            )
            db.add(bmrb_row)
            selected_files.append(bmrb_row)

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
            'target_depsys': session_row.target_depsys,
            'files': [
                {
                    'ordinal': f.ordinal,
                    'original_name': f.original_name,
                    'stored_path': f.stored_path,
                    'file_type': f.file_type,
                    'selected': f.selected,
                    'source': f.source,
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

    # Trigger the Prefect flow run for this conversion+run via the Prefect REST
    # API (the backend has no prefect package). Best-effort: the run is already
    # committed, so a trigger failure is logged rather than failing the request —
    # the workflow rows stay 'pending' and the run can be re-triggered.
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            dep = await client.get(f'{PREFECT_API_URL}/deployments/name/{PREFECT_DEPLOYMENT}')
            dep.raise_for_status()
            run = await client.post(
                f'{PREFECT_API_URL}/deployments/{dep.json()["id"]}/create_flow_run',
                json={'parameters': {
                    'token': str(token),
                    'conversion_id': conversion_id,
                    'run_number': run_number,
                }},
            )
            run.raise_for_status()
        app.logger.info(
            'triggered Prefect flow run %s for C_%s run %s',
            run.json().get('id'), conversion_id, run_number,
        )
    except Exception as exc:  # noqa: BLE001
        app.logger.error(
            'failed to trigger Prefect flow for C_%s run %s: %s',
            conversion_id, run_number, exc,
        )

    return {'conversion_id': conversion_id, 'run_number': run_number, 'commit_sha': commit_sha}, 202
