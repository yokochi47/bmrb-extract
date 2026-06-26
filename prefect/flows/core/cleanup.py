"""Enforce the data-retention policy (T&C #8) by expiring stale sessions.

`session.token_expiry` is set/refreshed at the end of every run, but nothing
acts on it — so without this flow expired archives and workspaces would pile up
on disk forever and sessions would never reach the `expired` status.

Runs nightly (02:00 local site time; see prefect.yaml). For each session whose
`token_expiry` has passed and that is not already `expired`, it:
  * deletes the on-disk data — the per-session archive /<archive>/<token>/ (the
    git repo with all uploaded files) and the per-conversion workspace
    /<workspace>/<conversion_id>/ (ephemeral; outputs are already in the DB),
  * purges the session's child rows (upload_file / output_file / workflow /
    notification / communication), keeping the session row marked `expired` so
    the conversion id (C_<id>) stays on record for support history,
  * emails the admin a single summary of the run, including any per-session
    errors (e.g. permission failures). A flow-wide error is also emailed and
    re-raised so Prefect records the run as failed.

Runs in the prefect-worker, reusing the shared service ORM/config (mounted at
prefect/flows/shared/core -> backend/app/core) like process_session.py.
"""

import os
import shutil
import smtplib
import sys
import traceback
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from prefect import flow

# Make the flow's sibling modules and the shared service ORM + config importable
# before importing them (same pattern as process_session.py / versions.py).
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'shared'))

import asyncio  # noqa: E402

from sqlalchemy import delete, func, select, update  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from core.models import (  # noqa: E402
    Communication,
    Notification,
    OutputFile,
    Session,
    SessionStatusCode,
    UploadFile,
    Workflow,
)
from core.site_config import (  # noqa: E402
    ARCHIVE_BASE_PATH,
    SERVICE_ADMIN_EMAIL,
    SERVICE_HOST,
    SERVICE_DATABASE_URL,
    SMTP_SERVER,
    WORKSPACE_BASE_PATH,
)


def _send_admin_email(subject: str, content: str) -> str:
    """Send a plain-text email to the site admin (best-effort; plain internal
    relay on port 25). Returns 'sent' or 'failed'. Mirrors the helper of the
    same name in process_session.py — flow modules are loaded standalone (the
    shared `core.*` package holds only the ORM/config), so it is duplicated
    here rather than cross-imported."""
    try:
        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From'] = SERVICE_ADMIN_EMAIL
        msg['To'] = SERVICE_ADMIN_EMAIL
        msg.set_content(content)
        with smtplib.SMTP(SMTP_SERVER, 25, timeout=30) as smtp:
            smtp.send_message(msg)
        return 'sent'
    except Exception as exc:  # noqa: BLE001
        print(f'admin email FAILED ({exc})')
        return 'failed'


def _dir_size(path):
    """Best-effort total byte size of a directory tree; swallows per-entry
    errors (broken symlinks, races) so it never aborts a cleanup."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _human_bytes(n):
    """Human-readable byte count for the summary email."""
    size = float(n)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if size < 1024 or unit == 'TiB':
            return f'{int(size)} B' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024
    return f'{n} B'


async def cleanup_expired(archive_base=ARCHIVE_BASE_PATH, workspace_base=WORKSPACE_BASE_PATH):
    """Expire every session past its token_expiry: delete its on-disk data,
    purge its child rows, and mark it `expired`.

    Each session is handled in its own try/except and transaction so one
    failure (e.g. a permission error on rmtree) is recorded and the rest of the
    batch still proceeds. Returns a summary dict for the admin email.
    """
    engine = create_async_engine(SERVICE_DATABASE_URL, poolclass=NullPool)
    expired = []
    errors = []
    bytes_freed = 0
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            rows = (
                await db.execute(
                    select(Session.token, Session.conversion_id).where(
                        Session.token_expiry < func.now(),
                        Session.status != SessionStatusCode.expired,
                    )
                )
            ).all()

        for token, conversion_id in rows:
            label = f'C_{conversion_id}' if conversion_id is not None else str(token)
            try:
                freed = 0

                # 1. Remove on-disk data. Measure before deleting (best-effort).
                archive_dir = os.path.join(archive_base, str(token))
                if os.path.isdir(archive_dir):
                    freed += _dir_size(archive_dir)
                    shutil.rmtree(archive_dir)
                if conversion_id is not None:
                    workspace_dir = os.path.join(workspace_base, str(conversion_id))
                    if os.path.isdir(workspace_dir):
                        freed += _dir_size(workspace_dir)
                        shutil.rmtree(workspace_dir)

                # 2. Purge child rows, then mark the session expired (kept). Only
                #    after the files are gone, so a failure above leaves the
                #    session un-expired and it is retried on the next run.
                async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                    await db.execute(delete(UploadFile).where(UploadFile.token == token))
                    if conversion_id is not None:
                        await db.execute(
                            delete(OutputFile).where(OutputFile.conversion_id == conversion_id)
                        )
                        await db.execute(
                            delete(Workflow).where(Workflow.conversion_id == conversion_id)
                        )
                        await db.execute(
                            delete(Notification).where(Notification.conversion_id == conversion_id)
                        )
                        await db.execute(
                            delete(Communication).where(
                                Communication.conversion_id == conversion_id
                            )
                        )
                    await db.execute(
                        update(Session)
                        .where(Session.token == token)
                        .values(status=SessionStatusCode.expired)
                    )
                    await db.commit()

                bytes_freed += freed
                expired.append({'label': label, 'bytes_freed': freed})
                print(f'cleanup: expired {label} ({_human_bytes(freed)} freed)')
            except Exception as exc:  # noqa: BLE001
                errors.append({'label': label, 'error': str(exc)})
                print(f'cleanup: FAILED {label}: {exc}')
    finally:
        await engine.dispose()

    return {'expired': expired, 'errors': errors, 'bytes_freed': bytes_freed}


def _email_summary(summary):
    """Email the admin a single summary of the cleanup run (sent every run)."""
    expired = summary['expired']
    errors = summary['errors']
    subject = (
        f'[bmrb-extract:{SERVICE_HOST}] Data-retention cleanup: '
        f'{len(expired)} expired, {len(errors)} error(s)'
    )
    lines = [
        f'Data-retention cleanup run on {SERVICE_HOST}.',
        '',
        f'Time       : {datetime.now().isoformat(timespec="seconds")}',
        f'Expired    : {len(expired)} session(s)',
        f'Disk freed : {_human_bytes(summary["bytes_freed"])}',
        f'Errors     : {len(errors)}',
        '',
    ]
    if expired:
        lines.append('Expired sessions:')
        lines += [f'  - {e["label"]} ({_human_bytes(e["bytes_freed"])})' for e in expired]
        lines.append('')
    if errors:
        lines.append('Errors:')
        lines += [f'  - {er["label"]}: {er["error"]}' for er in errors]
        lines.append('')
    content = '\n'.join(lines)
    delivery_status = _send_admin_email(subject, content)
    print(f'cleanup: admin summary email {delivery_status}')
    return delivery_status


@flow(name='cleanup-sessions')
def cleanup_sessions_flow(
    archive_base: str = ARCHIVE_BASE_PATH,
    workspace_base: str = WORKSPACE_BASE_PATH,
) -> dict:
    """Scheduled (and on-demand) data-retention cleanup of expired sessions."""
    try:
        summary = asyncio.run(cleanup_expired(archive_base, workspace_base))
    except Exception:  # noqa: BLE001
        # A flow-wide failure (DB unreachable, etc.) is reported to the admin
        # and re-raised so Prefect marks the run failed.
        subject = f'[bmrb-extract:{SERVICE_HOST}] Data-retention cleanup FAILED'
        content = (
            f'The data-retention cleanup flow failed on {SERVICE_HOST}.\n\n'
            f'Time: {datetime.now().isoformat(timespec="seconds")}\n\n'
            f'{traceback.format_exc()}'
        )
        _send_admin_email(subject, content)
        raise
    _email_summary(summary)
    return summary
