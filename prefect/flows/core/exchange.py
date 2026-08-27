"""Cross-site data exchange (Osaka <-> UConn).

The two production sites (bmrb-extract.bmrb.io / bmrb-extract.pdbj.org) run as
independent deployments with disjoint conversion-id ranges. To let annotators at
one site troubleshoot conversions that ran on the other, this flow — scheduled
every 6 hours (see prefect.yaml) — PULLS the *peer* site's valid sessions (those
with a conversion_id) and merges them into the local DB:

  * over one SSH channel it reads the peer's own session rows + child rows
    (upload_file / output_file / workflow) as CSV (`COPY (...) TO STDOUT`), and
  * rsyncs the peer's on-disk data — the per-session git archive
    /<archive>/<token>/ and the per-conversion workspace /<workspace>/<conv_id>/
    (outputs + per-task logs) — into the local bind volumes.

Imported sessions are tagged `processing_site = <peer>` and `exchanged = TRUE`,
with `user_id = NULL` (no peer user/PII copied). Because conversion-id ranges are
disjoint per site and `token`/`token_admin` are UUIDs, merging never collides
with local rows; the local allocator ignores foreign ids (it filters max() to the
local range). Only the peer's *own* sessions are imported (WHERE processing_site =
<peer>), so previously-imported foreign sessions are never re-exported (no echo).

The flow no-ops while PEER_HOST is empty (safe to schedule before the peer's IP
is known). Per-run failures are NOT emailed; instead, if no exchange has
succeeded for 24 h, the admin is emailed ONCE per day (so a whole day of failed
attempts raises a single alert). Mirrors the standalone-module + own-async-engine
+ _send_admin_email pattern of cleanup.py.
"""

import json
import os
import shlex
import subprocess
import sys
import traceback
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

from prefect import flow

# Make sibling modules and the shared service ORM/config importable (same pattern
# as process_session.py / cleanup.py).
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'shared'))

import asyncio  # noqa: E402

from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

import core.site_config as cfg  # noqa: E402
from core.models import OutputFile, Session, UploadFile, Workflow  # noqa: E402
from core.site_config import (  # noqa: E402
    ARCHIVE_BASE_PATH,
    SERVICE_ADMIN_EMAIL,
    SERVICE_DATABASE_URL,
    SERVICE_DOMAIN,
    SERVICE_HOST,
    WORKSPACE_BASE_PATH,
)

# --- peer config (read defensively so the flow imports even before config.sh
# has re-rendered site_config.py with the PEER_* constants) --------------------
PEER_HOST = getattr(cfg, 'PEER_HOST', '') or ''
PEER_DOMAIN = getattr(cfg, 'PEER_DOMAIN', '') or ''
PEER_SSH_USER = getattr(cfg, 'PEER_SSH_USER', '') or 'bmrbxchg'
PEER_SSH_PORT = str(getattr(cfg, 'PEER_SSH_PORT', '') or '22')
PEER_SSH_KEY = getattr(cfg, 'PEER_SSH_KEY', '') or '/secrets/peer_ssh_key'
PEER_KNOWN_HOSTS = getattr(cfg, 'PEER_KNOWN_HOSTS', '') or '/secrets/peer_known_hosts'
PEER_PSQL = getattr(cfg, 'PEER_PSQL', '') or 'psql -d internal'
PEER_ARCHIVE_DIR = getattr(cfg, 'PEER_ARCHIVE_DIR', '') or '/var/lib/archive'
PEER_WORKSPACE_DIR = getattr(cfg, 'PEER_WORKSPACE_DIR', '') or '/var/lib/workspace'

# State file for the daily-failure alert (persisted on the workspace volume).
_STATE_PATH = os.path.join(WORKSPACE_BASE_PATH, '.exchange', 'state.json')


def _peer_domain() -> str:
    """The peer's processing_site value: configured PEER_DOMAIN, else the other
    of the two production domains derived from this site's SERVICE_DOMAIN."""
    if PEER_DOMAIN:
        return PEER_DOMAIN
    return {'bmrb.io': 'pdbj.org', 'pdbj.org': 'bmrb.io'}.get(SERVICE_DOMAIN, '')


def _send_admin_email(subject: str, content: str) -> str:
    """Plain-text email to the site admin (best-effort; internal relay, port 25).
    Duplicated from cleanup.py / process_session.py — flow modules load standalone.
    """
    try:
        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From'] = SERVICE_ADMIN_EMAIL
        msg['To'] = SERVICE_ADMIN_EMAIL
        msg.set_content(content)
        from core.local_mail import send_message as _send_message  # noqa: E402
        _send_message(msg, timeout=30)
        return 'sent'
    except Exception as exc:  # noqa: BLE001
        print(f'exchange: admin email FAILED ({exc})')
        return 'failed'


# --- SSH / rsync / peer-query helpers ---------------------------------------- #

def _ssh_opts_argv() -> list:
    return [
        '-i', PEER_SSH_KEY,
        '-p', PEER_SSH_PORT,
        '-o', 'BatchMode=yes',
        '-o', 'IdentitiesOnly=yes',
        '-o', f'UserKnownHostsFile={PEER_KNOWN_HOSTS}',
        '-o', 'StrictHostKeyChecking=accept-new',
        '-o', 'ConnectTimeout=20',
    ]


def _ssh_cmd_str() -> str:
    """The `-e` transport string for rsync (same options as _ssh_opts_argv)."""
    parts = ['ssh'] + _ssh_opts_argv()
    return ' '.join(shlex.quote(p) for p in parts)


def _peer_copy(select_sql: str) -> list:
    """Run `COPY (<select_sql>) TO STDOUT WITH (FORMAT csv, HEADER)` on the peer's
    'internal' DB over SSH (the COPY statement is piped to the peer psql on stdin,
    avoiding nested shell quoting) and return the rows as a list of dicts."""
    import csv
    import io

    remote = shlex.split(PEER_PSQL) + ['-q', '-v', 'ON_ERROR_STOP=1']
    argv = ['ssh'] + _ssh_opts_argv() + [f'{PEER_SSH_USER}@{PEER_HOST}'] + remote
    sql = f'COPY (\n{select_sql}\n) TO STDOUT WITH (FORMAT csv, HEADER true);\n'
    proc = subprocess.run(argv, input=sql, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f'peer query failed (rc={proc.returncode}): {proc.stderr.strip()[:400]}')
    return list(csv.DictReader(io.StringIO(proc.stdout)))


def _rsync(remote_src: str, local_dst: str) -> None:
    """Incrementally mirror a peer directory into a local one over SSH. Tolerates
    rsync's "some files vanished / partial" codes (23/24)."""
    os.makedirs(local_dst, exist_ok=True)
    argv = [
        'rsync', '-a', '--timeout=600', '-e', _ssh_cmd_str(),
        f'{PEER_SSH_USER}@{PEER_HOST}:{remote_src}', local_dst,
    ]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=1800)
    if proc.returncode not in (0, 23, 24):
        raise RuntimeError(f'rsync failed (rc={proc.returncode}): {proc.stderr.strip()[:300]}')


# --- CSV value coercion ------------------------------------------------------ #

def _s(v):
    return v if v not in (None, '') else None


def _i(v):
    return int(v) if v not in (None, '') else None


def _b(v):
    # PostgreSQL CSV renders booleans as 't' / 'f'.
    return (v == 't') if v not in (None, '') else None


def _ts(v):
    return datetime.fromisoformat(v) if v not in (None, '') else None


# --- SQL (only the peer's own valid sessions; child rows joined to them) ------ #

def _session_sql(peer):
    return f"""SELECT token, conversion_id, token_admin, token_expiry, consented,
       status, target_depsys, related_bmrb_id, latest_run_number,
       created_at, started_at, finished_at, approved, downloaded, help_user_seen_at
FROM session
WHERE processing_site = '{peer}' AND conversion_id IS NOT NULL"""


def _upload_sql(peer):
    return f"""SELECT uf.token, uf.ordinal, uf.conversion_id, uf.run_number,
       uf.original_name, uf.stored_path, uf.file_size, uf.checksum, uf.file_type,
       uf.selected, uf.source, uf.uploaded_at
FROM upload_file uf JOIN session s ON s.token = uf.token
WHERE s.processing_site = '{peer}' AND s.conversion_id IS NOT NULL"""


def _output_sql(peer):
    return f"""SELECT of.conversion_id, of.run_number, of.ordinal, of.stored_path,
       of.file_size, of.checksum, of.file_type, of.downloaded, of.downloaded_at
FROM output_file of JOIN session s ON s.conversion_id = of.conversion_id
WHERE s.processing_site = '{peer}' AND s.conversion_id IS NOT NULL"""


def _workflow_sql(peer):
    return f"""SELECT w.conversion_id, w.run_number, w.ordinal, w.task, w.status,
       w.log_path, w.report_status, w.report_summary,
       w.created_at, w.started_at, w.finished_at, w.expiry_at
FROM workflow w JOIN session s ON s.conversion_id = w.conversion_id
WHERE s.processing_site = '{peer}' AND s.conversion_id IS NOT NULL"""


def _session_values(row, peer):
    return {
        'token': _s(row['token']),
        'conversion_id': _i(row['conversion_id']),
        'token_admin': _s(row['token_admin']),
        'token_expiry': _ts(row['token_expiry']),
        'consented': _b(row['consented']),
        'status': _s(row['status']),
        'target_depsys': _s(row['target_depsys']),
        'related_bmrb_id': _i(row['related_bmrb_id']),
        'latest_run_number': _i(row['latest_run_number']),
        'created_at': _ts(row['created_at']),
        'started_at': _ts(row['started_at']),
        'finished_at': _ts(row['finished_at']),
        'approved': _b(row['approved']),
        'downloaded': _b(row['downloaded']),
        'help_user_seen_at': _ts(row['help_user_seen_at']),
        # Provenance / anonymization for imported rows.
        'processing_site': peer,
        'user_id': None,
        'exchanged': True,
    }


def _upload_values(row):
    return {
        'token': _s(row['token']), 'ordinal': _i(row['ordinal']),
        'conversion_id': _i(row['conversion_id']), 'run_number': _i(row['run_number']),
        'original_name': _s(row['original_name']), 'stored_path': _s(row['stored_path']),
        'file_size': _i(row['file_size']), 'checksum': _s(row['checksum']),
        'file_type': _s(row['file_type']), 'selected': _b(row['selected']),
        'source': _s(row['source']), 'uploaded_at': _ts(row['uploaded_at']),
    }


def _output_values(row):
    return {
        'conversion_id': _i(row['conversion_id']), 'run_number': _i(row['run_number']),
        'ordinal': _i(row['ordinal']), 'stored_path': _s(row['stored_path']),
        'file_size': _i(row['file_size']), 'checksum': _s(row['checksum']),
        'file_type': _s(row['file_type']), 'downloaded': _b(row['downloaded']),
        'downloaded_at': _ts(row['downloaded_at']),
    }


def _workflow_values(row):
    return {
        'conversion_id': _i(row['conversion_id']), 'run_number': _i(row['run_number']),
        'ordinal': _i(row['ordinal']), 'task': _s(row['task']), 'status': _s(row['status']),
        'log_path': _s(row['log_path']), 'report_status': _s(row['report_status']),
        'report_summary': _s(row['report_summary']), 'created_at': _ts(row['created_at']),
        'started_at': _ts(row['started_at']), 'finished_at': _ts(row['finished_at']),
        'expiry_at': _ts(row['expiry_at']),
    }


async def run_exchange() -> dict:
    """Pull the peer's valid sessions (files + rows) and upsert them locally.

    Returns a summary dict. `reached_peer` is True once the peer session list was
    fetched — i.e. SSH + peer DB read succeeded — regardless of per-session
    outcomes; it drives the daily-failure alert.
    """
    peer = _peer_domain()
    summary = {'reached_peer': False, 'peer': peer, 'imported': 0, 'errors': []}

    # 1. Fetch the peer's session list + child rows (one SSH call each). A failure
    #    here means we could not reach the peer → reached_peer stays False.
    sessions = _peer_copy(_session_sql(peer))
    uploads = _peer_copy(_upload_sql(peer))
    outputs = _peer_copy(_output_sql(peer))
    workflows = _peer_copy(_workflow_sql(peer))
    summary['reached_peer'] = True
    summary['found'] = len(sessions)

    # Index child rows by their owning session for per-session import.
    uploads_by_token = {}
    for r in uploads:
        uploads_by_token.setdefault(r['token'], []).append(r)
    outputs_by_cid = {}
    for r in outputs:
        outputs_by_cid.setdefault(r['conversion_id'], []).append(r)
    workflows_by_cid = {}
    for r in workflows:
        workflows_by_cid.setdefault(r['conversion_id'], []).append(r)

    engine = create_async_engine(SERVICE_DATABASE_URL, poolclass=NullPool)
    try:
        for srow in sessions:
            token = srow['token']
            cid = srow['conversion_id']
            label = f'C_{cid}'
            try:
                # 1a. Mirror files first, so no imported row points at a missing file.
                _rsync(f'{PEER_ARCHIVE_DIR}/{token}/', os.path.join(ARCHIVE_BASE_PATH, token))
                _rsync(f'{PEER_WORKSPACE_DIR}/{cid}/', os.path.join(WORKSPACE_BASE_PATH, cid))

                # 1b. Upsert rows in FK order within one transaction.
                async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                    sv = _session_values(srow, peer)
                    await db.execute(
                        pg_insert(Session).values(**sv).on_conflict_do_update(
                            index_elements=[Session.token],
                            set_={
                                'status': sv['status'],
                                'latest_run_number': sv['latest_run_number'],
                                'started_at': sv['started_at'],
                                'finished_at': sv['finished_at'],
                                'approved': sv['approved'],
                                'downloaded': sv['downloaded'],
                                'help_user_seen_at': sv['help_user_seen_at'],
                                'processing_site': peer,
                                'exchanged': True,
                            },
                        )
                    )
                    for r in uploads_by_token.get(token, []):
                        await db.execute(
                            pg_insert(UploadFile).values(**_upload_values(r))
                            .on_conflict_do_nothing(
                                index_elements=[UploadFile.token, UploadFile.ordinal]
                            )
                        )
                    for r in outputs_by_cid.get(cid, []):
                        await db.execute(
                            pg_insert(OutputFile).values(**_output_values(r))
                            .on_conflict_do_nothing(
                                index_elements=[
                                    OutputFile.conversion_id,
                                    OutputFile.run_number,
                                    OutputFile.ordinal,
                                ]
                            )
                        )
                    for r in workflows_by_cid.get(cid, []):
                        await db.execute(
                            pg_insert(Workflow).values(**_workflow_values(r))
                            .on_conflict_do_nothing(
                                index_elements=[
                                    Workflow.conversion_id,
                                    Workflow.run_number,
                                    Workflow.ordinal,
                                ]
                            )
                        )
                    await db.commit()

                summary['imported'] += 1
                print(f'exchange: imported {label} from {peer}')
            except Exception as exc:  # noqa: BLE001
                summary['errors'].append({'label': label, 'error': str(exc)})
                print(f'exchange: FAILED {label}: {exc}')
    finally:
        await engine.dispose()

    return summary


# --- daily-failure state / alert --------------------------------------------- #

def _load_state() -> dict:
    try:
        with open(_STATE_PATH) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
    tmp = _STATE_PATH + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(state, fh)
    os.replace(tmp, _STATE_PATH)


def _record_and_maybe_alert(ok: bool, summary: dict) -> None:
    """Record the attempt outcome; if no exchange has succeeded for 24 h, email
    the admin once per day (a whole day of failed attempts → a single alert)."""
    now = datetime.now()
    st = _load_state()
    st['last_attempt'] = now.isoformat(timespec='seconds')
    if ok:
        st['last_success'] = now.isoformat(timespec='seconds')
        st['alert_date'] = None
    else:
        last = st.get('last_success')
        stale = last is None or (now - datetime.fromisoformat(last)) >= timedelta(hours=24)
        today = now.date().isoformat()
        if stale and st.get('alert_date') != today:
            since = last or 'never'
            _send_admin_email(
                f'[bmrb-extract:{SERVICE_HOST}] Cross-site data exchange FAILING',
                'All cross-site data-exchange attempts are failing.\n\n'
                f'Site        : {SERVICE_HOST}\n'
                f'Peer        : {summary.get("peer") or PEER_DOMAIN}\n'
                f'Last success: {since}\n'
                f'Time        : {now.isoformat(timespec="seconds")}\n\n'
                f'Latest detail:\n{summary.get("error") or summary}\n',
            )
            st['alert_date'] = today
    _save_state(st)


@flow(name='exchange-sessions')
def exchange_sessions_flow() -> dict:
    """Scheduled (every 6 h) cross-site data exchange. No-op until PEER_HOST and a
    resolvable peer domain are configured."""
    if not PEER_HOST or not _peer_domain():
        print('exchange: peer not configured (PEER_HOST/PEER_DOMAIN empty); skipping')
        return {'skipped': True}

    try:
        summary = asyncio.run(run_exchange())
        ok = bool(summary.get('reached_peer'))
    except Exception:  # noqa: BLE001
        summary = {'reached_peer': False, 'error': traceback.format_exc()}
        ok = False
        print(f'exchange: run FAILED\n{summary["error"]}')

    _record_and_maybe_alert(ok, summary)
    return summary
