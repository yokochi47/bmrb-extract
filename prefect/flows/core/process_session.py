"""
Prefect flow for NMR data conversion of one session run.

Deploy with:
  prefect deploy prefect/flows/core/process_session.py:process_session \
      --name default --pool local-pool

Then trigger via the Prefect API or directly:
  prefect run deployment process-session/default \
      --param token=<uuid> --param conversion_id=<int> --param run_number=<int>

Active (selected) upload files are copied out of the git-managed archive
(/archive/<token>) into a per-run conversion workspace
(/workspace/<conversion_id>/<run_number>) before any conversion runs, so the
conversions — which may edit input files in place — never touch the archive.
"""

import json
import shutil
import sys
from pathlib import Path

from prefect import flow, task

# core/ is not a package and the flow is loaded by file path, so make the
# sibling workspace helper importable regardless of how Prefect loads us.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import workspace as ws  # noqa: E402

# Reuse the backend service ORM + config: prefect/flows/shared/core is a symlink
# to backend/app/core (mounted read-only in the worker). Inserted at the front so
# `core` resolves to the shared package, not the flow's own core/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'shared'))

import asyncio  # noqa: E402
import smtplib  # noqa: E402
from datetime import datetime  # noqa: E402
from email.message import EmailMessage  # noqa: E402

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from core.models import Notification  # noqa: E402
from core.site_config import (  # noqa: E402
    SERVICE_ADMIN_EMAIL,
    SERVICE_DATABASE_URL,
    SERVICE_HOST,
    SMTP_SERVER,
)


@task(name='issue-conversion', retries=1)
def issue_conversion(
    token: str,
    conversion_id: int,
    run_number: int,
    archive_base: str = '/archive',
    workspace_base: str = ws.WORKSPACE_BASE_PATH,
) -> list:
    """Prepare the per-run workspace and copy active inputs out of the archive.

    Creates /workspace/<conversion_id>/<run_number>/{input,output,work,log} and
    copies every file listed in the run's manifest.json (the selected uploads)
    from the archive into input/. Idempotent under retry: input/ and work/ are
    cleared first so a re-run starts clean.

    Returns the list of copied input file paths (as strings).
    """
    session_dir = Path(archive_base) / token
    manifest = json.loads((session_dir / 'manifest.json').read_text())

    ws.ensure_run_dirs(conversion_id, run_number, workspace_base)
    ws.reset_scratch(conversion_id, run_number, workspace_base)
    dst_dir = ws.input_dir(conversion_id, run_number, workspace_base)

    copied = []
    for f in manifest['files']:
        dst = dst_dir / f['original_name']
        shutil.copy2(f['stored_path'], dst)
        copied.append(str(dst))

    print(f'[{conversion_id}] run #{run_number}: copied {len(copied)} input file(s) to {dst_dir}')
    return copied


@task(name='coordinate-conversion', retries=1)
def coordinate_conversion(
    token: str,
    conversion_id: int,
    run_number: int,
    archive_base: str = '/archive',
    workspace_base: str = ws.WORKSPACE_BASE_PATH,
) -> bool:
    """Convert coordinate file using the maxit-ccd Docker Swarm service.

    Operates entirely inside the workspace: reads coordinate files from input/,
    writes results to output/ and any scratch to work/.

    TODO: implement HTTP call to maxit-ccd service (pass workspace paths).
    """
    session_dir = Path(archive_base) / token
    manifest = json.loads((session_dir / 'manifest.json').read_text())
    in_dir = ws.input_dir(conversion_id, run_number, workspace_base)

    coord_files = [
        in_dir / f['original_name']
        for f in manifest['files']
        if f['file_type'].startswith('co-')
    ]
    if not coord_files:
        print(f'[{conversion_id}] No coordinate file found — skipping coordinate conversion')
        return True

    print(f'[{conversion_id}] TODO: call maxit-ccd for {[p.name for p in coord_files]}')
    return True


@task(name='nmr-data-conversion', retries=1)
def nmr_data_conversion(
    token: str,
    conversion_id: int,
    run_number: int,
    archive_base: str = '/archive',
    workspace_base: str = ws.WORKSPACE_BASE_PATH,
) -> bool:
    """Convert NMR data files using the py-wwpdb_utils_nmr Docker Swarm service.

    Operates entirely inside the workspace: reads NMR files from input/ (which it
    may edit in place to fix minor format issues — safe, they are copies), writes
    results to output/ and any scratch to work/.

    TODO: implement HTTP call to py-wwpdb_utils_nmr service (pass workspace paths).
    """
    session_dir = Path(archive_base) / token
    manifest = json.loads((session_dir / 'manifest.json').read_text())
    in_dir = ws.input_dir(conversion_id, run_number, workspace_base)

    nmr_files = [
        in_dir / f['original_name']
        for f in manifest['files']
        if not f['file_type'].startswith('co-')
    ]
    if not nmr_files:
        print(f'[{conversion_id}] No NMR data files found — skipping NMR conversion')
        return True

    print(f'[{conversion_id}] TODO: call py-wwpdb_utils_nmr for {[p.name for p in nmr_files]}')
    return True


async def _record_notification(
    conversion_id: int, subject: str, content: str, delivery_status: str
) -> None:
    """Insert one row into the service DB `notification` table (async engine,
    same pattern as the backend)."""
    engine = create_async_engine(SERVICE_DATABASE_URL, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            max_ord = (
                await db.execute(
                    select(func.max(Notification.ordinal)).where(
                        Notification.conversion_id == conversion_id
                    )
                )
            ).scalar_one_or_none()
            db.add(
                Notification(
                    conversion_id=conversion_id,
                    ordinal=(max_ord or 0) + 1,
                    subject=subject,
                    content=content,
                    delivery_status=delivery_status,
                )
            )
            await db.commit()
    finally:
        await engine.dispose()


@task(name='notify-new-conversion', retries=0)
def notify_new_conversion(
    token: str,
    conversion_id: int,
    run_number: int,
    archive_base: str = '/archive',
) -> str:
    """Email the site admin that a new conversion was issued and record the
    message in the service DB `notification` table.

    Best-effort: any mail or DB error is logged and swallowed so a notification
    problem never aborts the conversion. Returns the delivery status.
    """
    # Pull a few details for the body; tolerate a missing/unreadable manifest.
    target_depsys, n_files = 'unknown', 0
    try:
        manifest = json.loads((Path(archive_base) / token / 'manifest.json').read_text())
        target_depsys = manifest.get('target_depsys', 'unknown')
        n_files = len(manifest.get('files', []))
    except Exception as exc:  # noqa: BLE001
        print(f'[{conversion_id}] notify: could not read manifest ({exc})')

    subject = f'[bmrb-extract:{SERVICE_HOST}] New conversion C_{conversion_id} issued'
    content = (
        f'A new conversion has been issued on {SERVICE_HOST}.\n\n'
        f'Conversion ID : C_{conversion_id}\n'
        f'Session token : {token}\n'
        f'Run number    : {run_number}\n'
        f'Target depsys : {target_depsys}\n'
        f'Selected files: {n_files}\n'
        f'Issued at     : {datetime.now().isoformat(timespec="seconds")}\n'
    )

    # Send the admin email (best-effort; plain internal relay on port 25).
    delivery_status = 'sent'
    try:
        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From'] = SERVICE_ADMIN_EMAIL
        msg['To'] = SERVICE_ADMIN_EMAIL
        msg.set_content(content)
        with smtplib.SMTP(SMTP_SERVER, 25, timeout=30) as smtp:
            smtp.send_message(msg)
        print(f'[{conversion_id}] notify: admin email sent to {SERVICE_ADMIN_EMAIL}')
    except Exception as exc:  # noqa: BLE001
        delivery_status = 'failed'
        print(f'[{conversion_id}] notify: admin email FAILED ({exc})')

    # Record the message regardless of email outcome (best-effort).
    try:
        asyncio.run(_record_notification(conversion_id, subject, content, delivery_status))
        print(f'[{conversion_id}] notify: recorded notification (delivery_status={delivery_status})')
    except Exception as exc:  # noqa: BLE001
        print(f'[{conversion_id}] notify: DB record FAILED ({exc})')

    return delivery_status


@flow(name='process-session')
def process_session(
    token: str,
    conversion_id: int,
    run_number: int,
    archive_base: str = '/archive',
    workspace_base: str = ws.WORKSPACE_BASE_PATH,
) -> dict:
    """Orchestrate NMR data conversion for one session run.

    Reads /archive/<token>/manifest.json (written by POST /api/process), copies
    the active inputs into the per-run workspace, then runs the coordinate and
    NMR data conversion pipelines against those copies. The archive directory is
    a git repo; each POST /api/process call creates one commit tagged run-<N>.

    Args:
        token:          Session token (UUID string) — the archive subdirectory name.
        conversion_id:  Numeric conversion ID (e.g. C_8000001 → 8000001).
        run_number:     The processing run this invocation handles.
        archive_base:   Base directory of the archive volume (default /archive).
        workspace_base: Base directory of the workspace volume (default /workspace).
    """
    session_dir = Path(archive_base) / token
    manifest = json.loads((session_dir / 'manifest.json').read_text())

    print(
        f'[{conversion_id}] Starting run #{run_number} '
        f'({len(manifest["files"])} selected files, target={manifest["target_depsys"]})'
    )

    try:
        issue_conversion(token, conversion_id, run_number, archive_base, workspace_base)
        # First run only: notify the admin that a new conversion was issued.
        if run_number == 1:
            notify_new_conversion(token, conversion_id, run_number, archive_base)
        coord_ok = coordinate_conversion(token, conversion_id, run_number, archive_base, workspace_base)
        nmr_ok = nmr_data_conversion(token, conversion_id, run_number, archive_base, workspace_base)
        success = coord_ok and nmr_ok
    finally:
        # work/ is pure scratch — drop it whether the run succeeded or failed.
        # output/ and log/ are kept for the validity period (download).
        scratch = ws.work_dir(conversion_id, run_number, workspace_base)
        if scratch.exists():
            shutil.rmtree(scratch, ignore_errors=True)

    print(f'[{conversion_id}] Run #{run_number} complete — success={success}')

    # TODO: update session status in DB (completed / failed) and insert output_file rows
    #       with run_number=run_number (PK = conversion_id, run_number, ordinal),
    #       stored_path pointing under the workspace output/ dir.
    return {'success': success, 'run_number': run_number}
