import hashlib
import html
import json
import os
import re
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from flask import Flask, request, send_file
from werkzeug.exceptions import HTTPException
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
    Returns: { files: [{ ordinal, original_name, file_size, file_type, selected }] }
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

        result = await db.execute(
            select(
                UploadFile.ordinal,
                UploadFile.original_name,
                UploadFile.file_size,
                UploadFile.file_type,
                UploadFile.selected,
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


# ── Coordinate geometry validation (pdbx_validate_* outliers in the converted mmCIF) ──

# Display order + friendly labels for the geometry-outlier categories maxit writes
# into the converted coordinate. Categories absent from a given file are skipped.
# pdbx_validate_planes_atom is folded into pdbx_validate_planes (nested atoms), so
# it is not listed here as a standalone metric.
_VALIDATE_METRICS = [
    ('pdbx_validate_close_contact', 'Close contacts'),
    ('pdbx_validate_symm_contact', 'Symmetry contacts'),
    ('pdbx_validate_rmsd_bond', 'Bond length outliers'),
    ('pdbx_validate_rmsd_angle', 'Bond angle outliers'),
    ('pdbx_validate_torsion', 'Torsion (Ramachandran) outliers'),
    ('pdbx_validate_peptide_omega', 'Peptide omega outliers'),
    ('pdbx_validate_main_chain_plane', 'Main-chain planarity outliers'),
    ('pdbx_validate_planes', 'Planarity outliers'),
    ('pdbx_validate_chiral', 'Chirality outliers'),
    ('pdbx_validate_polymer_linkage', 'Polymer linkage outliers'),
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
        ['Model', 'Atom 1', 'Atom 2', 'Distance (Å)'],
        [
            lambda g: g('PDB_model_num'),
            lambda g: _join(g, 'auth_asym_id_1', 'auth_comp_id_1', 'auth_seq_id_1', 'auth_atom_id_1'),
            lambda g: _join(g, 'auth_asym_id_2', 'auth_comp_id_2', 'auth_seq_id_2', 'auth_atom_id_2'),
            lambda g: g('dist'),
        ],
    ),
    'pdbx_validate_rmsd_bond': (
        ['Model', 'Chain', 'Bond', 'Value', 'Deviation'],
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
        ['Model', 'Chain', 'Residue', 'Atoms', 'Value', 'Deviation'],
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
        ['Model', 'Chain', 'Residue', 'Phi', 'Psi'],
        [
            lambda g: g('PDB_model_num'),
            lambda g: g('auth_asym_id'),
            lambda g: _join(g, 'auth_comp_id', 'auth_seq_id'),
            lambda g: g('phi'),
            lambda g: g('psi'),
        ],
    ),
    'pdbx_validate_peptide_omega': (
        ['Model', 'Chain', 'Residues', 'Omega'],
        [
            lambda g: g('PDB_model_num'),
            lambda g: g('auth_asym_id_1'),
            lambda g: f"{_join(g, 'auth_comp_id_1', 'auth_seq_id_1')}–"
                      f"{_join(g, 'auth_comp_id_2', 'auth_seq_id_2')}",
            lambda g: g('omega'),
        ],
    ),
    'pdbx_validate_main_chain_plane': (
        ['Model', 'Chain', 'Residue', 'Improper torsion'],
        [
            lambda g: g('PDB_model_num'),
            lambda g: g('auth_asym_id'),
            lambda g: _join(g, 'auth_comp_id', 'auth_seq_id'),
            lambda g: g('improper_torsion_angle'),
        ],
    ),
    'pdbx_validate_planes': (
        ['Model', 'Chain', 'Residue', 'RMSD', 'Type'],
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
    for cat, label in _VALIDATE_METRICS:
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
                  'count': len(rows), 'columns': columns}
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


def _parse_nmr_report(report, unified, model_file_name):
    """Build (errors, warnings) groups from an NmrDpUtility JSON report. errors:
    every type except 'total' (real = unified or designated type). warnings: from
    `warning` grouped by level, plus `corrected_warning` as level 0 (corrected)."""
    errors = []
    for etype, items in (report.get('error') or {}).items():
        if etype == 'total' or not items:
            continue
        real = unified or etype in _NMR_BLOCKING_ERROR_TYPES
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


def _nmr_unified_dep(token, target_depsys):
    """NMR_UNIFIED_DEP = (onedep & nm-uni-* present) | repl_cs — same as the flow's
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

    unified = _nmr_unified_dep(token, target_depsys)
    errors, warnings = _parse_nmr_report(report, unified, _nmr_model_file_name(report))
    return {
        'available': True,
        'status': report.get('information', {}).get('status'),
        'unified': unified,
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


def _iso_label(key):
    """Format an isotope-bearing key (e.g. '1h_chemical_shifts', 'all_13c_…',
    '15n') into a superscript-mass element label like ¹H / ¹³C / ¹⁵N. Falls back
    to a title-cased label when no isotope token is found."""
    m = _ISOTOPE_RE.search(key)
    if m:
        return m.group(1).translate(_SUPERSCRIPT) + m.group(2).upper()
    return key.replace('_', ' ').strip().title()


def _histogram_chart(stat_list):
    """Build [{label, categories, series}] from a stats list's `histogram`
    ({range_of_values, number_of_values: {key: [counts]}}). All-zero series are
    dropped to reduce clutter."""
    charts = []
    for st in stat_list or []:
        h = st.get('histogram')
        if not isinstance(h, dict) or not h.get('range_of_values'):
            continue
        categories = [str(v) for v in h['range_of_values']]
        nov = h.get('number_of_values') or {}
        series = [
            {'name': _iso_label(k), 'data': v}
            for k, v in nov.items()
            if isinstance(v, list) and any(v)
        ]
        if series:
            charts.append({'label': st.get('sf_framecode', ''),
                           'categories': categories, 'series': series})
    return charts


def _dihedral_charts(stat_list):
    """Build [{label, phi_psi, chi1_chi2}] scatter+error data from a
    dihed_restraint stats list. Each plot → {points:[{name,x,y}], errors:[[...]]}.
    errors arrays are [x, y, x_low, x_high, y_low, y_high] (absolute)."""
    def _plot(plot):
        if not isinstance(plot, dict) or not plot.get('values'):
            return None
        points, errors = [], []
        for vals in plot['values'].values():
            for p in vals:
                if len(p) >= 3:
                    points.append({'name': str(p[2]), 'x': p[0], 'y': p[1]})
        for errs in (plot.get('errors') or {}).values():
            for e in errs:
                errors.append(e)
        if not points:
            return None
        return {'points': points, 'errors': errors}

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


def _sc_type(sc):
    """Secondary-structure class from a struct_conf token (e.g. 'HELX_P:AA1')."""
    if not sc:
        return None
    head = re.split(r'[_:]', str(sc), 1)[0].upper()
    return _STRUCT_CONF_TYPES.get(head)


def _struct_conf_bands(struct_conf):
    """Collapse runs of the same struct_conf value into colored bands
    [{start, end, type}] (indices into the residue list)."""
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
        bands.append({'start': i, 'end': j, 'type': typ})
        i = j + 1
    return bands


def _constraint_label(key):
    """Prettify a constraint-type key, e.g. 'medium_range_constraints_backbone-backbone'
    → 'Medium range backbone-backbone'."""
    s = key.replace('_constraints', '').replace('_', ' ').strip()
    return s[:1].upper() + s[1:] if s else key


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
                {'name': _constraint_label(k), 'data': v}
                for k, v in pr.items()
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


def _discrepancy_charts(stat_list):
    """Histogram charts from `histogram_of_discrepancy` (same shape as `histogram`)."""
    out = []
    for st in stat_list or []:
        hd = st.get('histogram_of_discrepancy')
        if isinstance(hd, dict) and hd.get('range_of_values'):
            out.extend(_histogram_chart([{'sf_framecode': st.get('sf_framecode', ''), 'histogram': hd}]))
    return out


def _restraint_summary(dist_list, dihed_list, rdc_list):
    """Summary rows (type, saveframe, total constraints, value range)."""
    def total(d):
        return sum(v for v in (d or {}).values() if isinstance(v, (int, float)))
    rows = []
    for st in dist_list or []:
        rng = st.get('range') or {}
        rtext = (f"{rng.get('min_value')}–{rng.get('max_value')} Å"
                 if rng.get('min_value') is not None else '')
        rows.append({'type': 'Distance restraints', 'name': st.get('sf_framecode', ''),
                     'total': total(st.get('number_of_constraints')), 'range': rtext})
    for st in dihed_list or []:
        rows.append({'type': 'Dihedral angle restraints', 'name': st.get('sf_framecode', ''),
                     'total': total(st.get('number_of_constraints')), 'range': ''})
    for st in rdc_list or []:
        rng = st.get('range') or {}
        rtext = (f"{rng.get('min_value')}–{rng.get('max_value')} Hz"
                 if rng.get('min_value') is not None else '')
        rows.append({'type': 'RDC restraints', 'name': st.get('sf_framecode', ''),
                     'total': total(st.get('number_of_constraints')), 'range': rtext})
    return rows


def _contact_map_charts(stat_list):
    """Symmetric contact maps from `constraints_on_contact_map`: per chain, one
    series per constraint type with points [seq_id_1, seq_id_2, total]."""
    charts = []
    for st in stat_list or []:
        for cm in st.get('constraints_on_contact_map') or []:
            seq = cm.get('seq_id') or []
            if not seq:
                continue
            series = []
            for k, v in cm.items():
                if k in _PER_RESIDUE_SKIP or not isinstance(v, list):
                    continue
                pts = [[d['seq_id_1'], d['seq_id_2'], d.get('total', 1)]
                       for d in v if isinstance(d, dict) and 'seq_id_1' in d]
                if pts:
                    series.append({'name': _constraint_label(k), 'points': pts})
            if series:
                charts.append({'chain': cm.get('chain_id'), 'label': st.get('sf_framecode', ''),
                               'min': min(seq), 'max': max(seq), 'series': series})
    return charts


def _dim_atom(d):
    """Spectral-dimension atom label, e.g. isotope 13 + type 'C' → ¹³C."""
    iso = d.get('atom_isotope_number')
    atom = d.get('atom_type') or ''
    return _iso_label(f'{iso}{atom}') if iso and atom else atom


def _spectral_peaks(stat_list):
    """(summary rows, per-list dimension tables) for spectral peak lists."""
    summary, dims = [], []
    for st in stat_list or []:
        npk = st.get('number_of_spectral_peaks')
        n_peaks = (sum(v for v in npk.values() if isinstance(v, (int, float)))
                   if isinstance(npk, dict) else npk)
        summary.append({
            'name': st.get('sf_framecode', ''),
            'exp_class': st.get('exp_class') or st.get('exp_type') or '',
            'n_dims': st.get('number_of_spectral_dimensions'),
            'n_peaks': n_peaks,
        })
        rows = [
            {'id': d.get('id'), 'atom': _dim_atom(d), 'region': d.get('spectral_region') or '',
             'sweep_width': d.get('sweep_width'), 'units': d.get('sweep_width_units') or ''}
            for d in st.get('spectral_dim') or []
        ]
        if rows:
            dims.append({'name': st.get('sf_framecode', ''), 'rows': rows})
    return summary, dims


_ANGLE_LABELS = {'phi': 'φ', 'psi': 'ψ', 'chi1': 'χ1', 'chi2': 'χ2', 'chi3': 'χ3', 'chi4': 'χ4'}


def _angle_label(key):
    """Per-residue value-series label: 'phi_angle_constraints' → φ,
    'H-N_bond_vectors' → H-N, etc."""
    base = key.replace('_angle_constraints', '').replace('_bond_vectors', '').replace('_constraints', '')
    return _ANGLE_LABELS.get(base, base.replace('_', ' '))


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
            series = []
            for k, v in cm.items():
                if isinstance(v, list) and v and isinstance(v[0], dict) and 'seq_id_1' in v[0]:
                    pts = [[d['seq_id_1'], d['seq_id_2'], d.get('total', 1)] for d in v]
                    if pts:
                        series.append({'name': _constraint_label(k), 'points': pts})
            if series:
                charts.append({
                    'chain1': cm.get('chain_id_1'), 'chain2': cm.get('chain_id_2'),
                    'label': st.get('sf_framecode', ''),
                    'xmin': min(s1), 'xmax': max(s1), 'ymin': min(s2), 'ymax': max(s2),
                    'series': series,
                })
    return charts


def _rci_charts(chem_shift_list):
    """RCI/S² (0–1) and NMR-RMSD (Å, with well-defined-region threshold) per-residue
    line charts from `random_coil_index`."""
    charts = []
    for st in chem_shift_list or []:
        for rci in st.get('random_coil_index') or []:
            seq = rci.get('seq_id') or []
            comp = rci.get('comp_id') or []
            if not seq:
                continue
            cats = [f"{comp[i] if i < len(comp) else ''} {seq[i]}".strip() for i in range(len(seq))]
            bands = _struct_conf_bands(rci.get('struct_conf'))
            chain = rci.get('chain_id')
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
                charts.append({'chain': chain, 'label': 'NMR RMSD (Å)', 'sf': sf, 'categories': cats,
                               'series': [{'name': 'NMR RMSD', 'data': rmsd}], 'bands': bands,
                               'ymin': 0, 'ymax': None, 'threshold': rci.get('rmsd_in_well_defined_region')})
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


def _dedup_predictions(chem_shift_list, key):
    """First prediction per (chain, seq) across chem_shift saveframes."""
    seen, items = set(), []
    for st in chem_shift_list or []:
        for s in st.get(key) or []:
            k = (s.get('chain_id'), s.get('seq_id'))
            if k not in seen:
                seen.add(k)
                items.append(s)
    return items


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


def _seq_align(info):
    """Sequence alignments grouped by category. Each group carries per-chain rows
    that merge the summary stats with the aligned-sequence block (ref/mid/test),
    so the frontend can show stats and sequences together under one category."""
    groups = []
    for cat, lst in (info.get('sequence_alignments') or {}).items():
        if not isinstance(lst, list) or not lst:
            continue
        rows = []
        for a in lst:
            cov = a.get('sequence_coverage')
            rows.append({
                'chain': a.get('chain_id') or a.get('ref_chain_id') or '',
                'length': a.get('length'), 'matched': a.get('matched'),
                'conflict': a.get('conflict'), 'unmapped': a.get('unmapped'),
                'coverage': round(cov * 100, 1) if isinstance(cov, (int, float)) else None,
                'ref': a.get('ref_code') or '', 'mid': a.get('mid_code') or '',
                'test': a.get('test_code') or '',
            })
        groups.append({'category': _align_label(cat), 'rows': rows})
    return groups


def _nmr_preview_data(report):
    """Extract Phase-1 chart/table data from an NmrDpUtility report. Aggregates
    per-subtype stats (stats_of_exptl_data) across input sources."""
    info = report.get('information', {})
    sources, completeness = [], []
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

    # Per-chain completeness (all-assignments) + sequence coverage, from chem_shift.
    for st in chem_shift:
        cov = {c.get('chain_id'): c.get('sequence_coverage')
               for c in (st.get('sequence_coverage') or [])}
        for comp in st.get('completeness') or []:
            groups = [
                {'group': _iso_label(g.get('atom_group', '')),
                 'target': g.get('number_of_target_shifts'),
                 'assigned': g.get('number_of_assigned_shifts'),
                 'pct': round((g.get('completeness') or 0) * 100, 1)}
                for g in comp.get('completeness_of_all_assignments') or []
            ]
            chain = comp.get('chain_id')
            completeness.append({
                'chain': chain,
                'coverage_pct': round((cov.get(chain) or 0) * 100, 1) if chain in cov else None,
                'groups': groups,
            })

    return {
        'sources': sources,
        'charts': {
            'chem_shift_histogram': _histogram_chart(chem_shift),
            'dist_histogram': _histogram_chart(dist_restraint),
            'dist_discrepancy': _discrepancy_charts(dist_restraint),
            'rdc_histogram': _histogram_chart(rdc_restraint),
            'dihedral': _dihedral_charts(dihed_restraint),
            'per_residue': _per_residue_charts(dist_restraint),
            'contact_maps': _contact_map_charts(dist_restraint),
            'asym_contact_maps': _asym_contact_map_charts(dist_restraint),
            'dihedral_per_residue': _per_residue_value_charts(dihed_restraint, -180, 180),
            'rdc_per_residue': _per_residue_value_charts(rdc_restraint),
            'rci': _rci_charts(chem_shift),
        },
        'restraints': _restraint_summary(dist_restraint, dihed_restraint, rdc_restraint),
        'spectral_peaks': dict(zip(('summary', 'dims'), _spectral_peaks(spectral_peak))),
        'predictions': {
            'cys_redox': _prediction_table(_dedup_predictions(chem_shift, 'cys_redox_state'), 'cys'),
            'pro_cis_trans': _prediction_table(_dedup_predictions(chem_shift, 'pro_cis_trans'), 'pro'),
            'his_tautomer': _prediction_table(_dedup_predictions(chem_shift, 'his_tautomeric_state'), 'his'),
            'ilv_rotamer': _prediction_table(_dedup_predictions(chem_shift, 'ilv_rotameric_state'), 'ilv'),
        },
        'alignments': _seq_align(info),
        'completeness': completeness,
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
