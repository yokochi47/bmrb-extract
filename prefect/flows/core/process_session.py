"""
Prefect flow for NMR data conversion of one session run.

Deploy with:
  prefect deploy prefect/flows/core/process_session.py:process_session \
      --name default --pool local-pool

Then trigger via the Prefect API or directly:
  prefect run deployment process-session/default \
      --param token=<uuid> --param conversion_id=<int> --param run_number=<int>

Active (selected) upload files are copied out of the git-managed archive
(<archive_base>/<token>) into a per-run conversion workspace
(<workspace_base>/<conversion_id>/<run_number>) before any conversion runs, so the
conversions — which may edit input files in place — never touch the archive.
"""

import json
import os
import shutil
import subprocess
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

from sqlalchemy import func, select, update  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from core.models import Notification, Workflow, WfStatusCode, WfTaskCode  # noqa: E402
from core.site_config import (  # noqa: E402
    MAXIT_CCD_IMAGE,
    MAXIT_MEMORY_LIMIT,
    UTILS_NMR_IMAGE,
    SERVICE_ADMIN_EMAIL,
    SERVICE_DATABASE_URL,
    SERVICE_HOST,
    SMTP_SERVER,
    ARCHIVE_BASE_PATH,
    WORKSPACE_BASE_PATH,
)


@task(name='issue-conversion', retries=1)
def issue_conversion(
    token: str,
    conversion_id: int,
    run_number: int,
    archive_base: str = ARCHIVE_BASE_PATH,
    workspace_base: str = WORKSPACE_BASE_PATH,
) -> list:
    """Prepare the per-run workspace and copy active inputs out of the archive.

    Creates /<workspace_base>/<conversion_id>/<run_number>/{input,output,work,log} and
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


async def _update_workflow_status(
    conversion_id: int,
    run_number: int,
    task: WfTaskCode,
    status: WfStatusCode,
    *,
    started: bool = False,
    finished: bool = False,
    log_path: str | None = None,
    detail: str | None = None,
) -> None:
    """Update one workflow row's status (matched by task code) for this run.

    Whenever a task is set to `failed`, an admin failure notification (email +
    notification table row) is sent automatically. Mark every task failure via
    this helper so the rule applies to all current and future flow tasks; pass
    `detail` for a human-readable failure reason in the notification.
    """
    values = {'status': status}
    if started:
        values['started_at'] = func.now()
    if finished:
        values['finished_at'] = func.now()
    if log_path is not None:
        values['log_path'] = log_path
    engine = create_async_engine(SERVICE_DATABASE_URL, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            await db.execute(
                update(Workflow)
                .where(
                    Workflow.conversion_id == conversion_id,
                    Workflow.run_number == run_number,
                    Workflow.task == task,
                )
                .values(**values)
            )
            await db.commit()
    finally:
        await engine.dispose()

    # Notify the admin on any task failure (best-effort, never raises).
    if status == WfStatusCode.failed:
        await _notify_admin_failure(conversion_id, run_number, task, detail, log_path)


@task(name='coordinate-conversion', retries=0)
def coordinate_conversion(
    token: str,
    conversion_id: int,
    run_number: int,
    archive_base: str = ARCHIVE_BASE_PATH,
    workspace_base: str = WORKSPACE_BASE_PATH,
) -> bool:
    """Convert the uploaded coordinate file to mmCIF with maxit-ccd.

    Runs the maxit-ccd image via `docker run` against the host daemon, capped at
    MAXIT_MEMORY_LIMIT (malformed PDB input leaks memory). Reads the coordinate
    file from the run workspace input/, writes output/C_<id>_model.cif and
    log/C_<id>_model-check.log, and updates the convert_model workflow row
    (processing -> completed, or failed on OOM / non-zero exit / missing output).

    Returns True on success (and when there is no coordinate file, e.g. bmrbdep),
    False on failure.
    """
    manifest = json.loads((Path(archive_base) / token / 'manifest.json').read_text())
    coord = next((f for f in manifest['files'] if f['file_type'].startswith('co-')), None)
    if coord is None:
        print(f'[{conversion_id}] No coordinate file — skipping coordinate conversion')
        asyncio.run(_update_workflow_status(
            conversion_id, run_number, WfTaskCode.convert_model, WfStatusCode.completed,
            started=True, finished=True,
        ))
        return True

    # maxit -o conversion code (verified against the maxit-ccd image):
    #   co-pdb (legacy PDB)   -> -o 1  (PDB  -> mmCIF)
    #   co-cif (PDBx/mmCIF)   -> -o 8  (mmCIF -> mmCIF)
    # (PDB input with -o 8 is rejected as a CIF syntax error and yields no output.)
    o_flag = 1 if coord['file_type'] == 'co-pdb' else 8

    in_path = ws.input_dir(conversion_id, run_number, workspace_base) / coord['original_name']
    out_path = ws.output_dir(conversion_id, run_number, workspace_base) / f'C_{conversion_id}_model.cif'
    log_path = ws.log_dir(conversion_id, run_number, workspace_base) / f'C_{conversion_id}_model-check.log'

    asyncio.run(_update_workflow_status(
        conversion_id, run_number, WfTaskCode.convert_model, WfStatusCode.processing,
        started=True, log_path=str(log_path),
    ))

    # maxit-ccd mounts the host workspace volume at <workspace_base> — the same path the
    # worker uses — so the in-container paths above are valid inside maxit too.
    # Run maxit as the worker's own uid:gid: issue_conversion (this worker) created
    # the run dirs, so maxit must share that uid to write output/log into them.
    # A leaky/malformed conversion is killed by the -m memory cap (OOM -> exit 137).
    failed_reason = None
    try:
        cmd = [
            'docker', 'run', '--rm',
            '-m', MAXIT_MEMORY_LIMIT, '--memory-swap', MAXIT_MEMORY_LIMIT,
            '-u', f'{os.getuid()}:{os.getgid()}',
            '-v', f'{os.environ["WORKSPACE_VOL_DIR"]}:{os.environ["WORKSPACE_BASE_PATH"]}',
            MAXIT_CCD_IMAGE,
            'maxit', '-input', str(in_path), '-output', str(out_path),
            '-o', str(o_flag), '-log', str(log_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if proc.returncode != 0:
            failed_reason = f'maxit exit {proc.returncode}: {(proc.stderr or "").strip()[:300]}'
    except subprocess.TimeoutExpired:
        failed_reason = 'maxit timed out (possible memory leak / hang)'
    except Exception as exc:  # noqa: BLE001
        failed_reason = f'docker run error: {exc}'

    if failed_reason is None and (not out_path.exists() or out_path.stat().st_size == 0):
        failed_reason = 'maxit produced no output file'

    status = WfStatusCode.failed if failed_reason else WfStatusCode.completed
    asyncio.run(_update_workflow_status(
        conversion_id, run_number, WfTaskCode.convert_model, status,
        finished=True, log_path=str(log_path), detail=failed_reason,
    ))

    if failed_reason:
        print(f'[{conversion_id}] coordinate conversion FAILED — {failed_reason}')
        return False
    print(f'[{conversion_id}] coordinate conversion ok -> {out_path.name}')
    return True


def _nmr_driver_script(
    *, is_nef: bool, src: str, cif: str, consist_log: str, deposit_log: str,
    out_str: str, next_src: str, entry_id: str,
) -> str:
    """Build the NmrDpUtility driver run inside the py-wwpdb_utils_nmr image.

    Two ops in sequence (per the README's single-file deposition example, adapted
    to the *2str* deposit used by bmrb_extract): consistency-check writes its JSON
    report, then the deposit op reuses it via report_file_path and emits NMR-STAR.
    The NMR-STAR result is addOutput('nmr-star_file_path') for NEF input, and
    setDestination() for NMR-STAR input. (nmr_cif_file_path output is omitted.)
    """
    op_check = 'nmr-nef-consistency-check' if is_nef else 'nmr-str-consistency-check'
    op_deposit = 'nmr-nef2str-deposit' if is_nef else 'nmr-str2str-deposit'
    common_inputs = (
        "u.addInput(name='coordinate_file_path', value=CIF, type='file')\n"
        "u.addInput(name='nonblk_anomalous_cs', value=True, type='param')\n"
        "u.addInput(name='nonblk_bad_nterm', value=True, type='param')\n"
        "u.addInput(name='resolve_conflict', value=True, type='param')\n"
        "u.addInput(name='check_mandatory_tag', value=True, type='param')\n"
    )
    if is_nef:
        deposit_out = (
            "u.setDestination(NEXT_SRC)\n"
            "u.addOutput(name='nmr-star_file_path', value=OUT_STR, type='file')\n"
        )
    else:
        deposit_out = "u.setDestination(OUT_STR)\n"
    return (
        "import sys\n"
        "from nmr.NmrDpUtility import NmrDpUtility\n"
        f"SRC = {src!r}\n"
        f"CIF = {cif!r}\n"
        f"CONS_LOG = {consist_log!r}\n"
        f"DEP_LOG = {deposit_log!r}\n"
        f"OUT_STR = {out_str!r}\n"
        f"NEXT_SRC = {next_src!r}\n"
        f"ENTRY_ID = {entry_id!r}\n"
        "# Step 1: consistency check\n"
        "u = NmrDpUtility()\n"
        "u.setSource(SRC)\n"
        f"{common_inputs}"
        "u.setLog(CONS_LOG)\n"
        "u.addInput(name='remediation', value=True, type='param')\n"
        "u.setVerbose(True)\n"
        f"u.op({op_check!r})\n"
        "# Step 2: deposit (convert to NMR-STAR) reusing the consistency report\n"
        "u.setSource(SRC)\n"
        f"{common_inputs}"
        "u.addInput(name='report_file_path', value=CONS_LOG, type='file')\n"
        "u.setLog(DEP_LOG)\n"
        "u.setVerbose(True)\n"
        f"{deposit_out}"
        "u.addOutput(name='entry_id', value=ENTRY_ID, type='param')\n"
        "u.addOutput(name='leave_intl_note', value=False, type='param')\n"
        f"u.op({op_deposit!r})\n"
    )


@task(name='nmr-data-conversion', retries=0)
def nmr_data_conversion(
    token: str,
    conversion_id: int,
    run_number: int,
    archive_base: str = ARCHIVE_BASE_PATH,
    workspace_base: str = WORKSPACE_BASE_PATH,
) -> bool:
    """Convert NMR data to NMR-STAR with py-wwpdb_utils_nmr (NmrDpUtility).

    Pilot scope: OneDep *combined* deposition — a single NMR unified data file
    (nm-uni-nef or nm-uni-str). Runs the py-wwpdb_utils_nmr image via
    `docker run python <driver>` against the model mmCIF produced by
    coordinate_conversion (output/C_<id>_model.cif). Writes
    output/C_<id>-nmr-data.str plus two JSON reports
    (C_<id>-nmr-data-consist.log, C_<id>-nmr-data-deposit.log) and
    drives the convert_nmr_data workflow row (processing -> completed/failed).

    Returns True on success (and when there is no nm-uni-* file — separated /
    bmrbdep NMR conversion is not implemented in this pilot), False on failure.
    """
    manifest = json.loads((Path(archive_base) / token / 'manifest.json').read_text())
    uni = next((f for f in manifest['files'] if f['file_type'].startswith('nm-uni-')), None)
    if uni is None:
        print(f'[{conversion_id}] No NMR unified data file — separated/bmrbdep NMR '
              f'conversion not implemented in this pilot; skipping')
        return True

    is_nef = uni['file_type'] == 'nm-uni-nef'
    src = ws.input_dir(conversion_id, run_number, workspace_base) / uni['original_name']
    out_dir = ws.output_dir(conversion_id, run_number, workspace_base)
    log_d = ws.log_dir(conversion_id, run_number, workspace_base)
    work_d = ws.work_dir(conversion_id, run_number, workspace_base)
    model_cif = out_dir / f'C_{conversion_id}_model.cif'
    consist_log = log_d / f'C_{conversion_id}-nmr-data-consist.log'
    deposit_log = log_d / f'C_{conversion_id}-nmr-data-deposit.log'
    out_str = out_dir / f'C_{conversion_id}-nmr-data.str'
    next_src = work_d / f'C_{conversion_id}-nmr-data-next.{"nef" if is_nef else "str"}'
    entry_id = f'C_{conversion_id}'

    if not model_cif.exists():
        reason = 'model mmCIF (C_<id>_model.cif) not found — coordinate conversion did not produce it'
        print(f'[{conversion_id}] NMR data conversion FAILED — {reason}')
        asyncio.run(_update_workflow_status(
            conversion_id, run_number, WfTaskCode.convert_nmr_data, WfStatusCode.failed,
            finished=True, log_path=str(deposit_log), detail=reason,
        ))
        return False

    asyncio.run(_update_workflow_status(
        conversion_id, run_number, WfTaskCode.convert_nmr_data, WfStatusCode.processing,
        started=True, log_path=str(deposit_log),
    ))

    work_d.mkdir(parents=True, exist_ok=True)
    driver = work_d / 'nmr_driver.py'
    driver.write_text(_nmr_driver_script(
        is_nef=is_nef, src=str(src), cif=str(model_cif),
        consist_log=str(consist_log), deposit_log=str(deposit_log),
        out_str=str(out_str), next_src=str(next_src), entry_id=entry_id,
    ))

    failed_reason = None
    try:
        cmd = [
            'docker', 'run', '--rm',
            '-u', f'{os.getuid()}:{os.getgid()}',
            '-v', f'{os.environ["WORKSPACE_VOL_DIR"]}:${os.environ["WORKSPACE_BASE_PATH"]}',
            UTILS_NMR_IMAGE,
            'python', str(driver),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if proc.returncode != 0:
            failed_reason = f'NmrDpUtility exit {proc.returncode}: {(proc.stderr or "").strip()[-400:]}'
    except subprocess.TimeoutExpired:
        failed_reason = 'NMR data conversion timed out'
    except Exception as exc:  # noqa: BLE001
        failed_reason = f'docker run error: {exc}'

    if failed_reason is None and (not out_str.exists() or out_str.stat().st_size == 0):
        failed_reason = 'no NMR-STAR output produced'

    status = WfStatusCode.failed if failed_reason else WfStatusCode.completed
    asyncio.run(_update_workflow_status(
        conversion_id, run_number, WfTaskCode.convert_nmr_data, status,
        finished=True, log_path=str(deposit_log), detail=failed_reason,
    ))

    if failed_reason:
        print(f'[{conversion_id}] NMR data conversion FAILED — {failed_reason}')
        return False
    print(f'[{conversion_id}] NMR data conversion ok -> {out_str.name}')
    return True


def _send_admin_email(subject: str, content: str) -> str:
    """Send a plain-text email to the site admin (best-effort; plain internal
    relay on port 25). Returns 'sent' or 'failed'."""
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


async def _notify_admin_failure(
    conversion_id: int,
    run_number: int,
    task: WfTaskCode,
    detail: str | None,
    log_path: str | None,
) -> None:
    """Email the admin and record a notification row when a workflow task ends
    with failure status. Best-effort: never raises."""
    try:
        subject = (
            f'[bmrb-extract:{SERVICE_HOST}] FAILED: {task.value} '
            f'(C_{conversion_id} run #{run_number})'
        )
        content = (
            f'A workflow task ended with failure status on {SERVICE_HOST}.\n\n'
            f'Task          : {task.value}\n'
            f'Conversion ID : C_{conversion_id}\n'
            f'Run number    : {run_number}\n'
            f'Detail        : {detail or "(none)"}\n'
            f'Log           : {log_path or "(none)"}\n'
            f'Time          : {datetime.now().isoformat(timespec="seconds")}\n'
        )
        delivery_status = _send_admin_email(subject, content)
        await _record_notification(conversion_id, subject, content, delivery_status)
        print(f'[{conversion_id}] failure notification for {task.value} (delivery={delivery_status})')
    except Exception as exc:  # noqa: BLE001
        print(f'[{conversion_id}] failure notification FAILED ({exc})')


@task(name='notify-new-conversion', retries=0)
def notify_new_conversion(
    token: str,
    conversion_id: int,
    run_number: int,
    archive_base: str = ARCHIVE_BASE_PATH,
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
    delivery_status = _send_admin_email(subject, content)
    print(f'[{conversion_id}] notify: admin email {delivery_status} ({SERVICE_ADMIN_EMAIL})')

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
    archive_base: str = ARCHIVE_BASE_PATH,
    workspace_base: str = WORKSPACE_BASE_PATH,
) -> dict:
    """Orchestrate NMR data conversion for one session run.

    Reads <archive_base>/<token>/manifest.json (written by POST /api/process), copies
    the active inputs into the per-run workspace, then runs the coordinate and
    NMR data conversion pipelines against those copies. The archive directory is
    a git repo; each POST /api/process call creates one commit tagged run-<N>.

    Args:
        token:          Session token (UUID string) — the archive subdirectory name.
        conversion_id:  Numeric conversion ID (e.g. C_8000001 → 8000001).
        run_number:     The processing run this invocation handles.
        archive_base:   Base directory of the archive volume (default ARCHIVE_BASE_PATH).
        workspace_base: Base directory of the workspace volume (default WORKSPACE_BASE_PATH).
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
        if coord_ok:
            nmr_ok = nmr_data_conversion(token, conversion_id, run_number, archive_base, workspace_base)
        else:
            # Model conversion failed: the NMR step needs C_<id>_model.cif, so
            # short-circuit and mark convert_nmr_data aborted.
            print(f'[{conversion_id}] Coordinate conversion failed — skipping NMR data conversion')
            asyncio.run(_update_workflow_status(
                conversion_id, run_number, WfTaskCode.convert_nmr_data, WfStatusCode.aborted,
                finished=True,
            ))
            nmr_ok = False
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
