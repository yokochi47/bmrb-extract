import hashlib
import html
import io
import json
import os
import re
import smtplib
import traceback
import zipfile
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import httpx
from flask import Flask, request, send_file
from werkzeug.exceptions import HTTPException
from git import Actor, InvalidGitRepositoryError, Repo
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from core.models import (
    Communication,
    DeliveryStatusCode,
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
    SERVICE_HELP_EMAIL,
    SERVICE_HOST,
    SMTP_SERVER,
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


@app.errorhandler(Exception)
def handle_unexpected_error(exc):
    """Return unhandled server-side errors as JSON ({error: '<type>: <msg>'})
    instead of Flask's default HTML 500, so the frontend can surface an
    actionable detail. The full traceback is logged for backend debugging.
    Werkzeug HTTP errors (404, 405, ...) keep their standard responses."""
    if isinstance(exc, HTTPException):
        return exc
    app.logger.error(
        'unhandled error on %s %s:\n%s',
        request.method, request.path, traceback.format_exc(),
    )
    return {'error': f'{type(exc).__name__}: {exc}'}, 500


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


@app.route('/api/versions', methods=['GET'])
def get_versions():
    """Software/resource versions of the live conversion images, captured by the
    prefect-worker into the shared workspace (capture-versions flow). Read per
    request so a refresh shows up without a backend restart. Public, read-only."""
    path = Path(WORKSPACE_BASE_PATH) / 'versions.json'
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    return {'software': data.get('software', {}), 'resource': data.get('resource', {})}


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
            # Date (YYYY-MM-DD) the session and its results stay accessible.
            'token_expiry': session_row.token_expiry.date().isoformat(),
            'consented': bool(session_row.consented),
            'target_depsys': session_row.target_depsys,
            'related_bmrb_id': session_row.related_bmrb_id,
            'approved': bool(session_row.approved),
            'downloaded': bool(session_row.downloaded),
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


@app.route('/api/upload_files', methods=['GET'])
async def get_upload_files():
    """Return the editable working set for the Upload page.

    Query: token

    Unlike GET /api/files (which the summary page uses, filtered to selected
    files of the latest run), this returns every user-uploaded row for the
    token regardless of selection or run, with the fields needed to rebuild the
    upload table rows. BMRB-sourced rows are excluded — they are auto-managed at
    process time and are not user-editable.
    `uploaded_at` is the naive UTC wall-clock time (as in GET /api/files), best
    used to tell re-uploaded rows apart from a fresh upload.
    Returns: { files: [{ ordinal, original_name, file_size, file_type, selected,
    uploaded_at }] }
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

        # naive local (DB session tz) → UTC wall-clock time; see GET /api/files.
        uploaded_at_utc = func.timezone(
            'UTC', func.timezone(func.current_setting('TIMEZONE'), UploadFile.uploaded_at)
        )
        result = await db.execute(
            select(
                UploadFile.ordinal,
                UploadFile.original_name,
                UploadFile.file_size,
                UploadFile.file_type,
                UploadFile.selected,
                uploaded_at_utc.label('uploaded_at_utc'),
            )
            .where(
                UploadFile.token == token,
                UploadFile.source == UploadFileSource.user.value,
            )
            .order_by(UploadFile.uploaded_at.asc(), UploadFile.ordinal.asc())
        )
        files = [
            {
                'ordinal': row.ordinal,
                'original_name': row.original_name,
                'file_size': row.file_size,
                'file_type': row.file_type,
                'selected': bool(row.selected),
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
    'convert_nmr_data': 'C_{cid}_nmr-data.stdout.log',
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
        # `done` also requires a terminal session status: the flow marks the
        # convert_* tasks completed BEFORE harvesting output_file rows and
        # setting the session status (process_session: harvest + status update
        # run after the tasks). Gating on the session status guarantees that
        # when the dialog navigates to the summary, the converted coordinate /
        # NMR outputs are already registered — otherwise /api/coordinate and
        # /api/coordinate_validation race the harvest and return 404 / "not
        # available" on the summary page's first (and, for validation, only) load.
        done = (
            bool(tasks)
            and all(t['status'] in ('completed', 'failed') for t in tasks)
            and session_row.status in (SessionStatusCode.completed, SessionStatusCode.failed)
        )
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


def _download_output_rows(rows):
    """Filter a run's output_file rows to the set bundled in the download zip:
    the deposition files (coordinate, NMR-STAR, optional NEF) plus only the last
    conversion task's JSON report. The maxit coordinate-check log (text_report)
    and the intermediate JSON reports are dropped — the summary page still reads
    the full harvest, but the download carries just the final deliverables.

    "Last conversion task's JSON report" = the most recently written json_report,
    excluding the optional NMR-STAR→NEF release report (*-nef_release.json): that
    step runs last but is optional, so its report is ignored here. `rows` order is
    preserved.
    """
    json_rows = [
        r
        for r in rows
        if r.file_type == OutputFileType.json_report.value
        and 'nef_release' not in Path(r.stored_path).name
    ]
    chosen_json = None
    if json_rows:
        def _mtime(r):
            try:
                return Path(r.stored_path).stat().st_mtime
            except OSError:
                return -1.0
        chosen_json = max(json_rows, key=_mtime)

    kept = []
    for row in rows:
        if row.file_type == OutputFileType.text_report.value:
            continue
        if row.file_type == OutputFileType.json_report.value:
            if row is chosen_json:
                kept.append(row)
            continue
        kept.append(row)
    return kept


def _download_name(row, conversion_id):
    """User-facing name for a bundled output file. The JSON report is stored under
    an internal, task-specific name (…-str_deposit.json); present and archive it as
    C_<cid>_report.json (parallel to the PDF report) since the internal name is
    meaningless to the user. Other files keep their on-disk name."""
    if row.file_type == OutputFileType.json_report.value:
        return f'C_{conversion_id}_report.json'
    return Path(row.stored_path).name


@app.route('/api/download', methods=['GET'])
async def download_results():
    """Stream the session's latest-run conversion results as C_<cid>.zip and mark
    the session downloaded (Terms #7 / #8). Token-scoped.

    The zip bundles the deposition files (converted coordinate, NMR-STAR, optional
    NEF) and the last conversion task's JSON report (see _download_output_rows).
    Unlike GET /api/coordinate
    (a read-only preview), this is the official download: it requires the user to
    have approved the status, flips session.downloaded — which locks the session
    read-only (no further re-upload) — and stamps each output_file with
    downloaded / downloaded_at / client_ip / user_agent.
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
            return {'error': 'no conversion results available'}, 404
        if not session_row.approved:
            return {'error': 'results not approved for download'}, 409

        output_rows = (
            (
                await db.execute(
                    select(OutputFile)
                    .where(
                        OutputFile.conversion_id == conversion_id,
                        OutputFile.run_number == run_number,
                    )
                    .order_by(OutputFile.ordinal.asc())
                )
            )
            .scalars()
            .all()
        )

        # Block the download while either deferred step (NEF release, PDF report)
        # is still generating, so the Zip always carries them (the download page
        # also disables the button; this guards a direct fetch). A failed/absent
        # step (no longer pending/processing) does not block — both are best-effort.
        _busy = (WfStatusCode.pending.value, WfStatusCode.processing.value)
        deferred_status = {
            task: (
                await db.execute(
                    select(Workflow.status).where(
                        Workflow.conversion_id == conversion_id,
                        Workflow.run_number == run_number,
                        Workflow.task == task,
                    )
                )
            ).scalar_one_or_none()
            for task in (WfTaskCode.nef_release.value, WfTaskCode.convert_pdf.value)
        }
        has_pdf = any(r.file_type == OutputFileType.pdf_report.value for r in output_rows)
        has_nef = any(r.file_type == OutputFileType.nef.value for r in output_rows)
        if deferred_status[WfTaskCode.convert_pdf.value] in _busy and not has_pdf:
            return {'error': 'conversion report (PDF) is still being generated'}, 409
        if deferred_status[WfTaskCode.nef_release.value] in _busy and not has_nef:
            return {'error': 'NMR Exchange Format (NEF) file is still being generated'}, 409

        # Build the zip from the files present on disk (best-effort: skip a
        # missing row so a partial harvest still yields a usable archive).
        buf = io.BytesIO()
        added = 0
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            for row in _download_output_rows(output_rows):
                fpath = Path(row.stored_path)
                if fpath.is_file():
                    zf.write(str(fpath), arcname=_download_name(row, conversion_id))
                    added += 1
        if added == 0:
            return {'error': 'no conversion results available'}, 404
        buf.seek(0)

        # Record the download and lock the session read-only (Terms #7 / #8).
        await db.execute(
            update(OutputFile)
            .where(
                OutputFile.conversion_id == conversion_id,
                OutputFile.run_number == run_number,
            )
            .values(
                downloaded=True,
                downloaded_at=datetime.now(),
                client_ip=request.remote_addr,
                user_agent=request.headers.get('User-Agent', ''),
            )
        )
        session_row.downloaded = True
        await db.commit()

    return send_file(
        buf,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'C_{conversion_id}.zip',
    )


@app.route('/api/output_files', methods=['GET'])
async def get_output_files():
    """List the session's latest-run conversion result files — the contents of the
    C_<cid>.zip download: name, output_file_type and size. Token-scoped, read-only
    (mirrors GET /api/download's file selection, so only files present on disk are
    listed). Returns: { files: [{ name, file_type, file_size }] }
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
            return {'files': []}, 200

        rows = (
            (
                await db.execute(
                    select(OutputFile)
                    .where(
                        OutputFile.conversion_id == conversion_id,
                        OutputFile.run_number == run_number,
                    )
                    .order_by(OutputFile.ordinal.asc())
                )
            )
            .scalars()
            .all()
        )

        # The NEF release and the PDF report both run deferred (after the session
        # completes). Each is "still generating" while its workflow row is
        # pending/processing and its output has not been harvested yet — the
        # download page polls on these and blocks the Zip until both are ready.
        wf_status = {
            task: (
                await db.execute(
                    select(Workflow.status).where(
                        Workflow.conversion_id == conversion_id,
                        Workflow.run_number == run_number,
                        Workflow.task == task,
                    )
                )
            ).scalar_one_or_none()
            for task in (WfTaskCode.nef_release.value, WfTaskCode.convert_pdf.value)
        }

    _generating = (WfStatusCode.pending.value, WfStatusCode.processing.value)
    has_nef = any(row.file_type == OutputFileType.nef.value for row in rows)
    nef_generating = wf_status[WfTaskCode.nef_release.value] in _generating and not has_nef
    has_pdf = any(row.file_type == OutputFileType.pdf_report.value for row in rows)
    pdf_generating = wf_status[WfTaskCode.convert_pdf.value] in _generating and not has_pdf
    files = [
        {
            'name': _download_name(row, conversion_id),
            'file_type': row.file_type,
            'file_size': row.file_size,
        }
        for row in _download_output_rows(rows)
        if Path(row.stored_path).is_file()
    ]
    return {
        'files': files,
        'nef_generating': nef_generating,
        'pdf_generating': pdf_generating,
    }, 200


@app.route('/api/verify_email', methods=['POST'])
async def verify_email():
    """Validate a recipient address for the download page's "Send this URL by
    mail" (before the URL is emailed). Checks the address format and that its
    domain has MX records (deliverability); no SMTP-RCPT probe (unreliable /
    often blocked). Returns: { valid: bool }.

    JSON body: { email }
    """
    body = request.get_json(silent=True) or {}
    email = (body.get('email') or '').strip()
    if not email:
        return {'error': 'email is required'}, 400

    # Lazy import so the service keeps running if the image predates the
    # py3-validate-email dependency (rebuild required for this endpoint).
    try:
        from validate_email import validate_email as _validate_email
    except ImportError:
        return {'error': 'email validation unavailable'}, 503

    valid = bool(
        _validate_email(email, check_blacklist=False, check_smtp=False, dns_timeout=5)
    )
    return {'valid': valid}, 200


def _send_email(to_address: str, subject: str, content: str) -> str:
    """Send a plain-text email via the internal relay (port 25, best-effort).
    Returns the DeliveryStatusCode value ('sent' or 'failed')."""
    try:
        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From'] = SERVICE_ADMIN_EMAIL
        msg['To'] = to_address
        msg['Reply-To'] = SERVICE_HELP_EMAIL
        msg.set_content(content)
        with smtplib.SMTP(SMTP_SERVER, 25, timeout=15) as smtp:
            smtp.send_message(msg)
        return DeliveryStatusCode.sent.value
    except Exception as exc:  # noqa: BLE001
        app.logger.error('resume-url email to %s failed: %s', to_address, exc)
        return DeliveryStatusCode.failed.value


# Register the passwordless-login + annotator/admin auth blueprint. Injecting the
# session factory and mailer avoids a circular import with features.auth.
from features.auth import current_auth, init_auth  # noqa: E402
init_auth(app, async_session_factory, _send_email)


@app.route('/api/send_resume_url', methods=['POST'])
async def send_resume_url():
    """Email the session's resumable URL to a recipient and log it as a
    communication. JSON body: { token, email }. The address is re-validated
    (format + MX) server-side; the URL is https://<host>/info?token=<token>.
    Returns: { delivery_status }.
    """
    body = request.get_json(silent=True) or {}
    token = body.get('token')
    email = (body.get('email') or '').strip()
    if not token or not email:
        return {'error': 'token and email are required'}, 400

    # Re-validate server-side (don't trust the client's verified flag).
    try:
        from validate_email import validate_email as _validate_email
    except ImportError:
        return {'error': 'email validation unavailable'}, 503
    if not bool(_validate_email(email, check_blacklist=False, check_smtp=False, dns_timeout=5)):
        return {'error': 'invalid email address'}, 400

    async with async_session_factory() as db:
        session_row = (
            await db.execute(select(Session).where(Session.token == token))
        ).scalar_one_or_none()
        if session_row is None:
            return {'error': 'session not found'}, 404
        conversion_id = session_row.conversion_id
        if conversion_id is None:
            return {'error': 'session not yet processed'}, 409

        resume_url = f'https://{SERVICE_HOST}/info?token={token}'
        subject = f'Resume your NMR data conversion session (C_{conversion_id})'
        content = (
            f'You can resume or review your NMR data conversion session '
            f'(C_{conversion_id}) until it expires using this link:\n\n'
            f'{resume_url}\n\n'
            f'If you did not request this message, please ignore it.\n'
            f'For assistance, contact {SERVICE_HELP_EMAIL}.\n'
        )
        delivery_status = _send_email(email, subject, content)

        # Log it in the communication table (ordinal per conversion).
        max_ord = (
            await db.execute(
                select(func.max(Communication.ordinal)).where(
                    Communication.conversion_id == conversion_id
                )
            )
        ).scalar_one_or_none() or 0
        db.add(
            Communication(
                conversion_id=conversion_id,
                ordinal=max_ord + 1,
                subject=subject,
                content=content,
                email_address=email,
                delivery_status=delivery_status,
            )
        )
        await db.commit()

    if delivery_status != DeliveryStatusCode.sent.value:
        return {'error': 'failed to send email', 'delivery_status': delivery_status}, 502
    return {'delivery_status': delivery_status}, 200


# ── Coordinate geometry validation (pdbx_validate_* outliers in the converted mmCIF) ──

# Display order + friendly labels for the geometry-outlier categories maxit writes
# into the converted coordinate. Categories absent from a given file are skipped.
# pdbx_validate_planes_atom is folded into pdbx_validate_planes (nested atoms), so
# it is not listed here as a standalone metric.
# (category, label, description). The description is shown between the metric
# title and its outlier table on the summary page.
_VALIDATE_METRICS = [
    ('pdbx_validate_close_contact', 'Close contacts',
     'The following atoms were found to be less than 2.2 Å apart and are considered too '
     'close unless they are covalently bonded.'),
    ('pdbx_validate_symm_contact', 'Symmetry contacts',
     'The following atoms were found to be clashing with symmetry related atoms.'),
    ('pdbx_validate_rmsd_bond', 'Bond length outliers',
     'The following bond lengths were found to be significantly different from the '
     'expected bond length.'),
    ('pdbx_validate_rmsd_angle', 'Bond angle outliers',
     'The following bond angles were found to be significantly different from the '
     'expected bond angle.'),
    ('pdbx_validate_torsion', 'Torsion (Ramachandran) outliers',
     'The following backbone torsion (Ramachandran) outliers were identified.'),
    ('pdbx_validate_peptide_omega', 'Peptide omega outliers',
     'The following cis-peptides were detected in your coordinates. Please check these '
     'are expected.'),
    ('pdbx_validate_main_chain_plane', 'Main-chain planarity outliers',
     'The following main chain planarity outliers were identified.'),
    ('pdbx_validate_planes', 'Planarity outliers',
     'The following planarity outliers were identified.'),
    ('pdbx_validate_chiral', 'Chirality outliers',
     'The following atoms have unexpected chirality.'),
    ('pdbx_validate_polymer_linkage', 'Polymer linkage outliers',
     'The following bond lengths between adjacent residues were found to be significantly '
     'different from the expected bond length.'),
]

_MMCIF_TOKEN_RE = re.compile(r"'[^']*'|\"[^\"]*\"|\S+")


def _mmcif_tokens(line):
    """Tokenize an mmCIF data line, honoring '...'/"..." quoting; map ?/. to ''."""
    out = []
    for tok in _MMCIF_TOKEN_RE.findall(line):
        if len(tok) >= 2 and tok[0] in "'\"" and tok[-1] == tok[0]:
            tok = tok[1:-1]
        elif tok in ('?', '.'):
            tok = ''
        out.append(tok)
    return out


def _parse_validation_categories(path):
    """Extract pdbx_validate_* categories from an mmCIF file into
    {category: {'columns': [item...], 'rows': [[val...]]}}.

    Pure-Python and line-oriented; handles both loop_ tables and single-row
    key-value categories. These categories carry no multiline (;...;) text fields,
    so a whitespace tokenizer with quote handling is sufficient. Rows in a loop_
    may wrap across physical lines, so the data section is read as a flat token
    stream and chunked into rows of len(columns).
    """
    cats = {}
    try:
        lines = Path(path).read_text(errors='ignore').splitlines()
    except OSError:
        return cats
    _STOP = ('_', '#', ';')
    i, n = 0, len(lines)
    while i < n:
        stripped = lines[i].strip()
        if stripped == 'loop_':
            i += 1
            headers = []
            while i < n and lines[i].lstrip().startswith('_'):
                headers.append(lines[i].strip().split()[0])
                i += 1
            cat = headers[0].split('.', 1)[0].lstrip('_') if headers else ''
            items = [h.split('.', 1)[1] for h in headers if '.' in h]
            target = cat.startswith('pdbx_validate_')
            toks = []
            while i < n:
                ds = lines[i].strip()
                if not ds or ds[0] in _STOP or ds in ('loop_', 'stop_') or ds.startswith(('data_', 'save_')):
                    if ds == '':
                        i += 1
                        continue
                    break
                if target:
                    toks.extend(_mmcif_tokens(lines[i]))
                i += 1
            if target and items:
                ncol = len(items)
                cats[cat] = {
                    'columns': items,
                    'rows': [toks[j:j + ncol] for j in range(0, len(toks) - ncol + 1, ncol)],
                }
        elif stripped.startswith('_pdbx_validate_'):
            # single-row key-value form: "_cat.item  value" (value may wrap to next line)
            toks = _mmcif_tokens(lines[i])
            tag = toks[0]
            cat = tag.split('.', 1)[0].lstrip('_')
            item = tag.split('.', 1)[1] if '.' in tag else tag
            if len(toks) > 1:
                val = toks[1]
            else:
                val = ''
                if i + 1 < n and not lines[i + 1].lstrip().startswith('_'):
                    nxt = _mmcif_tokens(lines[i + 1])
                    if nxt:
                        val = nxt[0]
                        i += 1
            d = cats.setdefault(cat, {'columns': [], 'rows': [[]]})
            d['columns'].append(item)
            d['rows'][0].append(val)
            i += 1
        else:
            i += 1
    return cats


def _join(get, *items, sep=' '):
    """Join non-empty item values into one cell (e.g. 'A ASP 84 OD1')."""
    return sep.join(v for v in (get(it) for it in items) if v).strip()


# Curated columns per metric: (headers, [builder(get)->str ...]). `get(item)`
# returns a cell value for the current row. Metrics not listed fall back to the
# generic all-populated-columns renderer.
_CURATION = {
    'pdbx_validate_close_contact': (
        ['Model #', 'Atom 1', 'Atom 2', 'Distance (Å)'],
        [
            lambda g: g('PDB_model_num'),
            lambda g: _join(g, 'auth_asym_id_1', 'auth_comp_id_1', 'auth_seq_id_1', 'auth_atom_id_1'),
            lambda g: _join(g, 'auth_asym_id_2', 'auth_comp_id_2', 'auth_seq_id_2', 'auth_atom_id_2'),
            lambda g: g('dist'),
        ],
    ),
    'pdbx_validate_rmsd_bond': (
        ['Model #', 'Auth_asym_ID', 'Bond', 'Value (Å)', 'Deviation (Å)'],
        [
            lambda g: g('PDB_model_num'),
            lambda g: g('auth_asym_id_1'),
            lambda g: f"{_join(g, 'auth_comp_id_1', 'auth_seq_id_1')}: "
                      f"{g('auth_atom_id_1')}–{g('auth_atom_id_2')}",
            lambda g: g('bond_value'),
            lambda g: g('bond_deviation'),
        ],
    ),
    'pdbx_validate_rmsd_angle': (
        ['Model #', 'Auth_asym_ID', 'Residue', 'Atoms', 'Value (°)', 'Deviation (°)'],
        [
            lambda g: g('PDB_model_num'),
            lambda g: g('auth_asym_id_1'),
            lambda g: _join(g, 'auth_comp_id_1', 'auth_seq_id_1'),
            lambda g: f"{g('auth_atom_id_1')}-{g('auth_atom_id_2')}-{g('auth_atom_id_3')}",
            lambda g: g('angle_value'),
            lambda g: g('angle_deviation'),
        ],
    ),
    'pdbx_validate_torsion': (
        ['Model #', 'Auth_asym_ID', 'Residue', 'Phi (°)', 'Psi (°)'],
        [
            lambda g: g('PDB_model_num'),
            lambda g: g('auth_asym_id'),
            lambda g: _join(g, 'auth_comp_id', 'auth_seq_id'),
            lambda g: g('phi'),
            lambda g: g('psi'),
        ],
    ),
    'pdbx_validate_peptide_omega': (
        ['Model #', 'Auth_asym_ID', 'Residues', 'Omega (°)'],
        [
            lambda g: g('PDB_model_num'),
            lambda g: g('auth_asym_id_1'),
            lambda g: f"{_join(g, 'auth_comp_id_1', 'auth_seq_id_1')}–"
                      f"{_join(g, 'auth_comp_id_2', 'auth_seq_id_2')}",
            lambda g: g('omega'),
        ],
    ),
    'pdbx_validate_main_chain_plane': (
        ['Model #', 'Auth_asym_ID', 'Residue', 'Improper torsion angle (°)'],
        [
            lambda g: g('PDB_model_num'),
            lambda g: g('auth_asym_id'),
            lambda g: _join(g, 'auth_comp_id', 'auth_seq_id'),
            lambda g: g('improper_torsion_angle'),
        ],
    ),
    'pdbx_validate_planes': (
        ['Model #', 'Auth_asym_ID', 'Residue', 'RMSD (Å)', 'Type'],
        [
            lambda g: g('PDB_model_num'),
            lambda g: g('auth_asym_id'),
            lambda g: _join(g, 'auth_comp_id', 'auth_seq_id'),
            lambda g: g('rmsd'),
            lambda g: g('type'),
        ],
    ),
}


def _row_getter(data):
    """Return a function(row_index) -> get(item) for the parsed category `data`."""
    idx = {item: k for k, item in enumerate(data['columns'])}
    rows = data['rows']

    def for_row(r):
        row = rows[r]
        return lambda item: row[idx[item]] if item in idx and idx[item] < len(row) else ''

    return for_row


def _generic_columns(data):
    """All populated columns of a category (drop columns empty in every row, but
    keep 'id'); returns (columns, rows)."""
    items, rows = data['columns'], data['rows']
    keep = [
        k for k, item in enumerate(items)
        if item == 'id' or any(k < len(row) and row[k] for row in rows)
    ]
    cols = [items[k] for k in keep]
    out = [[row[k] if k < len(row) else '' for k in keep] for row in rows]
    return cols, out


def _plane_atoms(cats):
    """Map a pdbx_validate_planes id -> [atom labels] from pdbx_validate_planes_atom
    (the child category), if present. The child references the parent plane id via
    an item whose name contains 'planes_id'/'plane_id'."""
    data = cats.get('pdbx_validate_planes_atom')
    if not data:
        return {}
    cols = data['columns']
    link = next((c for c in cols if 'planes_id' in c or 'plane_id' in c), None)
    atom = next((c for c in cols if c in ('auth_atom_id', 'label_atom_id')), None)
    if link is None or atom is None:
        return {}
    li, ai = cols.index(link), cols.index(atom)
    mapping = {}
    for row in data['rows']:
        if li < len(row) and ai < len(row) and row[ai]:
            mapping.setdefault(row[li], []).append(row[ai])
    return mapping


def _curate_metrics(cats):
    """Build the ordered, present-only list of curated metric tables from parsed
    categories. pdbx_validate_planes gets its atoms nested from planes_atom."""
    metrics = []
    plane_atoms = _plane_atoms(cats)
    for cat, label, description in _VALIDATE_METRICS:
        data = cats.get(cat)
        if not data or not data['rows']:
            continue
        spec = _CURATION.get(cat)
        if spec:
            columns, builders = spec
            get_for = _row_getter(data)
            rows = [[b(get_for(r)) for b in builders] for r in range(len(data['rows']))]
        else:
            columns, rows = _generic_columns(data)
        metric = {'key': cat[len('pdbx_validate_'):], 'label': label,
                  'description': description, 'count': len(rows), 'columns': columns}
        if cat == 'pdbx_validate_planes' and plane_atoms:
            id_idx = data['columns'].index('id') if 'id' in data['columns'] else None
            nested = []
            for r, cells in enumerate(rows):
                pid = data['rows'][r][id_idx] if id_idx is not None and id_idx < len(data['rows'][r]) else None
                nested.append({'cells': cells, 'atoms': plane_atoms.get(pid, [])})
            metric['nested'] = True
            metric['rows'] = nested
        else:
            metric['rows'] = rows
        metrics.append(metric)
    return metrics


@app.route('/api/coordinate_validation', methods=['GET'])
async def get_coordinate_validation():
    """Geometry-outlier tables parsed from the session's latest-run converted mmCIF.

    Token-scoped, read-only (SELECT + file read; mutates nothing). Always 200
    except 400 for a missing token: `available` distinguishes "no coordinate"
    (bmrbdep / not converted) from "converted, zero outliers" (available + empty
    metrics). See _VALIDATE_METRICS for the categories surfaced.
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
            return {'available': False, 'metrics': []}

        output_row = (
            await db.execute(
                select(OutputFile).where(
                    OutputFile.conversion_id == conversion_id,
                    OutputFile.run_number == run_number,
                    OutputFile.file_type == OutputFileType.pdbx.value,
                )
            )
        ).scalar_one_or_none()

    if output_row is None or not Path(output_row.stored_path).is_file():
        return {'available': False, 'metrics': []}

    cats = _parse_validation_categories(output_row.stored_path)
    return {'available': True, 'metrics': _curate_metrics(cats)}


# ── NMR data validation (NmrDpUtility error/warning report) ────────────────────

# Error types that block a deposition (the "real" errors). Mirrors the flow's
# _BLOCKING_ERROR_TYPES / utils_nmr.nmr_dp_report_is_real_error.
_NMR_BLOCKING_ERROR_TYPES = (
    'format_issue',
    'coordinate_issue',
    'content_mismatch',
    'missing_mandatory_content',
    'sequence_mismatch',
    'atom_not_found',
    'hydrogen_not_instantiated',
)

# Warnings already auto-remediated (level 0). Mirrors the flow's
# _IGNORABLE_WARNING_TYPES / utils_nmr.ignorable_warning_types.
_NMR_IGNORABLE_WARNING_TYPES = (
    'atom_nomenclature_mismatch',
    'auth_atom_nomenclature_mismatch',
    'ccd_mismatch',
    'corrected_format_issue',
    'disordered_index',
    'enum_mismatch_ignorable',
    'skipped_saveframe_category',
    'skipped_loop_category',
)

_NMR_WARNING_LEVEL_3 = (
    'concatenated_sequence', 'not_superimposed_model', 'exactly_overlaid_model',
    'conflicted_data', 'conflicted_mr_data', 'conflicted_peak_list',
    'encouragement', 'unsupported_mr_data', 'unsupported_peak_list',
)

# Level → header color (0 added vs the Django original; user's scheme).
_NMR_WARNING_COLORS = {0: 'lightgray', 1: 'lightyellow', 2: 'khaki', 3: 'gold', 4: 'orange'}


def _nmr_warning_level(wtype):
    """Port of utils_nmr.nmr_dp_report_get_warning_level."""
    if wtype == 'total' or wtype in _NMR_IGNORABLE_WARNING_TYPES:
        return 0
    if 'missing' in wtype or 'anomalous' in wtype:
        return 4
    if wtype in _NMR_WARNING_LEVEL_3:
        return 3
    if wtype in ('unusual/rare_data', 'insufficient_data', 'unsupported_mr_data', 'unsupported_peak_list'):
        return 1
    if 'data' in wtype or 'skipped' in wtype or wtype == 'inconsistent_peak_list':
        return 2
    return 1


def _nmr_title(item):
    """Port of utils_nmr.nmr_dp_report_title (constraint->restraint, capitalize)."""
    s = item.replace('constraint', 'restraint')
    return s[0].upper() + s[1:].replace('_', ' ') if s else s


def _nmr_loc(item, model_file_name):
    """Port of utils_nmr.nmr_dp_report_loc -> location HTML for one item. Values are
    HTML-escaped; <b> labels and newlines preserved."""
    e = html.escape
    ret = ''
    rl = item.get('row_locations')
    rl1 = item.get('row_location')
    if isinstance(rl, dict):
        for k, v in rl.items():
            ret += '<b>' + e(str(k)) + ':</b>&nbsp;'
            for r in v:
                ret += e(str(r)) + ' and '
            ret = ret[:-5] + ', '
    elif isinstance(rl1, dict):
        for k, v in rl1.items():
            ret += '<b>' + e(str(k)) + ':</b>&nbsp;' + e(str(v)) + ', '
    if 'category' in item:
        if ret:
            ret += '\n'
        ret += '<b>Category:</b>&nbsp;' + e(str(item['category'])) + ', '
    if 'sf_framecode' in item:
        if ret:
            ret += '\n'
        sf = item['sf_framecode']
        ret += '<b>Saveframe:</b>&nbsp;' + (e(str(sf)) if sf else '?') + ', '
    if 'file_name' in item:
        fn = e(str(item['file_name']))
        if 'inheritable' in item:
            ret += '<b>Uploaded&nbsp;exptl.&nbsp;file:</b>&nbsp;' + fn + ', '
        elif item['file_name'] == model_file_name:
            if ret:
                ret += '\n'
            ret += '<b>Coordinate&nbsp;file:</b>&nbsp;' + fn + ', '
        elif not ret:
            ret += '<b>NMR&nbsp;data&nbsp;file:</b>&nbsp;' + fn + ', '
    return ret[:-2] if ret else ''


def _nmr_describe(item, is_error):
    """Description cell HTML: optional Subtotal label + description; for errors wrap a
    trailing '[Syntax error]…' in <pre> (port of nmr_dp_report_handle_syntax_error)."""
    parts = ''
    subtotal = item.get('subtotal')
    if isinstance(subtotal, int) and subtotal > 1:
        parts += f'<b>Subtotal:</b> {subtotal}<br />'
    desc = str(item.get('description', ''))
    if is_error and 'Syntax error' in desc:
        idx = desc.index('[Syntax error]')
        parts += html.escape(desc[:idx]) + '<pre>' + html.escape(desc[idx:]) + '</pre>'
    else:
        parts += html.escape(desc)
    return parts


def _nmr_model_file_name(report):
    """Coordinate file name from the report's input_sources (content_type=='model')."""
    for src in (report.get('information', {}).get('input_sources') or []):
        if isinstance(src, dict) and src.get('content_type') == 'model':
            return src.get('file_name')
    return None


def _parse_nmr_report(report, combined, model_file_name):
    """Build (errors, warnings) groups from an NmrDpUtility JSON report. errors:
    every type except 'total' (real = combined or designated type). warnings: from
    `warning` grouped by level, plus `corrected_warning` as level 0 (corrected)."""
    errors = []
    for etype, items in (report.get('error') or {}).items():
        if etype == 'total' or not items:
            continue
        real = combined or etype in _NMR_BLOCKING_ERROR_TYPES
        if etype == 'internal_error':
            rows = [{'location': '', 'description': html.escape(str(m)), 'active': True}
                    for m in items]
        else:
            rows = [{'location': _nmr_loc(m, model_file_name),
                     'description': _nmr_describe(m, True), 'active': real}
                    for m in items if isinstance(m, dict)]
        errors.append({'type': etype, 'title': _nmr_title(etype), 'real': real,
                       'count': len(items), 'rows': rows})
    errors.sort(key=lambda g: not g['real'])  # real errors first

    def _warn_groups(src, corrected):
        out = []
        for wtype, items in (src or {}).items():
            if wtype == 'total' or not items:
                continue
            level = 0 if corrected else _nmr_warning_level(wtype)
            rows = [{'location': _nmr_loc(m, model_file_name),
                     'description': _nmr_describe(m, False),
                     'active': m.get('status') == 'A'}
                    for m in items if isinstance(m, dict)]
            out.append({'type': wtype, 'title': _nmr_title(wtype), 'level': level,
                        'color': _NMR_WARNING_COLORS[level], 'count': len(items),
                        'corrected': corrected, 'rows': rows})
        return out

    warnings = _warn_groups(report.get('warning'), False) + \
        _warn_groups(report.get('corrected_warning'), True)
    warnings.sort(key=lambda g: (-g['level'], g['corrected']))
    return errors, warnings


def _nmr_combined_dep(token, target_depsys):
    """NMR_COMBINED_DEP = (onedep & nm-uni-* present) | repl_cs — same as the flow's
    onedep_combined; the uni-file check reads the run's manifest."""
    if target_depsys == TargetDepsysCode.repl_cs.value:
        return True
    if target_depsys != TargetDepsysCode.onedep.value:
        return False
    try:
        manifest = json.loads((Path(ARCHIVE_BASE_PATH) / str(token) / 'manifest.json').read_text())
        return any(
            str(f.get('file_type', '')).startswith('nm-uni-') for f in manifest.get('files', [])
        )
    except Exception:  # noqa: BLE001
        return False


@app.route('/api/nmr_validation', methods=['GET'])
async def get_nmr_validation():
    """Error/warning tables from the session's latest-run final NmrDpUtility report.

    The report is the convert_nmr_data workflow row's log_path (the last task's JSON
    per mode). Token-scoped, read-only (SELECT + file read; mutates nothing). Always
    200 (400 only for a missing token); `available` is false when there is no NMR
    report yet (not processed).
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
        target_depsys = session_row.target_depsys
        if conversion_id is None or run_number < 1:
            return {'available': False}

        wf = (
            await db.execute(
                select(Workflow).where(
                    Workflow.conversion_id == conversion_id,
                    Workflow.run_number == run_number,
                    Workflow.task == WfTaskCode.convert_nmr_data.value,
                )
            )
        ).scalar_one_or_none()

    if wf is None or not wf.log_path:
        return {'available': False}
    report_path = Path(wf.log_path)
    if not report_path.is_file():
        return {'available': False}
    try:
        report = json.loads(report_path.read_text())
    except Exception:  # noqa: BLE001
        return {'available': False}

    combined = _nmr_combined_dep(token, target_depsys)
    errors, warnings = _parse_nmr_report(report, combined, _nmr_model_file_name(report))
    return {
        'available': True,
        'status': report.get('information', {}).get('status'),
        'errors': errors,
        'warnings': warnings,
    }


# ── NMR data preview (graphical overview from the same NmrDpUtility report) ─────

_NMR_CONTENT_NAMES = {
    'model': 'Coordinates',
    'nmr-data-nef': 'NMR data (NEF)',
    'nmr-data-str': 'NMR data (NMR-STAR)',
    'nmr-chemical-shifts': 'Assigned chemical shifts',
    'nmr-restraints': 'NMR restraints',
    'nmr-peaks': 'Spectral peak lists',
}
_NMR_SUBTYPE_NAMES = {
    'poly_seq': 'Covalent bonds',
    'entity': 'Entity',
    'coordinate': 'Coordinates',
    'chem_shift': 'Assigned chemical shifts',
    'chem_shift_ref': 'Chemical shift references',
    'dist_restraint': 'Distance restraints',
    'dihed_restraint': 'Dihedral angle restraints',
    'rdc_restraint': 'RDC restraints',
    'spectral_peak': 'Spectral peak lists',
    'spectral_peak_alt': 'Spectral peak lists (alt.)',
}
_SUPERSCRIPT = str.maketrans('0123456789', '⁰¹²³⁴⁵⁶⁷⁸⁹')
_ISOTOPE_RE = re.compile(r'(\d+)([a-zA-Z]+)')

def _normalize_label(key):
    """1. Format an isotope-bearing key (e.g. '1h_chemical_shifts', 'all_13c_…',
    '15n') into a superscript-mass element label like ¹H / ¹³C / ¹⁵N.
    2. Prettify a constraint-type key, e.g. 'medium_range_constraints_backbone-backbone'
    → 'Medium range (bb-bb)'."""
    m = _ISOTOPE_RE.search(key)
    if m:
        return m.group(1).translate(_SUPERSCRIPT) + m.group(2).upper()
    key = key[0].upper() + key[1:]
    key = key.replace('_constraints', '')\
	.replace('backbone-backbone', '(bb-bb)')\
        .replace('backbone-sidechain', '(bb-sc)')\
	.replace('sidechain-sidechain', '(sc-sc)')
    return key.replace('_', ' ').strip()

def _annotation_x(value, rov, inverse=False):
    """Precise fractional position of `value` on the hidden marker axis (xAxis
    index 1, spanning 0 … n+1) that overlays the histogram's category axis. Bin
    i's band centre (index i) is the bin midpoint v_i + step/2, so `value` sits
    at (value - r0)/(rn - r0) * n. Marking this exact spot (rather than snapping
    to the bin) spreads multiple outliers that fall in the same bin, so their
    labels no longer stack on one line. Clamped to the plot extent (band edges of
    the first/last bins)."""
    r0, rn, n = rov[0], rov[-1], len(rov)
    if rn == r0:
        return 0.0
    frac = (value - r0) / (rn - r0) * n
    return max(0.0, min(n + 1.0, n + 1.0 - frac if inverse else frac))


def _histogram_annotations(h, inverse=False):
    """Per-outlier annotations for a normalized chemical-shift histogram: a dashed
    marker at each anomalous/unusual value (by Z score) with a short description.
    Empty for histograms without Z-score annotations."""
    rov = h.get('range_of_values') or []
    ann = h.get('annotations') or []
    if len(rov) < 2 or not ann:
        return []
    scale = rov[1] - rov[0]
    out = []
    for a in sorted(ann, key=lambda x: -(x.get('z_score') or 0)):
        z = a.get('z_score')
        if not isinstance(z, (int, float)) or not scale:
            continue
        out.append({
            'x': _annotation_x(z, rov, inverse),
            'anomalous': a.get('level') == 'anomalous',
            'text': (f"{a.get('chain_id')}:{a.get('seq_id')}:{a.get('comp_id')}:"
                     f"{a.get('atom_id')}, {a.get('value')} ppm, Z score {z}"),
        })
    return out


def _discrepancy_ann(h, text_fn, inverse=False):
    """Per-outlier annotations for a *-discrepancy histogram: a dashed marker at
    each redundant-restraint outlier's discrepancy value (red for conflicts),
    labelled by `text_fn(a, discrepancy)`. Shared by the distance, dihedral-angle
    and RDC discrepancy charts."""
    rov = h.get('range_of_values') or []
    ann = h.get('annotations') or []
    if len(rov) < 2 or not ann:
        return []
    scale = rov[1] - rov[0]
    out = []
    for a in sorted(ann, key=lambda x: x.get('discrepancy') or 0):
        d = a.get('discrepancy')
        if not isinstance(d, (int, float)) or not scale:
            continue
        out.append({
            'x': _annotation_x(d, rov, inverse),
            'anomalous': a.get('level') == 'conflict',
            'text': text_fn(a, d),
        })
    return out


def _dist_discrepancy_text(a, d):
    """Atom-pair label for a distance-restraint discrepancy outlier. Mirrors
    misc/utils_nmr.py::nmr_dp_report_dist_discrepancy_to_chart_label."""
    text = f"{a.get('chain_id_1')}:{a.get('seq_id_1')}:{a.get('comp_id_1')}:{a.get('atom_id_1')} - "
    if 'chain_id_2' in a:
        text += f"{a.get('chain_id_2')}:{a.get('seq_id_2')}:{a.get('comp_id_2')}:"
    elif 'seq_id_2' in a:
        text += f"{a.get('seq_id_2')}:{a.get('comp_id_2')}:"
    return text + f"{a.get('atom_id_2')}, {d}"


def _dihed_discrepancy_text(a, d):
    """Single-residue, four-atom label for a dihedral-restraint discrepancy
    outlier. Mirrors nmr_dp_report_dihed_discrepancy_to_chart_label."""
    atoms = '-'.join(str(a[k]) for k in ('atom_id_1', 'atom_id_2', 'atom_id_3', 'atom_id_4') if k in a)
    return f"{a.get('chain_id')}:{a.get('seq_id')}:{a.get('comp_id')}:{atoms}, {d}"


def _rdc_discrepancy_text(a, d):
    """Single-residue, atom-pair label for an RDC-restraint discrepancy outlier.
    Mirrors nmr_dp_report_rdc_discrepancy_to_chart_label."""
    return (f"{a.get('chain_id')}:{a.get('seq_id')}:{a.get('comp_id')}:"
            f"{a.get('atom_id_1')}-{a.get('atom_id_2')}, {d}")


def _discrepancy_annotations(h, inverse=False):
    return _discrepancy_ann(h, _dist_discrepancy_text, inverse)


def _dihed_discrepancy_annotations(h, inverse=False):
    return _discrepancy_ann(h, _dihed_discrepancy_text, inverse)


def _rdc_discrepancy_annotations(h, inverse=False):
    return _discrepancy_ann(h, _rdc_discrepancy_text, inverse)


def _histogram_chart(stat_list, inverse=False, annotate=_histogram_annotations):
    """Build [{label, categories, series, annotations}] from a stats list's
    `histogram` ({range_of_values, number_of_values: {key: [counts]}}). All-zero
    series are dropped; `annotate` marks outliers (chem-shift Z scores by default,
    distance discrepancies for the discrepancy histogram)."""
    charts = []
    for st in stat_list or []:
        h = st.get('histogram')
        if not isinstance(h, dict) or not h.get('range_of_values'):
            continue
        rov = h.get('range_of_values')
        scale = rov[1] - rov[0]
        categories = [f'({v + scale}, {v}]' if inverse else f'[{v}, {v + scale})'
                      for v in h['range_of_values']]
        nov = h.get('number_of_values') or {}
        # Order distance constraint-type series canonically (no-op for others).
        series = [
            {'name': _normalize_label(k), 'data': v}
            for k, v in sorted(nov.items(), key=lambda kv: _constraint_order(kv[0]))
            if isinstance(v, list) and any(v)
        ]
        if series:
            charts.append({'label': st.get('sf_framecode', ''),
                           'categories': categories, 'series': series,
                           'annotations': annotate(h, inverse)})
    return charts


def _dihedral_charts(stat_list):
    """Build [{label, phi_psi, chi1_chi2}] scatter+error data from a
    dihed_restraint stats list. Each plot → {groups:[{comp_id, points:[{x,y,seq_id}],
    errors:[[...]]}]}. `plot['values']`/`plot['errors']` are keyed by comp_id, so
    points and their error bars stay grouped per residue type (one scatter + one
    error-bar series each, sharing a name so the legend toggles both). Error
    arrays are [x, y, x_low, x_high, y_low, y_high] (absolute)."""
    def _plot(plot):
        if not isinstance(plot, dict) or not plot.get('values'):
            return None
        errors_by_comp = plot.get('errors') or {}
        groups = []
        for comp_id, vals in plot['values'].items():
            pts = [{'x': p[0], 'y': p[1], 'seq_id': ':'.join(p[2].split(':')[:2]) + ':'} for p in vals if len(p) >= 3]
            if pts:
                groups.append({'comp_id': comp_id, 'points': pts,
                               'errors': errors_by_comp.get(comp_id) or []})
        if not groups:
            return None
        return {'groups': groups}

    charts = []
    for st in stat_list or []:
        phi_psi = _plot(st.get('phi_psi_plot'))
        chi = _plot(st.get('chi1_chi2_plot'))
        if phi_psi or chi:
            entry = {'label': st.get('sf_framecode', '')}
            if phi_psi:
                entry['phi_psi'] = phi_psi
            if chi:
                entry['chi1_chi2'] = chi
            charts.append(entry)
    return charts


_STRUCT_CONF_TYPES = {'HELX': 'helix', 'STRN': 'strand', 'TURN': 'turn'}
_PER_RESIDUE_SKIP = {'chain_id', 'seq_id', 'comp_id', 'struct_conf'}


def _constraint_order(key):
    """Canonical display order for a distance-restraint constraint-type key:
    intra-residue, sequential (bb-bb, bb-sc, sc-sc), medium range (bb-bb, bb-sc,
    sc-sc), long range, inter-chain, symmetric. Returns a sort tuple; keys that
    aren't distance constraint types (e.g. chem-shift isotopes) fall to the end,
    keeping their original order under a stable sort. Matches on prefix so the
    hyphen/underscore spelling of the keys doesn't matter."""
    k = key.lower()
    # Backbone/side-chain sub-order within sequential / medium-range groups.
    if 'backbone-backbone' in k:
        sub = 0
    elif 'backbone-sidechain' in k:
        sub = 1
    elif 'sidechain-sidechain' in k:
        sub = 2
    else:
        sub = 3
    if k.startswith('intra'):
        return (0, 0)
    if k.startswith('sequential'):
        return (1, sub)
    if k.startswith('medium'):
        return (2, sub)
    if k.startswith('long'):
        return (3, 0)
    if k.startswith('inter'):
        return (4, 0)
    if k.startswith('symmetric'):
        return (5, 0)
    return (99, 0)


def _sc_type(sc):
    """Secondary-structure class from a struct_conf token (e.g. 'HELX_P:AA1')."""
    if not sc:
        return None
    head = re.split(r'[_:]', str(sc), 1)[0].upper()
    return _STRUCT_CONF_TYPES.get(head)


def _struct_conf_bands(struct_conf):
    """Collapse runs of the same struct_conf value into colored bands
    [{start, end, type, label}] (indices into the residue list). `label` is the
    legitimate struct_conf word (conf_type_id, e.g. 'HELX_P', 'STRN', 'TURN_TY1_P')
    taken from the token as-is (the part before any ':<id>' suffix)."""
    bands = []
    sc_list = struct_conf or []
    i, n = 0, len(sc_list)
    while i < n:
        typ = _sc_type(sc_list[i])
        if typ is None:
            i += 1
            continue
        j = i
        while j + 1 < n and sc_list[j + 1] == sc_list[i]:
            j += 1
        label = sc_list[i]
        bands.append({'start': i, 'end': j, 'type': typ, 'label': label})
        i = j + 1
    return bands


def _domain_bands(domain_id):
    """Collapse runs of the same domain_id into bands [{start, end, type, label}]
    (indices into the residue list). domain_id > 0 → a well-defined core
    (type 'core'); domain_id == -1 → unmodeled residues (type 'unmodeled'); null
    (or any other value) → a gap with no band."""
    bands = []
    dom = domain_id or []
    i, n = 0, len(dom)
    while i < n:
        d = dom[i]
        if not isinstance(d, int) or (d != -1 and d <= 0):
            i += 1
            continue
        j = i
        while j + 1 < n and dom[j + 1] == d:
            j += 1
        if d == -1:
            bands.append({'start': i, 'end': j, 'type': 'unmodeled',
                          'label': 'unmodeled residues'})
        else:
            bands.append({'start': i, 'end': j, 'type': 'core',
                          'label': f'well-defined core {d}'})
        i = j + 1
    return bands


def _per_residue_charts(stat_list):
    """Per-chain stacked per-residue constraint counts from `constraints_per_residue`,
    with secondary-structure bands. All-zero metrics are dropped."""
    charts = []
    for st in stat_list or []:
        for pr in st.get('constraints_per_residue') or []:
            seq = pr.get('seq_id') or []
            comp = pr.get('comp_id') or []
            if not seq:
                continue
            cats = [f"{comp[i] if i < len(comp) else ''} {seq[i]}".strip() for i in range(len(seq))]
            series = [
                {'name': _normalize_label(k), 'data': v}
                for k, v in sorted(pr.items(), key=lambda kv: _constraint_order(kv[0]))
                if k not in _PER_RESIDUE_SKIP and isinstance(v, list)
                and any(isinstance(x, (int, float)) and x for x in v)
            ]
            if not series:
                continue
            charts.append({
                'chain': pr.get('chain_id'),
                'label': st.get('sf_framecode', ''),
                'categories': cats,
                'series': series,
                'bands': _struct_conf_bands(pr.get('struct_conf')),
            })
    return charts


def _discrepancy_charts(stat_list, annotate=_discrepancy_annotations):
    """Histogram charts from `histogram_of_discrepancy` (same shape as `histogram`),
    with outlier markers for redundant-restraint discrepancies. `annotate` selects
    the outlier-label style (distance / dihedral-angle / RDC)."""
    out = []
    for st in stat_list or []:
        hd = st.get('histogram_of_discrepancy')
        if isinstance(hd, dict) and hd.get('range_of_values'):
            out.extend(_histogram_chart([{'sf_framecode': st.get('sf_framecode', ''), 'histogram': hd}],
                                        annotate=annotate))
    return out


def _map_bands(seq, struct_conf):
    """Secondary-structure bands for a contact-map axis: {start, end, type, label}
    with start/end as residue seq_id VALUES (each band spans [start-0.5, end+0.5]
    on the value axis). Empty when the axis has no struct_conf."""
    seq = seq or []
    out = []
    for b in _struct_conf_bands(struct_conf):
        if b['start'] < len(seq) and b['end'] < len(seq):
            out.append({'start': seq[b['start']], 'end': seq[b['end']],
                        'type': b['type'], 'label': b['label']})
    return out


def _contact_map_charts(stat_list):
    """Symmetric contact maps from `constraints_on_contact_map`: per chain, one
    series per constraint type with points [seq_id_1, seq_id_2, total]. `bands`
    are the secondary-structure regions, drawn on both axes (shared sequence)."""
    charts = []
    for st in stat_list or []:
        for cm in st.get('constraints_on_contact_map') or []:
            seq = cm.get('seq_id') or []
            if not seq:
                continue
            comp = cm.get('comp_id') or []
            comp_by_seq = {s: comp[i] for i, s in enumerate(seq) if i < len(comp)}
            series = []
            for k, v in sorted(cm.items(), key=lambda kv: _constraint_order(kv[0])):
                if k in _PER_RESIDUE_SKIP or not isinstance(v, list):
                    continue
                # Each point carries the two residues' names so the tooltip can
                # label them "<comp> <seq>" (see the frontend contactMapOption).
                pts = [{'value': [d['seq_id_1'], d['seq_id_2'], d.get('total', 1)],
                        'c1': comp_by_seq.get(d['seq_id_1'], ''),
                        'c2': comp_by_seq.get(d['seq_id_2'], '')}
                       for d in v if isinstance(d, dict) and 'seq_id_1' in d]
                if pts:
                    series.append({'name': _normalize_label(k), 'points': pts})
            if series:
                charts.append({'chain': cm.get('chain_id'), 'label': st.get('sf_framecode', ''),
                               'min': min(seq), 'max': max(seq), 'series': series,
                               'bands': _map_bands(seq, cm.get('struct_conf'))})
    return charts


def _dim_atom(d):
    """Spectral-dimension atom label, e.g. isotope 13 + type 'C' → ¹³C."""
    iso = d.get('atom_isotope_number')
    atom = d.get('atom_type') or ''
    return _normalize_label(f'{iso}{atom}') if iso and atom else atom


def _spectral_peak_saveframes(sp_list, aligns):
    """Per-saveframe spectral-peak-list preview, in report order: status,
    experiment class, peak counts (assigned/unassigned), spectral-dimension
    table, atom-name mapping. `aligns` is the nmr_poly_seq_vs_spectral_peak
    sequence alignments (for the coverage block)."""
    out = []
    for st in sp_list or []:
        npk = st.get('number_of_spectral_peaks')
        if isinstance(npk, dict):
            n_peaks = sum(v for v in npk.values() if isinstance(v, (int, float)))
            peak_counts = [
                {'label': k.replace('_spectral_peaks', '').replace('_', ' ').strip().capitalize(),
                 'count': v}
                for k, v in npk.items() if isinstance(v, (int, float))
            ]
        else:
            n_peaks, peak_counts = npk, []
        exp = st.get('exp_class') or st.get('exp_type') or ''
        out.append({
            'sf_framecode': st.get('sf_framecode', ''),
            'status': st.get('status'),
            'error_descriptions': st.get('error_descriptions') or [],
            'warning_descriptions': st.get('warning_descriptions') or [],
            'sequence_coverage': _sequence_coverage(st, aligns),
            'exp_class': '' if exp == '.' else exp,
            'n_dims': st.get('number_of_spectral_dimensions'),
            'n_peaks': n_peaks,
            'peak_counts': peak_counts,
            'dims': [
                {'id': d.get('id'), 'atom': _dim_atom(d), 'region': d.get('spectral_region') or '',
                 'sweep_width': d.get('sweep_width'), 'units': d.get('sweep_width_units') or ''}
                for d in st.get('spectral_dim') or []
            ],
            'atom_name_mapping': _atom_name_mapping(st),
        })
    return out


_ANGLE_LABELS = {'phi': 'φ', 'psi': 'ψ', 'chi1': 'χ1', 'chi2': 'χ2', 'chi3': 'χ3', 'chi4': 'χ4'}


def _angle_label(key):
    """Per-residue value-series label: 'phi_angle_constraints' → φ,
    'H-N_bond_vectors' → H-N, etc."""
    base = key.replace('_angle_constraints', '').replace('_bond_vectors', '').replace('_constraints', '')
    return base.replace('_', ' ')
    # return _ANGLE_LABELS.get(base, base.replace('_', ' '))


def _per_residue_value_charts(stat_list, ymin=None, ymax=None):
    """Per-chain per-residue VALUE line charts (e.g. dihedral angles, observed RDC)
    from `constraints_per_residue`. None values are kept (line gaps)."""
    charts = []
    for st in stat_list or []:
        for pr in st.get('constraints_per_residue') or []:
            seq = pr.get('seq_id') or []
            comp = pr.get('comp_id') or []
            if not seq:
                continue
            cats = [f"{comp[i] if i < len(comp) else ''} {seq[i]}".strip() for i in range(len(seq))]
            series = [
                {'name': _angle_label(k), 'data': v}
                for k, v in pr.items()
                if k not in _PER_RESIDUE_SKIP and isinstance(v, list)
                and any(x is not None for x in v)
            ]
            if series:
                charts.append({
                    'chain': pr.get('chain_id'), 'label': st.get('sf_framecode', ''),
                    'categories': cats, 'series': series,
                    'bands': _struct_conf_bands(pr.get('struct_conf')),
                    'ymin': ymin, 'ymax': ymax, 'threshold': None,
                })
    return charts


def _asym_contact_map_charts(stat_list):
    """Asymmetric (inter-chain) contact maps from `constraints_on_asym_contact_map`:
    distinct x (chain 1) and y (chain 2) residue ranges."""
    charts = []
    for st in stat_list or []:
        for cm in st.get('constraints_on_asym_contact_map') or []:
            s1 = cm.get('seq_id_1') or []
            s2 = cm.get('seq_id_2') or []
            if not s1 or not s2:
                continue
            c1 = cm.get('comp_id_1') or []
            c2 = cm.get('comp_id_2') or []
            comp1_by_seq = {s: c1[i] for i, s in enumerate(s1) if i < len(c1)}
            comp2_by_seq = {s: c2[i] for i, s in enumerate(s2) if i < len(c2)}
            series = []
            for k, v in sorted(cm.items(), key=lambda kv: _constraint_order(kv[0])):
                if isinstance(v, list) and v and isinstance(v[0], dict) and 'seq_id_1' in v[0]:
                    # Each point carries the two residues' names so the tooltip can
                    # label them "<chain> <comp> <seq>" (see asymContactMapOption).
                    pts = [{'value': [d['seq_id_1'], d['seq_id_2'], d.get('total', 1)],
                            'c1': comp1_by_seq.get(d['seq_id_1'], ''),
                            'c2': comp2_by_seq.get(d['seq_id_2'], '')}
                           for d in v]
                    if pts:
                        series.append({'name': _normalize_label(k), 'points': pts})
            if series:
                charts.append({
                    'chain1': cm.get('chain_id_1'), 'chain2': cm.get('chain_id_2'),
                    'label': st.get('sf_framecode', ''),
                    'xmin': min(s1), 'xmax': max(s1), 'ymin': min(s2), 'ymax': max(s2),
                    'series': series,
                    # Per-chain secondary-structure bands: chain 1 on x, chain 2 on y.
                    'xbands': _map_bands(s1, cm.get('struct_conf_1')),
                    'ybands': _map_bands(s2, cm.get('struct_conf_2')),
                })
    return charts


def _rci_charts(chem_shift_list, auth=False):
    """RCI/S² (0–1) and NMR-RMSD (Å, with well-defined-region threshold) per-residue
    line charts from `random_coil_index`. auth=True keys residues by
    auth_chain_id/auth_seq_id (the coordinate scheme, output_statistics/download
    page); auth=False by chain_id/seq_id (the NMR-data scheme, summary page). The
    remaining items share the same semantics."""
    chain_key = 'auth_chain_id' if auth else 'chain_id'
    seq_key = 'auth_seq_id' if auth else 'seq_id'
    charts = []
    for st in chem_shift_list or []:
        for rci in st.get('random_coil_index') or []:
            seq = rci.get(seq_key) or []
            comp = rci.get('comp_id') or []
            if not seq:
                continue
            cats = [f"{comp[i] if i < len(comp) else ''} {seq[i]}".strip() for i in range(len(seq))]
            bands = _struct_conf_bands(rci.get('struct_conf'))
            chain = rci.get(chain_key)
            order = [
                {'name': nm, 'data': rci[k]}
                for k, nm in (('rci', 'RCI'), ('s2', 'S²'))
                if isinstance(rci.get(k), list) and any(x is not None for x in rci[k])
            ]
            sf = st.get('sf_framecode', '')
            if order:
                charts.append({'chain': chain, 'label': 'RCI / S²', 'sf': sf, 'categories': cats,
                               'series': order, 'bands': bands, 'ymin': 0, 'ymax': 1, 'threshold': None})
            rmsd = rci.get('nmr_rmsd')
            if isinstance(rmsd, list) and any(x is not None for x in rmsd):
                # The RMSD plot marks the well-defined cores (domain_id) rather
                # than the secondary-structure bands used by the RCI/S² plot.
                thr = rci.get('rmsd_in_well_defined_region')
                rmsd_vals = [x for x in rmsd if isinstance(x, (int, float))]
                ymax = max(max(rmsd_vals), 3.0) if rmsd_vals else 3.0
                charts.append({'chain': chain, 'label': 'NMR RMSD (Å)', 'sf': sf, 'categories': cats,
                               'series': [{'name': 'NMR RMSD', 'data': rmsd}],
                               'bands': _domain_bands(rci.get('domain_id')),
                               'ymin': 0, 'ymax': ymax,
                               'threshold': round(thr, 2) if isinstance(thr, (int, float)) else None})
    return charts


def _dominant(pred):
    """Dominant state from a prediction like 'cis 3.1%, trans 96.9%' or
    'gauche+ 2.0%, trans 40.4%, gauche- 57.6%'; a single state word passes through."""
    if not pred or pred == 'unknown':
        return pred or ''
    parts = []
    for tok in pred.split(','):
        m = re.match(r'(.+?)\s+([\d.]+)%?$', tok.strip())
        if m:
            parts.append((m.group(1), float(m.group(2))))
    return max(parts, key=lambda x: x[1])[0] if parts else pred


def _rotamer_observed(rs):
    """Coordinate rotamer (dominant of gauche+/trans/gauche-) for a chi entry."""
    if not isinstance(rs, dict) or 'unknown' in rs:
        return 'unknown'
    opts = {k: rs[k] for k in ('gauche+', 'trans', 'gauche-') if isinstance(rs.get(k), (int, float))}
    return max(opts, key=opts.get) if opts else 'unknown'


def _shifts_of(item):
    """'CA 65.38, CB 25.92' from an item's *_chem_shift fields (non-null)."""
    return ', '.join(
        f"{k[:-len('_chem_shift')].upper()} {round(v, 2)}"
        for k, v in item.items()
        if k.endswith('_chem_shift') and isinstance(v, (int, float))
    )


_PREDICTION_COMP = {'cys': 'CYS', 'pro': 'PRO', 'his': 'HIS'}


def _prediction_table(items, kind):
    """Rows of a chemical-shift-based prediction table: residue, shifts, predicted
    state (by CS), coordinate state, and a consistency flag (None if unknown)."""
    rows = []
    for s in items or []:
        comp = _PREDICTION_COMP.get(kind) or s.get('comp_id', '')
        if kind == 'cys':
            observed = 'oxidized' if (s.get('in_disulfide_bond') or s.get('in_other_bond')) else 'reduced'
            pred = s.get('redox_state_pred', '')
        elif kind == 'pro':
            observed = 'cis' if s.get('in_cis_peptide_bond') else 'trans'
            pred = s.get('cis_trans_pred', '')
        elif kind == 'his':
            observed = s.get('tautomeric_state', '')
            pred = s.get('tautomeric_state_pred', '')
        else:  # ilv
            chi = 'chi1' if comp == 'VAL' else 'chi2'
            rs = next((r for r in s.get('rotameric_state', []) if r.get('name') == chi), None)
            observed = _rotamer_observed(rs)
            pred = s.get('rotameric_state_pred', '')
        consistent = (None if (not pred or pred == 'unknown' or observed == 'unknown')
                      else _dominant(pred) == observed)
        rows.append({'residue': f"{s.get('chain_id')}:{s.get('seq_id')}:{comp}",
                     'shifts': _shifts_of(s), 'predicted': pred, 'observed': observed,
                     'consistent': consistent})
    return rows


_ALIGN_SIDE = {
    'nmr_poly_seq': 'NMR sequence', 'model_poly_seq': 'Model sequence', 'coordinate': 'Coordinates',
    'chem_shift': 'Chemical shifts', 'dist_restraint': 'Distance restraints',
    'dihed_restraint': 'Dihedral restraints', 'rdc_restraint': 'RDC restraints',
    'spectral_peak': 'Spectral peaks', 'spectral_peak_alt': 'Spectral peaks (alt)',
    'mr_restraint': 'MR restraints', 'mr_topology': 'MR topology',
}


def _align_label(cat):
    if '_vs_' in cat:
        a, b = cat.split('_vs_', 1)
        return f"{_ALIGN_SIDE.get(a, a.replace('_', ' '))} vs {_ALIGN_SIDE.get(b, b.replace('_', ' '))}"
    return cat.replace('_', ' ')


# Only the model-vs-NMR polymer-sequence alignment is shown on the preview as the
# representative one; the other categories (vs coordinates, vs each restraint /
# peak type, etc.) are omitted.
_SEQ_ALIGN_CATEGORY = 'model_poly_seq_vs_nmr_poly_seq'


def _seq_align(info):
    """The representative model-vs-NMR polymer-sequence alignment, one row per
    VALID chain assignment.

    sequence_alignments holds every coordinate-chain × nmr-chain combination,
    which is redundant for multimers with magnetically equivalent chains (e.g.
    coordinates A/B vs nmr 1/2 yields A-1, A-2, B-1, B-2). chain_assignments
    holds only the valid pairings (e.g. A-1, B-2) keyed by (ref_chain_id =
    coordinate chain, test_chain_id = nmr chain); we use those to pick the
    matching alignment rows and take their detailed ref/mid/test sequence blocks.
    """
    chain_pairs = (info.get('chain_assignments') or {}).get(_SEQ_ALIGN_CATEGORY)
    aligns = (info.get('sequence_alignments') or {}).get(_SEQ_ALIGN_CATEGORY)
    if not isinstance(chain_pairs, list) or not isinstance(aligns, list):
        return []
    by_pair = {(a.get('ref_chain_id'), a.get('test_chain_id')): a for a in aligns}
    rows = []
    for ca in chain_pairs:
        ref, test = ca.get('ref_chain_id'), ca.get('test_chain_id')
        a = by_pair.get((ref, test)) or {}
        cov = ca.get('sequence_coverage')
        auth_ref = ca.get('ref_auth_chain_id') or ref or ''
        ref_gauge = a.get('ref_gauge_code') or ''
        test_gauge = a.get('test_gauge_code') or ''
        if ref_gauge == test_gauge:
            test_gauge = ''
        rows.append({
            'chain': (f"Auth_asym_ID: {auth_ref}, Label_asym_ID: {ref} (model) ↔ Entity_assembly_ID: {test} (NMR data)"
                      if ((auth_ref or test) and (auth_ref != ref))
                      else f"Auth_asym_ID: {auth_ref} (model) ↔ Entity_assembly_ID: {test} (NMR_DATA)"
                      if (auth_ref or test) else ''),
            'length': ca.get('length'), 'matched': ca.get('matched'),
            'conflict': ca.get('conflict'), 'unmapped': ca.get('unmapped'),
            'coverage': round(cov * 100, 1) if isinstance(cov, (int, float)) else None,
            'ref_gauge': ref_gauge,
            'ref': a.get('ref_code') or '', 'mid': a.get('mid_code') or '',
            'test': a.get('test_code') or '',
            'test_gauge': test_gauge,
        })
    if not rows:
        return []
    return [{'category': _align_label(_SEQ_ALIGN_CATEGORY), 'rows': rows}]


def _bond_atom(b, n):
    """Format a bond endpoint 'chain:seq:comp:atom' (seq omitted when null)."""
    chain, seq = b.get(f'chain_id_{n}'), b.get(f'seq_id_{n}')
    comp, atom = b.get(f'comp_id_{n}'), b.get(f'atom_id_{n}')
    fields = [chain, comp, atom] if seq is None else [chain, seq, comp, atom]
    return ':'.join(str(f) for f in fields if f not in (None, ''))


def _bond_rows(items):
    """Rows for a disulfide / other bond table: the two bonded atoms + distance."""
    return [
        {'type': b.get('bond_type'), 'atom1': _bond_atom(b, 1), 'atom2': _bond_atom(b, 2),
         'distance': b.get('distance_value')}
        for b in items or []
    ]


def _nstd_res_rows(items):
    """Non-standard residue rows (one per residue across chains): chain, seq,
    comp, the CCD chemical-component name (null when not matched), and the
    experimental-data subtypes that reference the residue."""
    rows = []
    for c in items or []:
        cid = c.get('chain_id')
        seq = c.get('seq_id') or []
        comp = c.get('comp_id') or []
        names = c.get('chem_comp_name') or []
        exptl = c.get('exptl_data') or []
        for j in range(len(seq)):
            name = names[j] if j < len(names) else None
            ed = exptl[j] if j < len(exptl) else {}
            types = ', '.join(
                _NMR_SUBTYPE_NAMES.get(k, k.replace('_', ' ').title())
                for k, v in (ed or {}).items() if v
            )
            rows.append({
                'chain': cid,
                'seq_id': seq[j],
                'comp_id': comp[j] if j < len(comp) else '',
                'name': name,
                'matched': name is not None,
                'exptl': types,
            })
    return rows


def _assembly_properties(report):
    """Global properties of the molecular assembly (NMR experiment environment):
    diamagnetism and presence of disulfide / other bonds and cyclic polymers,
    plus detail tables for the disulfide bonds, other bonds, and non-standard
    residues. The booleans are entry-level (information.*); the detail lists live
    on an input source, preferring the model (coordinate) source."""
    info = report.get('information', {})
    srcs = info.get('input_sources') or []

    def pick(key):
        model = next((s for s in srcs if s.get('content_type') == 'model'), None)
        if model and isinstance(model.get(key), list) and model[key]:
            return model[key]
        for s in srcs:
            v = s.get(key)
            if isinstance(v, list) and v:
                return v
        return []

    return {
        'diamagnetic': info.get('diamagnetic'),
        'disulfide_bond': info.get('disulfide_bond'),
        'other_bond': info.get('other_bond'),
        'cyclic_polymer': info.get('cyclic_polymer'),
        'disulfide_bonds': _bond_rows(pick('disulfide_bond')),
        'other_bonds': _bond_rows(pick('other_bond')),
        'non_standard_residues': _nstd_res_rows(pick('non_standard_residue')),
    }


# Completeness sub-categories surfaced per chain, in display order.
_COMPLETENESS_CATEGORIES = [
    ('completeness_of_all_assignments', 'All atoms'),
    ('completeness_of_backbone_assignments', 'Backbone atoms'),
    ('completeness_of_sidechain_assignments', 'Side chain atoms'),
    ('completeness_of_methyl_assignments', 'Methyl group atoms'),
    ('completeness_of_aromatic_assignments', 'Aromatic group atoms'),
]


def _completeness_of(st):
    """Per-chain assignment completeness + sequence coverage for one chem_shift
    saveframe → [{chain, coverage_pct, categories:[{label, groups:[{group,
    target, assigned, pct}]}]}]. Each category (all / backbone / side chain /
    methyl / aromatic) is included only when present."""
    cov = {c.get('chain_id'): c.get('sequence_coverage')
           for c in (st.get('sequence_coverage') or [])}
    out = []
    for comp in st.get('completeness') or []:
        categories = []
        for key, label in _COMPLETENESS_CATEGORIES:
            groups = [
                {'group': _normalize_label(g.get('atom_group', '')),
                 'target': g.get('number_of_target_shifts'),
                 'assigned': g.get('number_of_assigned_shifts'),
                 'pct': round((g.get('completeness') or 0) * 100, 1)}
                for g in comp.get(key) or []
            ]
            if groups:
                categories.append({'label': label, 'groups': groups})
        # Residues / atoms excluded from the completeness calculation. Comp_IDs
        # with a null ('.') comp_id are dropped (no real residue to report).
        excluded_comp = [
            {'seq_id': e.get('seq_id'), 'comp_id': e.get('comp_id')}
            for e in comp.get('excluded_comp_id_in_statistics') or []
            if e.get('comp_id') not in (None, '.', '')
        ]
        excluded_atom = [
            {'seq_id': e.get('seq_id'), 'comp_id': e.get('comp_id'),
             'atom_id': e.get('atom_id'), 'value': e.get('value')}
            for e in comp.get('excluded_atom_id_in_statistics') or []
        ]
        chain = comp.get('chain_id')
        out.append({
            'chain': chain,
            'coverage_pct': round((cov.get(chain) or 0) * 100, 1) if chain in cov else None,
            'categories': categories,
            'excluded_comp_id': excluded_comp,
            'excluded_atom_id': excluded_atom,
        })
    return out


def _assignments_of(st):
    """Total assignment counts per isotope for one saveframe → [{label, count}]."""
    noa = st.get('number_of_assignments') or {}
    return [{'label': _normalize_label(k), 'count': v}
            for k, v in noa.items() if isinstance(v, (int, float))]


def _atom_mapping_normal(atom_name, atom_ids):
    """Whether an original → IUPAC atom-name mapping looks expected (mirrors the
    reference nmr_dp_report_atom_name_mapping_history). A mapping is normal when
    an original/IUPAC name is a prefix of the other, allowing for pseudo-atom
    conventions: wildcards (#/%/*) stripped, Q/M → H, a leading digit moved to
    the end, and a trailing digit trimmed. Unusual mappings are flagged."""
    if not atom_name or not atom_ids:
        return True

    def matches(name):
        return bool(name) and (
            any(a.startswith(name) for a in atom_ids)
            or any(name.startswith(a) for a in atom_ids)
        )

    if matches(atom_name):
        return True
    name = atom_name.replace('#', '').replace('%', '').replace('*', '')
    if name and name[0] in ('Q', 'M'):
        name = 'H' + name[1:]
    if name and name[0] in ('1', '2', '3'):
        name = name[1:] + name[0]
    if matches(name):
        return True
    if (len(name) > 2 and name[-1].isdigit()
            and (not name[-2].isdigit() or atom_ids[0].startswith(name[:-1]))):
        if matches(name[:-1]):
            return True
    return False


def _atom_name_mapping(st):
    """Per-residue (Comp_ID) atom-name mapping for one saveframe:
    [{comp_id, history:[{name, atoms, unusual}]}] where each history entry maps
    an author-defined atom name to its IUPAC Atom_ID(s) in the CCD; unusual flags
    an unexpected mapping for the user."""
    out = []
    for m in st.get('atom_name_mapping') or []:
        history = []
        for h in m.get('history') or []:
            name = h.get('atom_name', '')
            atom_ids = [str(a) for a in (h.get('atom_id') or [])]
            history.append({'name': name, 'atoms': ', '.join(atom_ids),
                            'unusual': not _atom_mapping_normal(name, atom_ids)})
        if history:
            out.append({'comp_id': m.get('comp_id', ''), 'history': history})
    return out


def _sequence_coverage(st, aligns):
    """Per-chain sequence coverage of the experimental data for one saveframe →
    [{chain, length, coverage_pct, ref_gauge, ref, mid, test}]. The aligned-
    sequence block (ref/mid/test) is joined from `aligns` (the
    nmr_poly_seq_vs_<subtype> sequence alignments) by this saveframe's list_id
    and the chain id."""
    list_id = st.get('list_id')
    by_chain = {a.get('chain_id'): a for a in (aligns or []) if a.get('list_id') == list_id}
    out = []
    for c in st.get('sequence_coverage') or []:
        cov = c.get('sequence_coverage')
        chain_id = c.get('chain_id')
        a = by_chain.get(chain_id) or {}
        out.append({
            'chain': chain_id,
            'length': c.get('length'),
            'coverage_pct': round(cov * 100, 1) if isinstance(cov, (int, float)) else None,
            'ref_gauge': a.get('ref_gauge_code') or '',
            'ref': a.get('ref_code') or '',
            'mid': a.get('mid_code') or '',
            'test': a.get('test_code') or '',
        })
    return out


def _chem_shift_saveframes(chem_shift_list, aligns):
    """Per-saveframe assigned-chemical-shift preview, in report order. Reuses the
    per-content helpers on a single saveframe so the summary page can group all
    chem-shift content (status, coverage/completeness, CS-prediction tables,
    histogram, RCI charts, atom-name mapping) under its sf_framecode. `aligns` is
    the nmr_poly_seq_vs_chem_shift sequence alignments (for the coverage block)."""
    out = []
    for st in chem_shift_list or []:
        out.append({
            'sf_framecode': st.get('sf_framecode', ''),
            'status': st.get('status'),
            'error_descriptions': st.get('error_descriptions') or [],
            'warning_descriptions': st.get('warning_descriptions') or [],
            'sequence_coverage': _sequence_coverage(st, aligns),
            'assignments': _assignments_of(st),
            'completeness': _completeness_of(st),
            'predictions': {
                'cys_redox': _prediction_table(st.get('cys_redox_state'), 'cys'),
                'pro_cis_trans': _prediction_table(st.get('pro_cis_trans'), 'pro'),
                'his_tautomer': _prediction_table(st.get('his_tautomeric_state'), 'his'),
                'ilv_rotamer': _prediction_table(st.get('ilv_rotameric_state'), 'ilv'),
            },
            'histogram': _histogram_chart([st], True),
            'rci': _rci_charts([st]),
            'atom_name_mapping': _atom_name_mapping(st),
        })
    return out


# Tailwind class for the red highlight used in the hierarchical lists below.
_DIST_RED = 'text-red-600 dark:text-red-400'


def _count_most_common_value(pairs):
    """Render one classification's [[value, count], …] frequency list, e.g.
    '1.0 (179)'. Port of utils_nmr.nmr_dp_report_count_most_common_value."""
    return ', '.join(f'{p[0]} ({p[1]})' for p in pairs)


def _count_most_common_values(item):
    """Aggregate the [[value, count], …] lists across several classifications and
    render 'value (total), …'. Port of nmr_dp_report_count_most_common_values."""
    totals = {}
    for pairs in item.values():
        for value, count in pairs:
            totals[str(value)] = totals.get(str(value), 0) + count
    return ', '.join(f'{k} ({v})' for k, v in totals.items())


def _sort_of_most_common_values(item):
    """Number of distinct values across all classifications (port of
    nmr_dp_report_sort_of_most_common_values); decides whether the weight /
    potential-type breakdown is worth expanding."""
    return len({str(value) for pairs in item.values() for value, _c in pairs})


def _dist_leaf(item, key, mode):
    """Render one classification's value: an integer count ('count' mode) or a
    most-common-value summary ('mc' mode, for weight / potential type)."""
    return str(item[key]) if mode == 'count' else _count_most_common_value(item[key])


def _dist_agg(item, keys, mode):
    """Aggregate several classifications: sum of counts ('count') or merged
    most-common values ('mc')."""
    if mode == 'count':
        return str(sum(item[k] for k in keys))
    return _count_most_common_values({k: item[k] for k in keys})


def _dist_bond_label(label):
    """Highlight '(too close!)' / '(too far!)' in a bond-restraint subtype."""
    for flag in ('(too close!)', '(too far!)'):
        label = label.replace(flag, f'<span class="{_DIST_RED}">{flag}</span>')
    return label


def _dist_bond_section(item, name, title, mode):
    """One bond-type section (hydrogen / disulfide / diselenide / other bonds) of
    the distance-restraint tree: a total line, the direct subtypes, then the
    long-range / inter-chain nested subtypes. Port of the repeated bond blocks in
    utils_nmr.nmr_dp_report_distance_constraints."""
    if not any(name in k for k in item):
        return ''
    html = f'<li>Total {title} restraints: ' + _dist_agg(item, [k for k in item if name in k], mode)
    direct = [k for k in item if k.startswith(name + '_')]
    if direct:
        html += '<ul>' + ''.join(
            f'<li>{_dist_bond_label(k[len(name) + 1:])}: {_dist_leaf(item, k, mode)}</li>' for k in direct
        ) + '</ul>'
    nested = ''
    for prefix, label in ((f'long_range_{name}_', 'Long range'),
                          (f'inter-chain_{name}_', 'Inter-chain')):
        keys = [k for k in item if k.startswith(prefix)]
        if keys:
            nested += f'<li>{label}: ' + _dist_agg(item, keys, mode) + '<ul>' + ''.join(
                f'<li>{_dist_bond_label(k[len(prefix):])}: {_dist_leaf(item, k, mode)}</li>' for k in keys
            ) + '</ul></li>'
    if nested:
        html += '<ul>' + nested + '</ul>'
    return html + '</li>'


def _dist_constraint_tree(item, mode, top, expand):
    """Build the hierarchical <li>…</li> for one distance-restraint field. `item`
    is a {classification: value} dict sharing the NmrDpUtility distance-restraint
    key structure; `mode` is 'count' (leaf values are integer counts) or 'mc'
    (leaf values are [[value, count], …] frequency lists). Port of
    utils_nmr.nmr_dp_report_distance_constraints / …_most_common_value_of_… ."""
    html = '<li>' + top
    if not expand:
        return html + '</li>'
    inner = ''
    if 'intra-residue_constraints' in item:
        inner += ('<li>Intra-residue restraints (<em>| i - j | = 0</em>) : '
                  + _dist_leaf(item, 'intra-residue_constraints', mode) + '</li>')
    for prefix, label, rng in (
        ('sequential', 'Sequential restraints', '(<em>| i - j | = 1</em>)'),
        ('medium_range', 'Medium range restraints', '(<em>1 &lt; | i - j | &lt; 5</em>)'),
    ):
        keys = [k for k in item if k.startswith(prefix)]
        if not keys:
            continue
        inner += f'<li>{label} {rng} : ' + _dist_agg(item, keys, mode)
        subs = ''.join(
            f'<li>{lbl}: {_dist_leaf(item, k, mode)}</li>'
            for k, lbl in ((f'{prefix}_constraints_backbone-backbone', 'Backbone-backbone'),
                           (f'{prefix}_constraints_backbone-sidechain', 'Backbone-side chain'),
                           (f'{prefix}_constraints_sidechain-sidechain', 'Side chain-side chain'))
            if k in item
        )
        if subs:
            inner += '<ul>' + subs + '</ul>'
        inner += '</li>'
    if 'long_range_constraints' in item:
        inner += ('<li>Long range restraints (<em>| i - j | ≥ 5)</em> : '
                  + _dist_leaf(item, 'long_range_constraints', mode) + '</li>')
    if 'inter-chain_constraints' in item:
        inner += '<li>Inter-chain restraints: ' + _dist_leaf(item, 'inter-chain_constraints', mode) + '</li>'
    for name, title in (('hydrogen_bonds', 'hydrogen bond'), ('disulfide_bonds', 'disulfide bond'),
                        ('diselenide_bonds', 'diselenide bond'), ('other_bonds', 'other bond')):
        inner += _dist_bond_section(item, name, title, mode)
    if 'symmetric_constraints' in item:
        inner += '<li>Symmetric restraints: ' + _dist_leaf(item, 'symmetric_constraints', mode) + '</li>'
    if inner:
        html += '<ul>' + inner + '</ul>'
    return html + '</li>'


# Distance-restraint fields rendered as hierarchical lists, in display order:
# (key, report_key, mode, top-line param, section title). The number_of_* fields
# are counts; weight / potential type carry [[value, count], …] frequency lists.
_DIST_CONSTRAINT_FIELDS = (
    ('number', 'number_of_constraints', 'count', None, 'Number of unique restraints'),
    ('combined', 'number_of_combined_constraints', 'count', 'combined', 'Number of combined restraints'),
    ('redundant', 'number_of_redundant_constraints', 'count', 'redundant', 'Number of redundant restraints'),
    ('inconsistent', 'number_of_inconsistent_constraints', 'count', 'inconsistent',
     'Number of inconsistent restraints'),
    ('weight', 'weight_of_constraints', 'mc', 'Weight', 'Weight of restraints'),
    ('potential', 'potential_type_of_constraints', 'mc', 'Potential type', 'Potential type of restraints'),
)


def _dist_constraint_lists(st):
    """Hierarchical lists for a distance-restraint saveframe: number / combined /
    redundant / inconsistent counts and weight / potential type of constraints
    (each optional, included only when present in the report). Each entry is
    {key, title, html} where html is a ready-to-render <ul> tree (bound via
    [innerHTML] on the summary page)."""
    out = []
    for key, field, mode, param, title in _DIST_CONSTRAINT_FIELDS:
        item = st.get(field)
        if not isinstance(item, dict) or not item:
            continue
        if mode == 'count':
            top = ('Total number of unique ' + (param + ' ' if param else '') + 'restraints: '
                   + _dist_agg(item, list(item.keys()), mode))
            expand = True
        else:
            top = f'{param} of restraints: ' + _dist_agg(item, list(item.keys()), mode)
            expand = _sort_of_most_common_values(item) > 1
        html = '<ul>' + _dist_constraint_tree(item, mode, top, expand) + '</ul>'
        out.append({'key': key, 'title': title, 'html': html})
    return out


def _flat_constraint_tree(item, mode, top, expand):
    """Build a one-level <li>top<ul>per-classification</ul></li> for a flat
    {classification: value} restraint dict (dihedral-angle / RDC style, where
    classifications are not further nested). `mode` selects the leaf rendering
    ('count' or 'mc'); classifications are prettified with _normalize_label."""
    html = '<li>' + top
    if expand:
        subs = ''.join(f'<li>{_normalize_label(k)}: {_dist_leaf(item, k, mode)}</li>' for k in item)
        if subs:
            html += '<ul>' + subs + '</ul>'
    return html + '</li>'


# Flat (single-level) restraint fields rendered as hierarchical lists, in
# display order: (key, report_key, mode, top-line param, section title). Shared
# by dihedral-angle and RDC restraints. number_of_* and constraints_per_polymer_type
# are flat count dicts; weight / potential type carry [[value, count], …] lists.
_FLAT_CONSTRAINT_FIELDS = (
    ('number', 'number_of_constraints', 'count', None, 'Number of unique restraints'),
    ('combined', 'number_of_combined_constraints', 'count', 'combined', 'Number of combined restraints'),
    ('redundant', 'number_of_redundant_constraints', 'count', 'redundant', 'Number of redundant restraints'),
    ('inconsistent', 'number_of_inconsistent_constraints', 'count', 'inconsistent',
     'Number of inconsistent restraints'),
    ('polymer_type', 'constraints_per_polymer_type', 'count', None, 'Constraints per polymer type'),
    ('weight', 'weight_of_constraints', 'mc', 'Weight', 'Weight of restraints'),
    ('potential', 'potential_type_of_constraints', 'mc', 'Potential type', 'Potential type of restraints'),
)


def _flat_constraint_lists(st):
    """Hierarchical lists for a flat-structured restraint saveframe (dihedral-angle
    or RDC): number / combined / redundant / inconsistent counts, per-polymer-type
    counts, and weight / potential type of constraints (each optional). Each entry
    is {key, title, html}; html is a ready-to-render <ul> tree (bound via
    [innerHTML] on the summary page)."""
    out = []
    for key, field, mode, param, title in _FLAT_CONSTRAINT_FIELDS:
        item = st.get(field)
        if not isinstance(item, dict) or not item:
            continue
        if mode == 'count':
            top = ('Total number of unique ' + (param + ' ' if param else '') + 'restraints: '
                   + _dist_agg(item, list(item.keys()), mode))
            expand = True
        else:
            top = f'{param} of restraints: ' + _dist_agg(item, list(item.keys()), mode)
            expand = _sort_of_most_common_values(item) > 1
        html = '<ul>' + _flat_constraint_tree(item, mode, top, expand) + '</ul>'
        out.append({'key': key, 'title': title, 'html': html})
    return out


def _dist_restraint_saveframes(dist_list, aligns):
    """Per-saveframe distance-restraint preview, in report order. Reuses the
    per-content helpers on a single saveframe so the summary page can group all
    distance-restraint content (status, constraint counts/range, target-value
    histogram + discrepancy, per-residue counts, symmetric/asymmetric contact
    maps, atom-name mapping) under its sf_framecode. `aligns` is the
    nmr_poly_seq_vs_dist_restraint sequence alignments (for the coverage block)."""
    out = []
    for st in dist_list or []:
        rng = st.get('range') or {}
        range_text = (f"{rng.get('min_value')}–{rng.get('max_value')} Å"
                      if rng.get('min_value') is not None else '')
        out.append({
            'sf_framecode': st.get('sf_framecode', ''),
            'status': st.get('status'),
            'error_descriptions': st.get('error_descriptions') or [],
            'warning_descriptions': st.get('warning_descriptions') or [],
            'exp_type': st.get('exp_type') or '',
            'sequence_coverage': _sequence_coverage(st, aligns),
            'constraint_lists': _dist_constraint_lists(st),
            'range': range_text,
            'histogram': _histogram_chart([st]),
            'discrepancy': _discrepancy_charts([st]),
            'per_residue': _per_residue_charts([st]),
            'contact_maps': _contact_map_charts([st]),
            'asym_contact_maps': _asym_contact_map_charts([st]),
            'atom_name_mapping': _atom_name_mapping(st),
        })
    return out


def _dihed_restraint_saveframes(dihed_list, aligns):
    """Per-saveframe dihedral-angle-restraint preview, in report order: status,
    constraint counts, φ/ψ and χ1/χ2 scatter, per-residue angle values,
    atom-name mapping. Reuses the per-content helpers on a single saveframe.
    `aligns` is the nmr_poly_seq_vs_dihed_restraint sequence alignments."""
    out = []
    for st in dihed_list or []:
        out.append({
            'sf_framecode': st.get('sf_framecode', ''),
            'status': st.get('status'),
            'error_descriptions': st.get('error_descriptions') or [],
            'warning_descriptions': st.get('warning_descriptions') or [],
            'exp_type': st.get('exp_type') or '',
            'sequence_coverage': _sequence_coverage(st, aligns),
            'constraint_lists': _flat_constraint_lists(st),
            'histogram': _histogram_chart([st]),
            'discrepancy': _discrepancy_charts([st], annotate=_dihed_discrepancy_annotations),
            'dihedral': _dihedral_charts([st]),
            'per_residue': _per_residue_value_charts([st], -180, 180),
            'atom_name_mapping': _atom_name_mapping(st),
        })
    return out


def _rdc_restraint_saveframes(rdc_list, aligns):
    """Per-saveframe RDC-restraint preview, in report order: status, constraint
    counts/range, observed-value histogram, per-residue observed RDC, atom-name
    mapping. Reuses the per-content helpers on a single saveframe. `aligns` is
    the nmr_poly_seq_vs_rdc_restraint sequence alignments."""
    out = []
    for st in rdc_list or []:
        rng = st.get('range') or {}
        range_text = (f"{rng.get('min_value')}–{rng.get('max_value')} Hz"
                      if rng.get('min_value') is not None else '')
        out.append({
            'sf_framecode': st.get('sf_framecode', ''),
            'status': st.get('status'),
            'error_descriptions': st.get('error_descriptions') or [],
            'warning_descriptions': st.get('warning_descriptions') or [],
            'exp_type': st.get('exp_type') or '',
            'sequence_coverage': _sequence_coverage(st, aligns),
            'constraint_lists': _flat_constraint_lists(st),
            'range': range_text,
            'histogram': _histogram_chart([st]),
            'discrepancy': _discrepancy_charts([st], annotate=_rdc_discrepancy_annotations),
            'per_residue': _per_residue_value_charts([st]),
            'atom_name_mapping': _atom_name_mapping(st),
        })
    return out


# Experiment types for which low sequence coverage is expected (not flagged).
_IGNORABLE_EXP_TYPE_FOR_COVERAGE = (
    'disulfide bound', 'disulfide_bond', 'paramagnetic relaxation', 'pre',
    'symmetry', 'J-couplings', 'jcoupling',
)
# Subtypes that, when present, are expected to carry sequence coverage.
_PRIMARY_SUBTYPES = ('chem_shift', 'dist_restraint', 'dihed_restraint', 'rdc_restraint')


def _coverage_summary(item):
    """One-line per-chain coverage, e.g. '57.4% (chain 1, length 94), …'."""
    parts = []
    for sc in item.get('sequence_coverage') or []:
        cov = sc.get('sequence_coverage')
        pct = f'{cov * 100:.1f}%' if isinstance(cov, (int, float)) else '?'
        parts.append(f'{pct} (chain {sc.get("chain_id")}, length {sc.get("length")})')
    return ', '.join(parts)


def _is_low_seq_coverage(item):
    """True if any chain (length > 1) has < 30% coverage, unless the experiment
    type is one for which low coverage is expected."""
    exp_type = item.get('exp_type')
    if exp_type in _IGNORABLE_EXP_TYPE_FOR_COVERAGE:
        return False
    for sc in item.get('sequence_coverage') or []:
        cov = sc.get('sequence_coverage')
        if isinstance(cov, (int, float)) and cov < 0.3 and (sc.get('length') or 0) > 1:
            return True
    return False


def _nmr_inventory(report):
    """Per-file inventory of what was parsed/interpreted during conversion: one
    table per non-model input source, one row per saveframe across every
    experimental-data subtype (content subtype, saveframe, status, # of rows
    [/sets], experiment type, sequence coverage). The single global summary of
    successful parsing/interpretation for the NMR data preview."""
    info = report.get('information', {})
    files = []
    for src in info.get('input_sources') or []:
        if not isinstance(src, dict) or src.get('content_type') == 'model':
            continue
        stats = src.get('stats_of_exptl_data')
        if not isinstance(stats, dict):
            continue
        has_sets = any(
            isinstance(i, dict) and i.get('number_of_constraint_sets') is not None
            and i.get('number_of_constraint_sets') != i.get('number_of_rows')
            for lst in stats.values() if isinstance(lst, list) for i in lst
        )
        rows = []
        for subtype, lst in stats.items():
            if not isinstance(lst, list) or not lst:
                continue
            label = _NMR_SUBTYPE_NAMES.get(subtype)
            for item in lst:
                if not isinstance(item, dict):
                    continue
                n_rows = item.get('number_of_rows')
                n_sets = item.get('number_of_constraint_sets')
                rows_text = str(n_rows)
                if n_sets is not None and n_sets != n_rows:
                    rows_text = f'{n_rows} ({n_sets})'
                exp = item.get('exp_type')
                status = item.get('status')
                rows.append({
                    'list_id': item.get('list_id'),
                    'subtype': label or subtype.replace('_', ' ').title(),
                    'subtype_unknown': label is None,
                    'sf_framecode': item.get('sf_framecode', ''),
                    'status': status,
                    'has_issue': bool(item.get('error_descriptions')
                                      or item.get('warning_descriptions')),
                    'is_error': status == 'Error',
                    'n_rows': rows_text,
                    'rows_zero': n_rows == 0,
                    'exp_type': '' if exp in (None, '.', '') else exp,
                    'exp_unknown': exp == 'unknown',
                    'coverage': _coverage_summary(item),
                    'coverage_low': _is_low_seq_coverage(item),
                    'coverage_missing': not item.get('sequence_coverage'),
                    'coverage_required': subtype in _PRIMARY_SUBTYPES,
                })
        files.append({
            'content_name': _NMR_CONTENT_NAMES.get(src.get('content_type'), src.get('content_type')),
            'file_name': src.get('original_file_name') or src.get('file_name') or '',
            'has_sets': has_sets,
            'rows': rows,
        })
    return files


def _nmr_preview_data(report):
    """Extract Phase-1 chart/table data from an NmrDpUtility report. Aggregates
    per-subtype stats (stats_of_exptl_data) across input sources."""
    info = report.get('information', {})
    sources = []
    chem_shift, dist_restraint, dihed_restraint = [], [], []
    rdc_restraint, spectral_peak = [], []

    for src in info.get('input_sources') or []:
        if not isinstance(src, dict):
            continue
        ctype = src.get('content_type', '')
        subtypes = [
            _NMR_SUBTYPE_NAMES.get(k, k.replace('_', ' ').title())
            for k, v in (src.get('content_subtype') or {}).items() if v
        ]
        sources.append({
            'name': src.get('original_file_name') or src.get('file_name') or '',
            'content_name': _NMR_CONTENT_NAMES.get(ctype, ctype),
            'subtypes': subtypes,
        })
        stats = src.get('stats_of_exptl_data')
        if not isinstance(stats, dict):
            continue
        chem_shift.extend(stats.get('chem_shift') or [])
        dist_restraint.extend(stats.get('dist_restraint') or [])
        dihed_restraint.extend(stats.get('dihed_restraint') or [])
        rdc_restraint.extend(stats.get('rdc_restraint') or [])
        spectral_peak.extend(stats.get('spectral_peak') or [])

    # Per-subtype sequence alignments (nmr_poly_seq_vs_<subtype>) supply the
    # per-chain aligned-sequence block shown in each saveframe's coverage table.
    sa = info.get('sequence_alignments') or {}
    return {
        'sources': sources,
        # Single global inventory of what was parsed/interpreted, per file.
        'inventory': _nmr_inventory(report),
        # Chemical shifts and all restraint / spectral-peak content are grouped
        # by saveframe (sf_framecode); sequence alignments remain a single table.
        'chem_shift_saveframes': _chem_shift_saveframes(
            chem_shift, sa.get('nmr_poly_seq_vs_chem_shift') or []),
        'dist_restraint_saveframes': _dist_restraint_saveframes(
            dist_restraint, sa.get('nmr_poly_seq_vs_dist_restraint') or []),
        'dihed_restraint_saveframes': _dihed_restraint_saveframes(
            dihed_restraint, sa.get('nmr_poly_seq_vs_dihed_restraint') or []),
        'rdc_restraint_saveframes': _rdc_restraint_saveframes(
            rdc_restraint, sa.get('nmr_poly_seq_vs_rdc_restraint') or []),
        'spectral_peak_saveframes': _spectral_peak_saveframes(
            spectral_peak, sa.get('nmr_poly_seq_vs_spectral_peak') or []),
        'assembly': _assembly_properties(report),
        'alignments': _seq_align(info),
        # Coordinate ensemble well-defined regions (same source as the download
        # page); null when there is no pdbx input source with the analysis.
        'ensemble_composition': _ensemble_composition(report),
        # Per-saveframe bookkeeping rows (from output_statistics), keyed by
        # sf_framecode, for the download-page bookkeeping table transplanted here.
        'bookkeeping': _bookkeeping_by_sf(report),
    }


@app.route('/api/nmr_preview', methods=['GET'])
async def get_nmr_preview():
    """Graphical-overview data for the converted NMR data, parsed from the same
    report as /api/nmr_validation (the convert_nmr_data workflow log_path JSON).
    Token-scoped, read-only; 200 always (400 missing token); available=false when
    there is no report yet."""
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
            return {'available': False}
        wf = (
            await db.execute(
                select(Workflow).where(
                    Workflow.conversion_id == conversion_id,
                    Workflow.run_number == run_number,
                    Workflow.task == WfTaskCode.convert_nmr_data.value,
                )
            )
        ).scalar_one_or_none()

    if wf is None or not wf.log_path or not Path(wf.log_path).is_file():
        return {'available': False}
    try:
        report = json.loads(Path(wf.log_path).read_text())
    except Exception:  # noqa: BLE001
        return {'available': False}

    return {'available': True, **_nmr_preview_data(report)}


# Sub-sections of output_statistics excluded from the download-page summary: the
# large per-restraint/per-peak validation tables. The summary objects
# (chem_shift_summary, restraint_summary) ARE kept — the page shows them. The
# chem_shift array is kept too, but pruned to per-saveframe bookkeeping counts
# (_CHEM_SHIFT_STATS_KEYS) rather than its heavy validation sub-tables.
_OUTPUT_STATS_EXCLUDE = {
    'dist_restraint', 'dihed_restraint', 'rdc_restraint', 'spectral_peak',
}

# Columns kept from restraint_summary.dist_violation_summary (per-category
# distance-violation counts/percentages).
_DIST_VIOLATION_SUMMARY_KEYS = (
    'restraint_type', 'restraint_count', 'restraint_percent',
    'viol_count', 'viol_inline_percent', 'viol_absol_percent',
    'consist_viol_count', 'consist_viol_inline_percent', 'consist_viol_absol_percent',
)

# Columns kept for each most-violated restraint (restraint_summary.
# most_violated_{dist,dihed}_restraints); the per-model violated_model_id list
# and min/max are dropped.
_MOST_VIOLATED_KEYS = (
    'restraint_key', 'distance_type', 'dihedral_angle_name',
    'atom_key_1', 'atom_key_2', 'atom_key_3', 'atom_key_4',
    'total_violated_models', 'mean_violation', 'std_violation', 'median_violation',
)

# Columns kept for each per-model violation entry (restraint_summary.
# all_{dist,dihed}_violations).
_ALL_VIOLATION_KEYS = (
    'restraint_key', 'distance_type', 'dihedral_angle_name',
    'atom_key_1', 'atom_key_2', 'atom_key_3', 'atom_key_4',
    'model_id', 'violation',
)

# Per-saveframe chemical-shift bookkeeping fields kept for the download-page
# 'Assigned chemical shift summary' — the large validation sub-tables (RCI charts,
# atom-name mapping, per-shift completeness/outlier lists) are dropped.
_CHEM_SHIFT_STATS_KEYS = (
    'original_file_name', 'list_id', 'sf_framecode',
    'number_of_parsed', 'number_of_mapped_to_model', 'number_of_unmapped_to_model',
    'number_of_unparsed_with_error', 'number_of_parsed_with_warning',
    'number_of_outliers',
)

# Common bookkeeping columns shared by the restraint / spectral-peak subtypes
# (output_stats_common_bookkeeping); number_of_outliers is chem-shift-specific and
# omitted here.
_BOOKKEEPING_KEYS = (
    'original_file_name', 'list_id', 'sf_framecode',
    'number_of_parsed', 'number_of_mapped_to_model', 'number_of_unmapped_to_model',
    'number_of_unparsed_with_error', 'number_of_parsed_with_warning',
)


def _bookkeeping_saveframes(stats, key):
    """Per-saveframe bookkeeping (+ atom-name mapping history) for a restraint /
    spectral-peak subtype (output_statistics.<key>), sharing the common
    output_stats_common_bookkeeping semantics."""
    out = []
    for item in stats.get(key) or []:
        if not isinstance(item, dict):
            continue
        row = {k: item[k] for k in _BOOKKEEPING_KEYS if k in item}
        anm = _atom_name_mapping(item)
        if anm:
            row['atom_name_mapping'] = anm
        out.append(row)
    return out


def _bookkeeping_by_sf(report):
    """Per-saveframe bookkeeping Property/Value rows keyed by sf_framecode, built
    from output_statistics (chem_shift + restraint / spectral-peak subtypes). Lets
    the summary page show the same bookkeeping table the download page renders,
    matched to its nmr_preview saveframes by sf_framecode."""
    stats = report.get('information', {}).get('output_statistics') or {}
    if not isinstance(stats, dict):
        return {}

    def rows_for(item, noun, outliers=False):
        pairs = [
            (f'Number of parsed {noun}', item.get('number_of_parsed')),
            (f'Number of {noun} mapped to model', item.get('number_of_mapped_to_model')),
            (f'Number of {noun} unmapped to model', item.get('number_of_unmapped_to_model')),
            (f'Number of unparsed {noun} with error', item.get('number_of_unparsed_with_error')),
            (f'Number of parsed {noun} with warning', item.get('number_of_parsed_with_warning')),
        ]
        if outliers:
            pairs.append(('Number of chemical shift outliers', item.get('number_of_outliers')))
        return [{'label': lbl, 'value': v} for lbl, v in pairs if v is not None]

    out = {}
    for item in stats.get('chem_shift') or []:
        if isinstance(item, dict) and item.get('sf_framecode'):
            out[item['sf_framecode']] = rows_for(item, 'shifts', outliers=True)
    for key, noun in (
        ('dist_restraint', 'distance restraints'),
        ('dihed_restraint', 'dihedral-angle restraints'),
        ('rdc_restraint', 'RDC restraints'),
        ('spectral_peak', 'spectral peaks'),
    ):
        for item in stats.get(key) or []:
            if isinstance(item, dict) and item.get('sf_framecode'):
                out[item['sf_framecode']] = rows_for(item, noun)
    return out

# Per-shift columns kept for each unmapped assigned chemical shift
# (output_statistics.chem_shift[].chemical_shift_unmapped) — shown in a collapsible
# table when a saveframe has unmapped shifts.
_CHEM_SHIFT_UNMAPPED_KEYS = (
    'auth_chain_id', 'auth_seq_id', 'ins_code', 'comp_id', 'atom_id',
    'value', 'error', 'ambig_code',
)

# Per-shift columns kept for each chemical shift outlier
# (output_statistics.chem_shift[].chemical_shift_outlier) — shown in a collapsible
# table when a saveframe reports outliers. expected_range is kept as its nested
# {min_value, max_value} object.
_CHEM_SHIFT_OUTLIER_KEYS = (
    'auth_chain_id', 'auth_seq_id', 'ins_code', 'comp_id', 'atom_id',
    'value', 'ambig_code', 'z_score', 'expected_range', 'details',
)

# Assignment-category arrays kept from each completeness-region object
# (output_statistics.chem_shift[].completeness_in_*_region).
_CHEM_SHIFT_COMPLETENESS_KEYS = (
    'completeness_of_overall_assignments',
    'completeness_of_favorable_assignments',
    'completeness_of_backbone_assignments',
    'completeness_of_sidechain_assignments',
    'completeness_of_aromatic_assignments',
    'completeness_of_sugar_assignments',
    'completeness_of_base_assignments',
    'completeness_of_stereomethyl_assignments',
)
_CHEM_SHIFT_COMPLETENESS_ENTRY_KEYS = (
    'atom_group', 'number_of_assigned_shifts', 'number_of_target_shifts', 'completeness',
)


# Per-region columns kept from the coordinate ensemble_composition well-defined
# regions (information.input_sources[file_type='pdbx'].ensemble_composition).
_ENSEMBLE_WDR_KEYS = (
    'domain_id', 'medoid_model_id', 'number_of_monomers',
    'percent_of_core', 'medoid_rmsd', 'range_of_seq_id',
)
# Per-cluster columns kept from ensemble_composition.cluster_analysis (the heavy
# per-model principal_components are dropped). cluster_id == -1 flags the
# single-model (non-cluster) models.
_ENSEMBLE_CLUSTER_KEYS = (
    'cluster_id', 'model_ids', 'centroid_model_id', 'mean_rmsd',
)


def _ensemble_composition(report):
    """Ensemble composition from the coordinate (pdbx) input source, pruned to the
    total model count and the well-defined region table. Returns None when absent."""
    for src in report.get('information', {}).get('input_sources') or []:
        if not isinstance(src, dict) or src.get('file_type') != 'pdbx':
            continue
        ec = src.get('ensemble_composition')
        if not isinstance(ec, dict):
            return None
        wdr = ec.get('well_defined_region')
        regions = [
            {k: d[k] for k in _ENSEMBLE_WDR_KEYS if k in d}
            for d in wdr if isinstance(d, dict)
        ] if isinstance(wdr, list) else []
        if not regions:
            return None
        out = {'well_defined_region': regions}
        if isinstance(ec.get('total_models'), int):
            out['total_models'] = ec['total_models']
        if isinstance(ec.get('representative_model_id'), int):
            out['representative_model_id'] = ec['representative_model_id']
        if isinstance(ec.get('selection_criteria'), str) and ec['selection_criteria']:
            out['selection_criteria'] = ec['selection_criteria']
        clusters = ec.get('cluster_analysis')
        if isinstance(clusters, list) and clusters:
            cluster_rows = []
            for c in clusters:
                if not isinstance(c, dict):
                    continue
                crow = {k: c[k] for k in _ENSEMBLE_CLUSTER_KEYS if k in c}
                # Per-model PC coordinates for the PCA scatter (PC1/PC2).
                pcs = c.get('principal_components')
                if isinstance(pcs, list) and pcs:
                    crow['principal_components'] = [
                        {k: p[k] for k in ('model_id', 'pc1', 'pc2') if k in p}
                        for p in pcs if isinstance(p, dict)
                    ]
                cluster_rows.append(crow)
            out['cluster_analysis'] = cluster_rows
        return out
    return None


def _prune_completeness(region):
    """Prune a completeness_in_*_region object to its known assignment-category
    arrays, each entry reduced to atom_group / assigned / target / completeness.
    Returns None when there is nothing to show."""
    if not isinstance(region, dict):
        return None
    out = {}
    for key in _CHEM_SHIFT_COMPLETENESS_KEYS:
        arr = region.get(key)
        if isinstance(arr, list) and arr:
            out[key] = [
                {k: e[k] for k in _CHEM_SHIFT_COMPLETENESS_ENTRY_KEYS if k in e}
                for e in arr if isinstance(e, dict)
            ]
    return out or None


@app.route('/api/output_statistics', methods=['GET'])
async def get_output_statistics():
    """Conversion statistics (information.output_statistics) from the same report
    as /api/nmr_preview (the convert_nmr_data workflow log_path JSON). Returns the
    subtree pruned of the validation sections (_OUTPUT_STATS_EXCLUDE) — the
    download page shows only the entry/assembly/entity/software metadata.
    Token-scoped, read-only; available=false when there is no report yet."""
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
            return {'available': False}
        wf = (
            await db.execute(
                select(Workflow).where(
                    Workflow.conversion_id == conversion_id,
                    Workflow.run_number == run_number,
                    Workflow.task == WfTaskCode.convert_nmr_data.value,
                )
            )
        ).scalar_one_or_none()

    if wf is None or not wf.log_path or not Path(wf.log_path).is_file():
        return {'available': False}
    try:
        report = json.loads(Path(wf.log_path).read_text())
    except Exception:  # noqa: BLE001
        return {'available': False}

    stats = report.get('information', {}).get('output_statistics')
    if not stats:
        return {'available': False}
    pruned = {k: v for k, v in stats.items() if k not in _OUTPUT_STATS_EXCLUDE}
    # restraint_summary: keep only the scalar restraint counts for the key-value
    # card — drop the average/violation tables (all arrays) and any non-scalar.
    rs = pruned.get('restraint_summary')
    if isinstance(rs, dict):
        summary = {
            k: v for k, v in rs.items()
            if isinstance(v, (int, float, str))
            and 'average' not in k and 'violation' not in k
        }
        # Keep the per-model distance/dihedral-violation bins (small/medium/large)
        # for the 'Average number of ... violations per model' tables.
        for avg_key in ('average_number_of_dist_violations_per_model',
                        'average_number_of_dihed_violations_per_model'):
            avg = rs.get(avg_key)
            if isinstance(avg, list) and avg:
                summary[avg_key] = [
                    {k: e[k] for k in ('bin_type', 'average_number_of_violations_per_model',
                                       'max_violation_in_bin') if k in e}
                    for e in avg if isinstance(e, dict)
                ]
        # Keep the per-category distance/dihedral-violation summary tables.
        for vs_key in ('dist_violation_summary', 'dihed_violation_summary'):
            vs = rs.get(vs_key)
            if isinstance(vs, list) and vs:
                summary[vs_key] = [
                    {k: e[k] for k in _DIST_VIOLATION_SUMMARY_KEYS if k in e}
                    for e in vs if isinstance(e, dict)
                ]
        # Keep the per-model violation statistics (dynamic scalar keys: per-type
        # *_viol_count plus mean/min/max/std/median_violation) and the per-ensemble
        # fraction breakdown (fraction_count / fraction_percent + *_viol_count).
        for m_key in ('dist_violation_for_each_model', 'dihed_violation_for_each_model',
                      'dist_violation_for_ensemble', 'dihed_violation_for_ensemble'):
            mv = rs.get(m_key)
            if isinstance(mv, list) and mv:
                summary[m_key] = [
                    {k: v for k, v in e.items() if isinstance(v, (int, float)) or v is None}
                    for e in mv if isinstance(e, dict)
                ]
        # Most-violated restraint tables (kept to their display columns).
        for mv_key in ('most_violated_dist_restraints', 'most_violated_dihed_restraints'):
            mv = rs.get(mv_key)
            if isinstance(mv, list) and mv:
                summary[mv_key] = [
                    {k: e[k] for k in _MOST_VIOLATED_KEYS if k in e}
                    for e in mv if isinstance(e, dict)
                ]
        # All per-model violation entries (potentially large; kept to columns).
        for av_key in ('all_dist_violations', 'all_dihed_violations'):
            av = rs.get(av_key)
            if isinstance(av, list) and av:
                summary[av_key] = [
                    {k: e[k] for k in _ALL_VIOLATION_KEYS if k in e}
                    for e in av if isinstance(e, dict)
                ]
        pruned['restraint_summary'] = summary
    # chem_shift: keep only the per-saveframe bookkeeping counts for the
    # 'Assigned chemical shift summary' — drop the heavy validation sub-tables.
    cs = pruned.get('chem_shift')
    if isinstance(cs, list):
        saveframes = []
        for item in cs:
            if not isinstance(item, dict):
                continue
            row = {k: item[k] for k in _CHEM_SHIFT_STATS_KEYS if k in item}
            # Unmapped assigned shifts (rendered as a collapsible table when > 0).
            unmapped = item.get('chemical_shift_unmapped')
            if isinstance(unmapped, list) and unmapped:
                row['chemical_shift_unmapped'] = [
                    {k: u[k] for k in _CHEM_SHIFT_UNMAPPED_KEYS if k in u}
                    for u in unmapped if isinstance(u, dict)
                ]
            # Chemical shift outliers (rendered as a collapsible table when > 0).
            outlier = item.get('chemical_shift_outlier')
            if isinstance(outlier, list) and outlier:
                row['chemical_shift_outlier'] = [
                    {k: o[k] for k in _CHEM_SHIFT_OUTLIER_KEYS if k in o}
                    for o in outlier if isinstance(o, dict)
                ]
            # Unparsed shifts (same column shape as unmapped; collapsible when > 0).
            unparsed = item.get('chemical_shift_unparsed')
            if isinstance(unparsed, list) and unparsed:
                row['chemical_shift_unparsed'] = [
                    {k: u[k] for k in _CHEM_SHIFT_UNMAPPED_KEYS if k in u}
                    for u in unparsed if isinstance(u, dict)
                ]
            # Duplicated shifts (same column shape as unmapped; collapsible when > 0).
            duplicated = item.get('chemical_shift_duplicated')
            if isinstance(duplicated, list) and duplicated:
                row['chemical_shift_duplicated'] = [
                    {k: d[k] for k in _CHEM_SHIFT_UNMAPPED_KEYS if k in d}
                    for d in duplicated if isinstance(d, dict)
                ]
            # Assignment-completeness pivot tables (well-defined / full-length).
            for region_key in (
                'completeness_in_well_defined_region', 'completeness_in_full_length_region',
            ):
                region = _prune_completeness(item.get(region_key))
                if region:
                    row[region_key] = region
            # Normalized (Z-score) assigned-chemical-shift histogram (same chart
            # data as the summary page); inverse axis to match NMR-spectrum sense.
            # Author→CCD atom-name mapping history (same shape as the summary page).
            anm = _atom_name_mapping(item)
            if anm:
                row['atom_name_mapping'] = anm
            histogram = _histogram_chart([item], True)
            if histogram:
                row['histogram'] = histogram
            # RCI/S² and NMR-RMSD per-residue plots (coordinate residue scheme).
            rci_charts = _rci_charts([item], auth=True)
            if rci_charts:
                row['rci'] = rci_charts
            saveframes.append(row)
        pruned['chem_shift'] = saveframes
    result = {'available': True, 'statistics': pruned}
    # Report file modification time (when the conversion produced it), UTC.
    try:
        mtime = Path(wf.log_path).stat().st_mtime
        result['report_timestamp'] = (
            datetime.fromtimestamp(mtime, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        )
    except OSError:
        pass
    ensemble = _ensemble_composition(report)
    if ensemble:
        result['ensemble_composition'] = ensemble
    # Per-saveframe bookkeeping for the restraint / spectral-peak subtypes (the
    # heavy validation sub-tables are excluded from `pruned`; here we keep only
    # the common bookkeeping counts + atom-name mapping).
    result['restraint_bookkeeping'] = {
        key: _bookkeeping_saveframes(stats, key)
        for key in ('dist_restraint', 'dihed_restraint', 'rdc_restraint', 'spectral_peak')
    }
    return result


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


@app.route('/api/approve', methods=['POST'])
async def approve_session():
    """Set session.approved — the user's acknowledgment of all warnings (Terms #7),
    which gates download. The frontend computes the value (all acknowledgeable
    validation tables checked, no blocking error); this only persists it.

    JSON body: { token, approved }. 400 missing token / non-bool; 404 no session;
    409 before processing (no conversion_id) or after download (locked).
    """
    body = request.get_json(silent=True) or {}
    token = body.get('token')
    approved = body.get('approved')

    if not token or not isinstance(approved, bool):
        return {'error': 'token and boolean approved are required'}, 400

    async with async_session_factory() as db:
        session_row = (
            await db.execute(select(Session).where(Session.token == token))
        ).scalar_one_or_none()
        if session_row is None:
            return {'error': 'session not found'}, 404
        if session_row.conversion_id is None:
            return {'error': 'session not yet processed'}, 409
        if session_row.downloaded:
            return {'error': 'session is locked after download'}, 409
        session_row.approved = approved
        await db.commit()

    return {'approved': approved}, 200


# ── Consent ───────────────────────────────────────────────────────────────────

@app.route('/api/new_consent', methods=['POST'])
async def new_consent():
    async with async_session_factory() as db:
        # Associate the session with the logged-in user (if any) so it appears in
        # their "my sessions" list; anonymous sessions keep user_id NULL.
        _, user = await current_auth(db)
        new_session = Session(
            token_expiry=datetime.now() + timedelta(days=FAILURE_VALIDITY_PERIOD_IN_DAYS),
            consented=True,
            user_id=user.id if user is not None else None,
            client_ip=request.remote_addr,
            user_agent=request.headers.get('User-Agent', ''),
        )
        db.add(new_session)
        await db.commit()
        await db.refresh(new_session)
        return {'token': str(new_session.token)}


@app.route('/api/consent', methods=['POST'])
async def update_consent():
    """Set session.consented — the user's agreement to the Terms & Privacy Policy.
    Toggled when the user checks/unchecks the consent box on an existing session;
    persisting it means a revoked consent is enforced on reload / direct URL.

    JSON body: { token, consented }. 400 missing token / non-bool; 404 no session.
    """
    body = request.get_json(silent=True) or {}
    token = body.get('token')
    consented = body.get('consented')

    if not token or not isinstance(consented, bool):
        return {'error': 'token and boolean consented are required'}, 400

    async with async_session_factory() as db:
        session_row = (
            await db.execute(select(Session).where(Session.token == token))
        ).scalar_one_or_none()
        if session_row is None:
            return {'error': 'session not found'}, 404
        session_row.consented = consented
        await db.commit()

    return {'consented': consented}, 200


# ── File upload ───────────────────────────────────────────────────────────────

@app.route('/api/upload', methods=['POST'])
async def upload():
    """Store an uploaded file in the session archive and create a DB record.

    Multipart form fields: token, file (binary), file_type (optional — null
    until the user assigns a type in the UI).
    On the first upload for a session, git init is called for the session directory.
    Returns: { ordinal, stored_path, file_size }
    """
    token = request.form.get('token')
    file_type = request.form.get('file_type') or None
    f = request.files.get('file')

    if not all([token, f]):
        return {'error': 'token and file are required'}, 400

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

        # Reset approval: new files invalidate any prior warning acknowledgment.
        await db.execute(
            update(Session)
            .where(Session.token == token)
            .values(status=SessionStatusCode.uploading, approved=False)
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
        if session_row.downloaded:
            return {'error': 'session is locked after download'}, 409

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


@app.route('/api/upload', methods=['PATCH'])
async def patch_upload():
    """Update a draft file's type and/or selection without re-uploading it.

    JSON body: { token, ordinal, file_type?, selected? }

    Applies whichever of file_type / selected is present. `file_type` may be
    null (the user cleared an unrecognised type). Mirrors POST's side effect:
    an edit invalidates a prior approval, so the session returns to 'uploading'
    with approved=False.
    Returns: { ok: true }
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
        if session_row.downloaded:
            return {'error': 'session is locked after download'}, 409

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

        if 'file_type' in body:
            upload_row.file_type = body.get('file_type') or None
        if 'selected' in body:
            upload_row.selected = bool(body.get('selected'))

        # Reset approval: an edit invalidates any prior warning acknowledgment.
        session_row.status = SessionStatusCode.uploading
        session_row.approved = False
        await db.commit()

    return {'ok': True}, 200


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
        has_uni = any(f.file_type and f.file_type.startswith('nm-uni-') for f in selected_files)
        has_user_shifts = any(
            f.file_type and f.file_type.startswith('nm-shi')
            and f.source != UploadFileSource.bmrb.value
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
