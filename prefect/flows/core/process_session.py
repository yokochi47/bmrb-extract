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

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from prefect import flow, task

# Make both the flow's sibling modules (workspace) and the shared service ORM +
# config (prefect/flows/shared/core -> backend/app/core, mounted read-only in the
# worker) importable before importing them. workspace itself imports from
# core.site_config, so the shared-core path must be on sys.path before workspace.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'shared'))

import workspace as ws  # noqa: E402
import asyncio  # noqa: E402
import smtplib  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402
from email.message import EmailMessage  # noqa: E402

from sqlalchemy import func, select, update  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from core.models import (  # noqa: E402
    Notification,
    OutputFile,
    Session,
    SessionStatusCode,
    UploadFile,
    Workflow,
    WfStatusCode,
    WfTaskCode,
)
from core.site_config import (  # noqa: E402
    MAXIT_CCD_IMAGE,
    MAXIT_MEMORY_LIMIT,
    PDF_REPORT_IMAGE,
    UTILS_NMR_IMAGE,
    SERVICE_ADMIN_EMAIL,
    SERVICE_DATABASE_URL,
    SERVICE_HELP_EMAIL,
    SERVICE_HOST,
    AUTH_SECRET,
    SMTP_SERVER,
    ARCHIVE_BASE_PATH,
    WORKSPACE_BASE_PATH,
    SUCCESS_VALIDITY_PERIOD_IN_DAYS,
    FAILURE_VALIDITY_PERIOD_IN_DAYS,
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
    report_status: str | None = None,
    report_summary: str | None = None,
) -> None:
    """Update one workflow row's status (matched by task code) for this run.

    Whenever a task is set to `failed`, an admin failure notification (email +
    notification table row) is sent automatically. Mark every task failure via
    this helper so the rule applies to all current and future flow tasks; pass
    `detail` for a human-readable failure reason in the notification.
    `report_status`/`report_summary` carry the NmrDpUtility report analysis.
    """
    values = {'status': status}
    if started:
        values['started_at'] = func.now()
    if finished:
        values['finished_at'] = func.now()
    if log_path is not None:
        values['log_path'] = log_path
    if report_status is not None:
        values['report_status'] = report_status
    if report_summary is not None:
        values['report_summary'] = report_summary
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


async def _nmr_report_status(conversion_id: int, run_number: int) -> str | None:
    """Return the convert_nmr_data report_status for this run (None if unset).

    Set by _run_nmr_driver via the NmrDpUtility report analysis; 'Error' means a
    blocking, user-critical issue. Used to mark the session failed even though
    the conversion task itself completed.
    """
    engine = create_async_engine(SERVICE_DATABASE_URL, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            result = await db.execute(
                select(Workflow.report_status).where(
                    Workflow.conversion_id == conversion_id,
                    Workflow.run_number == run_number,
                    Workflow.task == WfTaskCode.convert_nmr_data,
                )
            )
            return result.scalar_one_or_none()
    finally:
        await engine.dispose()


async def _update_session_status(token: str, status: SessionStatusCode) -> None:
    """Set the session's lifecycle status and finish time when a run ends, and
    refresh the token expiry from this run's outcome.

    Called once per run with `completed` (the pipeline ran to completion, even
    if the NMR report flags user-facing errors) or `failed` (a task failed, was
    aborted, or the NMR report is a blocker). Clearing `processing` also lets the
    user start another run. Per-task detail lives on the workflow rows; this is
    the session-level outcome.

    Token expiry is reset to now + the validity period for the outcome: the
    shorter FAILURE period when the run failed, the longer SUCCESS period
    otherwise — so a successful conversion's results stay available longer.
    """
    days = (FAILURE_VALIDITY_PERIOD_IN_DAYS if status == SessionStatusCode.failed
            else SUCCESS_VALIDITY_PERIOD_IN_DAYS)
    token_expiry = datetime.now() + timedelta(days=days)
    engine = create_async_engine(SERVICE_DATABASE_URL, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            await db.execute(
                update(Session)
                .where(Session.token == token)
                .values(status=status, finished_at=func.now(), token_expiry=token_expiry)
            )
            await db.commit()
    finally:
        await engine.dispose()


# Converted result files produced in the run's output/ dir, mapped to their
# output_file_type. The converted coordinate (pdbx) exists only for OneDep /
# repl_cs (bmrbdep has none); the NEF is optional (NEF-release step). Names
# mirror coordinate_conversion / nmr_data_conversion / _generate_nef_release.
_OUTPUT_FILE_SPECS = (
    ('C_{cid}_model.cif', 'pdbx'),
    ('C_{cid}_nmr-data.str', 'nmr-star'),
    ('C_{cid}_nmr-data.nef', 'nef'),
    ('C_{cid}_report.pdf', 'pdf_report'),
)

# Report files produced in the run's log/ dir: maxit-ccd's coordinate-check log
# (text_report) and every NmrDpUtility JSON report (json_report). The captured
# *.stdout.log progress logs are not harvested (surfaced live via /api/log).


def _file_digest(path: Path) -> tuple[str, int]:
    """Stream a file to compute (sha256 hex, size) without loading it whole."""
    h = hashlib.sha256()
    size = 0
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


async def _record_output_files(conversion_id: int, run_number: int, files: list) -> None:
    """Replace this run's output_file rows with the harvested set (idempotent for
    a re-run of the same run_number). Ordinals are assigned 1..N in list order;
    sha256 checksum + size are recorded. downloaded=False / downloaded_at=NULL
    until the user actually fetches the file (set by the future download path)."""
    engine = create_async_engine(SERVICE_DATABASE_URL, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            await db.execute(
                OutputFile.__table__.delete().where(
                    OutputFile.conversion_id == conversion_id,
                    OutputFile.run_number == run_number,
                )
            )
            for ordinal, (path, file_type) in enumerate(files, start=1):
                checksum, size = _file_digest(path)
                await db.execute(
                    OutputFile.__table__.insert().values(
                        conversion_id=conversion_id,
                        run_number=run_number,
                        ordinal=ordinal,
                        stored_path=str(path),
                        file_size=size,
                        checksum=checksum,
                        file_type=file_type,
                        downloaded=False,
                        downloaded_at=None,
                    )
                )
            await db.commit()
    finally:
        await engine.dispose()


def _harvest_output_files(conversion_id: int, run_number: int, workspace_base: str) -> list:
    """Record the conversion outputs into the output_file table: from output/ the
    converted coordinate (pdbx), the generated NMR-STAR, and the optional NEF;
    from log/ the maxit-ccd coordinate-check log (text_report) and every
    NmrDpUtility JSON report (json_report). Returns the harvested file names."""
    out_dir = ws.output_dir(conversion_id, run_number, workspace_base)
    log_d = ws.log_dir(conversion_id, run_number, workspace_base)
    found = []
    # Converted result files (fixed names).
    for name_tpl, file_type in _OUTPUT_FILE_SPECS:
        path = out_dir / name_tpl.format(cid=conversion_id)
        if path.exists() and path.stat().st_size > 0:
            found.append((path, file_type))
    # maxit-ccd coordinate-check log (text_report).
    maxit_log = log_d / f'C_{conversion_id}_model-check.log'
    if maxit_log.exists() and maxit_log.stat().st_size > 0:
        found.append((maxit_log, 'text_report'))
    # NmrDpUtility JSON reports (json_report), sorted for stable ordinals.
    for report in sorted(log_d.glob('*.json')):
        if report.stat().st_size > 0:
            found.append((report, 'json_report'))
    asyncio.run(_record_output_files(conversion_id, run_number, found))
    return [path.name for path, _ in found]


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
    out_str: str, next_src: str, entry_id: str, work_dir: str, cache_dir: str,
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
        "u.addInput(name='conversion_server', value=True, type='param')\n"
        f"u.addInput(name='secret_key', value={AUTH_SECRET!r}, type='param')\n"
        f"u.addInput(name='service_host', value={SERVICE_HOST!r}, type='param')\n"
        "u.addInput(name='dep_sys_name', value='onedep', type='param')\n"
        f"u.addOutput(name='entry_id', value={entry_id!r}, type='param')\n"
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
        f"WORK_DIR = {work_dir!r}\n"
        f"CACHE_DIR = {cache_dir!r}\n"
        "u = NmrDpUtility()\n"
        "# Step 1: consistency check\n"
        "u.setWorkspace(WORK_DIR, CACHE_DIR)\n"
        "u.setSource(SRC)\n"
        f"{common_inputs}"
        "u.setLog(CONS_LOG)\n"
        "u.addInput(name='remediation', value=True, type='param')\n"
        "u.setVerbose(True)\n"
        f"u.op({op_check!r})\n"
        "# Step 2: deposit (convert to NMR-STAR) reusing the consistency report\n"
        "u.setWorkspace(WORK_DIR, CACHE_DIR)\n"
        "u.setSource(SRC)\n"
        f"{common_inputs}"
        "u.addInput(name='report_file_path', value=CONS_LOG, type='file')\n"
        "u.setLog(DEP_LOG)\n"
        "u.setVerbose(True)\n"
        f"{deposit_out}"
        "u.addOutput(name='leave_intl_note', value=False, type='param')\n"
        f"u.op({op_deposit!r})\n"
    )


# NMR-STAR / STAR structural tokens, used to decide whether an 'nm-res-oth' file
# is actually NMR-STAR V3 (data block, saveframe open/close, loop open/close).
_NMR_STAR_TOKEN_RE = re.compile(r'\s*(?:data_\S+|save_\S+|save_|loop_|stop_)\s*')


def _looks_like_nmr_star(path: Path) -> bool:
    """True if any line of the file matches an NMR-STAR structural token."""
    try:
        with open(path, 'r', errors='ignore') as fh:
            for line in fh:
                if _NMR_STAR_TOKEN_RE.match(line):
                    return True
    except OSError:
        pass
    return False


def _nmr_merge_driver_script(
    *, cif: str, cs_list: list, atypical_list: list, restraint_list: list,
    merge_log: str, merged_str: str, deposit_log: str, out_str: str, entry_id: str,
    work_dir: str, cache_dir: str,
) -> str:
    """Driver for OneDep conventional *separated* deposition: nmr-cs-mr-merge
    (merge chemical shifts + restraints/topology/peak lists against the
    coordinates into one NMR-STAR) then nmr-str2str-deposit on the merged file
    (same settings as the combined str case). The merge writes its JSON log
    (setLog) and the merged NMR-STAR (setDestination); the deposit consumes both.
    """
    common_inputs = (
        "u.addInput(name='coordinate_file_path', value=CIF, type='file')\n"
        "u.addInput(name='nonblk_anomalous_cs', value=True, type='param')\n"
        "u.addInput(name='nonblk_bad_nterm', value=True, type='param')\n"
        "u.addInput(name='resolve_conflict', value=True, type='param')\n"
        "u.addInput(name='check_mandatory_tag', value=True, type='param')\n"
        "u.addInput(name='conversion_server', value=True, type='param')\n"
        f"u.addInput(name='secret_key', value={AUTH_SECRET!r}, type='param')\n"
        f"u.addInput(name='service_host', value={SERVICE_HOST!r}, type='param')\n"
        "u.addInput(name='dep_sys_name', value='onedep', type='param')\n"
        f"u.addOutput(name='entry_id', value={entry_id!r}, type='param')\n"
    )
    restraint_input = (
        "u.addInput(name='restraint_file_path_list', value=RESTRAINT, type='file_dict_list')\n"
        if restraint_list else ""
    )
    return (
        "from nmr.NmrDpUtility import NmrDpUtility\n"
        f"CIF = {cif!r}\n"
        f"CS_LIST = {cs_list!r}\n"
        f"ATYPICAL = {atypical_list!r}\n"
        f"RESTRAINT = {restraint_list!r}\n"
        f"MERGE_LOG = {merge_log!r}\n"
        f"MERGED_STR = {merged_str!r}\n"
        f"DEP_LOG = {deposit_log!r}\n"
        f"OUT_STR = {out_str!r}\n"
        f"WORK_DIR = {work_dir!r}\n"
        f"CACHE_DIR = {cache_dir!r}\n"
        "u = NmrDpUtility()\n"
        "# Step 1: merge chemical shifts + restraints/topology/peaks into NMR-STAR\n"
        "u.setWorkspace(WORK_DIR, CACHE_DIR)\n"
        "u.addInput(name='chem_shift_file_path_list', value=CS_LIST, type='file_dict_list')\n"
        "u.addInput(name='atypical_restraint_file_path_list', value=ATYPICAL, type='file_dict_list')\n"
        f"{restraint_input}"
        f"{common_inputs}"
        "u.addInput(name='remediation', value=True, type='param')\n"
        "u.setLog(MERGE_LOG)\n"
        "u.setDestination(MERGED_STR)\n"
        "u.setVerbose(True)\n"
        "u.op('nmr-cs-mr-merge')\n"
        "# Step 2: deposit the merged NMR-STAR (same as the combined str case)\n"
        "u.setWorkspace(WORK_DIR, CACHE_DIR)\n"
        "u.setSource(MERGED_STR)\n"
        f"{common_inputs}"
        "u.addInput(name='report_file_path', value=MERGE_LOG, type='file')\n"
        "u.setLog(DEP_LOG)\n"
        "u.setDestination(OUT_STR)\n"
        "u.addOutput(name='leave_intl_note', value=False, type='param')\n"
        "u.setVerbose(True)\n"
        "u.op('nmr-str2str-deposit')\n"
    )


def _nmr_replace_cs_driver_script(
    *, src: str, cif: str, cs_list: list, replace_log: str, consist_log: str,
    out_str: str, entry_id: str, work_dir: str, cache_dir: str,
) -> str:
    """Driver for OneDep repl_cs (replacing assigned chemical shifts): replace the
    chemical shifts in the OneDep-processed NMR-STAR unified data file (setSource)
    with the correct ones (chem_shift_file_path_list), against the coordinates,
    writing the report (setLog) and the resulting NMR-STAR (setDestination).
    2 step ops: nmr-str-replace-cs and nmr-str-consistency-check.
    Same input params as the OneDep case."""
    common_inputs = (
        "u.addInput(name='coordinate_file_path', value=CIF, type='file')\n"
        "u.addInput(name='nonblk_anomalous_cs', value=True, type='param')\n"
        "u.addInput(name='nonblk_bad_nterm', value=True, type='param')\n"
        "u.addInput(name='resolve_conflict', value=True, type='param')\n"
        "u.addInput(name='check_mandatory_tag', value=True, type='param')\n"
        "u.addInput(name='remediation', value=True, type='param')\n"
        "u.addInput(name='conversion_server', value=True, type='param')\n"
        f"u.addInput(name='secret_key', value={AUTH_SECRET!r}, type='param')\n"
        f"u.addInput(name='service_host', value={SERVICE_HOST!r}, type='param')\n"
        "u.addInput(name='dep_sys_name', value='repl_cs', type='param')\n"
        f"u.addOutput(name='entry_id', value={entry_id!r}, type='param')\n"
    )
    return (
        "from nmr.NmrDpUtility import NmrDpUtility\n"
        f"SRC = {src!r}\n"
        f"CIF = {cif!r}\n"
        f"CS_LIST = {cs_list!r}\n"
        f"REPL_LOG = {replace_log!r}\n"
        f"CONS_LOG = {consist_log!r}\n"
        f"OUT_STR = {out_str!r}\n"
        f"WORK_DIR = {work_dir!r}\n"
        f"CACHE_DIR = {cache_dir!r}\n"
        "u = NmrDpUtility()\n"
        "# Step 1: replace chemical shifts into NMR-STAR\n"
        "u.setWorkspace(WORK_DIR, CACHE_DIR)\n"
        "u.setSource(SRC)\n"
        f"{common_inputs}"
        "u.addInput(name='chem_shift_file_path_list', value=CS_LIST, type='file_dict_list')\n"
        "u.setLog(REPL_LOG)\n"
        "u.setDestination(OUT_STR)\n"
        "u.setVerbose(True)\n"
        "u.op('nmr-str-replace-cs')\n"
        "# Step 2: consistency check\n"
        "u.setWorkspace(WORK_DIR, CACHE_DIR)\n"
        "u.setSource(OUT_STR)\n"
        f"{common_inputs}"
        "u.setLog(CONS_LOG)\n"
        "u.setVerbose(True)\n"
        "u.op('nmr-str-consistency-check')\n"
    )


def _nmr_bmrbdep_driver_script(
    *, cs_list: list, atypical_cs_list: list, atypical_restraint_list: list,
    merge_log: str, consist_log: str, out_str: str, entry_id: str,
    work_dir: str, cache_dir: str,
) -> str:
    """Driver for BMRBdep (BMRB-only) deposition: merge chemical shifts (NMR-STAR
    nm-uni-str/nm-shi and NEF nm-uni-nef in chem_shift_file_path_list, plus any
    nm-shi-* variants in atypical_chem_shift_file_path_list) and optional topology
    (nm-aux-* in atypical_restraint_file_path_list) into one NMR-STAR. No
    coordinates.
    2 step ops: nmr-cs-mr-merge and nmr-str-consistency-check.
    Similar input params as repl_cs case (except for coordinates) with conversion_server=True."""
    common_inputs = (
        "u.addInput(name='nonblk_anomalous_cs', value=True, type='param')\n"
        "u.addInput(name='nonblk_bad_nterm', value=True, type='param')\n"
        "u.addInput(name='resolve_conflict', value=True, type='param')\n"
        "u.addInput(name='check_mandatory_tag', value=True, type='param')\n"
        "u.addInput(name='remediation', value=True, type='param')\n"
        "u.addInput(name='conversion_server', value=True, type='param')\n"
        f"u.addInput(name='secret_key', value={AUTH_SECRET!r}, type='param')\n"
        f"u.addInput(name='service_host', value={SERVICE_HOST!r}, type='param')\n"
        "u.addInput(name='dep_sys_name', value='bmrbdep', type='param')\n"
        f"u.addOutput(name='entry_id', value={entry_id!r}, type='param')\n"
    )
    atypical_cs_input = (
        "u.addInput(name='atypical_chem_shift_file_path_list', value=ATYPICAL_CS, type='file_dict_list')\n"
        if atypical_cs_list else ""
    )
    atypical_r_input = (
        "u.addInput(name='atypical_restraint_file_path_list', value=ATYPICAL_R, type='file_dict_list')\n"
        if atypical_restraint_list else ""
    )
    return (
        "from nmr.NmrDpUtility import NmrDpUtility\n"
        f"CS_LIST = {cs_list!r}\n"
        f"ATYPICAL_CS = {atypical_cs_list!r}\n"
        f"ATYPICAL_R = {atypical_restraint_list!r}\n"
        f"MERGE_LOG = {merge_log!r}\n"
        f"CONS_LOG = {consist_log!r}\n"
        f"OUT_STR = {out_str!r}\n"
        f"WORK_DIR = {work_dir!r}\n"
        f"CACHE_DIR = {cache_dir!r}\n"
        "u = NmrDpUtility()\n"
        "u.setWorkspace(WORK_DIR, CACHE_DIR)\n"
        "u.addInput(name='chem_shift_file_path_list', value=CS_LIST, type='file_dict_list')\n"
        f"{atypical_cs_input}"
        f"{atypical_r_input}"
        f"{common_inputs}"
        # conversion_server mode derives entry_id = C_<conversion_id> from this
        # (the conversion_id matches CNV_ID_PAT ^C_[1-9]\\d{6}$ as C_<id>).
        "u.setLog(MERGE_LOG)\n"
        "u.setDestination(OUT_STR)\n"
        "u.setVerbose(True)\n"
        "u.op('nmr-cs-mr-merge')\n"
        "# Step 2: consistency check\n"
        "u.setWorkspace(WORK_DIR, CACHE_DIR)\n"
        "u.setSource(OUT_STR)\n"
        f"{common_inputs}"
        "u.setLog(CONS_LOG)\n"
        "u.setVerbose(True)\n"
        "u.op('nmr-str-consistency-check')\n"
    )


def _nmr_nef_release_driver_script(
    *, src: str, cif: str, report_log: str, out_nef: str, entry_id: str,
    work_dir: str, cache_dir: str,
) -> str:
    """Driver for releasing a NEF file from a converted NMR-STAR (OneDep combined /
    repl_cs). Single op: nmr-str2nef-release. The converted NMR-STAR is the source
    and the coordinates / params mirror the OneDep deposit; the NEF and its report
    are addOutput targets."""
    return (
        "from nmr.NmrDpUtility import NmrDpUtility\n"
        f"SRC = {src!r}\n"
        f"CIF = {cif!r}\n"
        f"REPORT_LOG = {report_log!r}\n"
        f"OUT_NEF = {out_nef!r}\n"
        f"ENTRY_ID = {entry_id!r}\n"
        f"WORK_DIR = {work_dir!r}\n"
        f"CACHE_DIR = {cache_dir!r}\n"
        "u = NmrDpUtility()\n"
        "u.setSource(SRC)\n"
        "u.addInput(name='coordinate_file_path', value=CIF, type='file')\n"
        "u.addInput(name='nonblk_anomalous_cs', value=True, type='param')\n"
        "u.addInput(name='nonblk_bad_nterm', value=True, type='param')\n"
        "u.addInput(name='resolve_conflict', value=True, type='param')\n"
        "u.addInput(name='check_mandatory_tag', value=True, type='param')\n"
        "u.addOutput(name='nef_file_path', value=OUT_NEF, type='file')\n"
        "u.addOutput(name='report_file_path', value=REPORT_LOG, type='file')\n"
        "u.addOutput(name='entry_id', value=ENTRY_ID, type='param')\n"
        "u.setWorkspace(WORK_DIR, CACHE_DIR)\n"
        "u.setVerbose(True)\n"
        "u.op('nmr-str2nef-release')\n"
    )


# Warning types that NmrDpUtility may report but which are safe to ignore (not
# surfaced to the user). From the prototype error/warning message generator.
_IGNORABLE_WARNING_TYPES = (
    'atom_nomenclature_mismatch',
    'auth_atom_nomenclature_mismatch',
    'ccd_mismatch',
    'corrected_format_issue',
    'disordered_index',
    'enum_mismatch_ignorable',
    'skipped_saveframe_category',
    'skipped_loop_category',
)

# Error types that block a non-combined deposition (conventional separated,
# repl_cs, bmrbdep). OneDep combined treats ANY error as a blocker.
# (Key spelling verified against the py-wwpdb_utils_nmr source.)
_BLOCKING_ERROR_TYPES = (
    'format_issue',
    'coordinate_issue',
    'content_mismatch',
    'missing_mandatory_content',
    'sequence_mismatch',
    'atom_not_found',
    'hydrogen_not_instantiated',
)

# A converted NMR-STAR can be released as NEF only when every content_subtype it
# carries is NEF-representable. If the final report's first input source is
# 'nmr-data-str' and all its content_subtype keys fall in this set, we emit a NEF
# release file as an additional output (OneDep combined / repl_cs only).
_NEF_RELEASE_CONTENT_SUBTYPES = frozenset((
    'entry_info',
    'poly_seq',
    'entity',
    'chem_shift',
    'chem_shift_ref',
    'dist_restraint',
    'dihed_restraint',
    'rdc_restraint',
    'spectral_peak',
    'spectral_peak_alt',
))


def _analyze_report(report_path: Path, onedep_combined: bool, conversion_id: int):
    """Analyze an NmrDpUtility JSON report and return (report_status, report_summary).

    report_status is 'OK' | 'Warning' | 'Error' (Error = blocker: the user must
    fix the reported errors and re-upload). report_summary is an HTML error/warning
    summary for the frontend (None when there is nothing to surface). Best-effort:
    a missing/unparseable report returns (None, None) so analysis never breaks the
    run. Logic follows misc/error_warning_messge_generator.py.
    """
    try:
        report = json.loads(report_path.read_text())
    except Exception as exc:  # noqa: BLE001
        print(f'[{conversion_id}] report analysis skipped ({report_path.name}: {exc})')
        return None, None

    if report.get('information', {}).get('status') == 'OK':
        return 'OK', None

    parts = []
    blocker = False

    err = report.get('error')
    if err and err.get('total', 0) > 0:
        total = err['total']
        if onedep_combined:
            blocker = True
        else:
            blocker = any(err.get(t) for t in _BLOCKING_ERROR_TYPES)
        parts.append(f"Found total {total} {'errors' if blocker else 'potential errors'} in NMR data<br />")
        for etype, items in err.items():
            if etype == 'total' or items is None:
                continue
            if etype == 'internal_error':
                parts.append(
                    'Sorry for the inconvenience, please contact us via the "Help Desk" page '
                    f'or the email address {SERVICE_HELP_EMAIL}, making sure to include your '
                    f'conversion ID: C_{conversion_id}.<ul>'
                    + ''.join(f'<li>Internal error: {msg}</li>' for msg in items)
                    + '</ul>'
                )
            else:
                title = etype[0].upper() + etype[1:].replace('_', ' ')
                lis = ''.join(
                    f"<li>{title}: {msg['description']}</li>"
                    for msg in items if isinstance(msg, dict) and 'description' in msg
                )
                if lis:
                    parts.append(f'<ul>{lis}</ul>')

    warn = report.get('warning')
    if warn:
        total = warn.get('total', 0)
        for wtype in _IGNORABLE_WARNING_TYPES:
            if warn.get(wtype):
                total -= len(warn[wtype])
        if total > 0:
            parts.append(f"Found total {total} warnings in NMR data<br /><ul>")
            for wtype, items in warn.items():
                if wtype == 'total' or wtype in _IGNORABLE_WARNING_TYPES or items is None:
                    continue
                title = wtype[0].upper() + wtype[1:].replace('_', ' ')
                parts.append(f'<li>Total of {len(items)} {title} found.</li>')
            parts.append('</ul>')

    summary = ''.join(parts) or None
    report_status = 'Error' if blocker else ('Warning' if summary else 'OK')
    return report_status, summary


def _run_nmr_driver(
    conversion_id: int, run_number: int, workspace_base: str,
    work_d: Path, driver_text: str, out_str: Path, deposit_log: Path,
    report_path: Path, onedep_combined: bool,
) -> tuple[bool, str | None]:
    """Mark convert_nmr_data processing, run the NmrDpUtility driver in the
    py-wwpdb_utils_nmr image (docker run python <driver>), then mark
    completed/failed by the exit code and whether the NMR-STAR output exists.
    On success, analyze the first-task JSON report (report_path) and record the
    report_status/report_summary on the workflow row. Shared by all branches.
    Returns (success, report_status) — report_status is None on failure."""
    asyncio.run(_update_workflow_status(
        conversion_id, run_number, WfTaskCode.convert_nmr_data, WfStatusCode.processing,
        started=True, log_path=str(deposit_log),
    ))

    work_d.mkdir(parents=True, exist_ok=True)
    driver = work_d / 'nmr_driver.py'
    driver.write_text(driver_text)

    # Stream the driver's combined stdout+stderr to a tailable log so the
    # progress dialog can show NmrDpUtility output live during the long run.
    # `python -u` keeps the child unbuffered so lines land in the file as emitted.
    stdout_log = ws.log_dir(conversion_id, run_number, workspace_base) / f'C_{conversion_id}_nmr-data.stdout.log'
    failed_reason = None
    try:
        cmd = [
            'docker', 'run', '--rm',
            '-u', f'{os.getuid()}:{os.getgid()}',
            '-v', f'{os.environ["WORKSPACE_VOL_DIR"]}:{WORKSPACE_BASE_PATH}',
            UTILS_NMR_IMAGE,
            'python', '-u', str(driver),
        ]
        with open(stdout_log, 'w') as fh:
            proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, text=True, timeout=3600)
        if proc.returncode != 0:
            tail = stdout_log.read_text(errors='ignore')[-400:].strip() if stdout_log.exists() else ''
            failed_reason = f'NmrDpUtility exit {proc.returncode}: {tail}'
    except subprocess.TimeoutExpired:
        failed_reason = 'NMR data conversion timed out'
    except Exception as exc:  # noqa: BLE001
        failed_reason = f'docker run error: {exc}'

    if failed_reason is None and (not out_str.exists() or out_str.stat().st_size == 0):
        failed_reason = 'no NMR-STAR output produced'

    if failed_reason:
        asyncio.run(_update_workflow_status(
            conversion_id, run_number, WfTaskCode.convert_nmr_data, WfStatusCode.failed,
            finished=True, log_path=str(deposit_log), detail=failed_reason,
        ))
        print(f'[{conversion_id}] NMR data conversion FAILED — {failed_reason}')
        return False, None

    # Conversion ran: analyze the first-task report for user-facing errors/warnings.
    report_status, report_summary = _analyze_report(report_path, onedep_combined, conversion_id)
    asyncio.run(_update_workflow_status(
        conversion_id, run_number, WfTaskCode.convert_nmr_data, WfStatusCode.completed,
        finished=True, log_path=str(deposit_log),
        report_status=report_status, report_summary=report_summary,
    ))
    print(f'[{conversion_id}] NMR data conversion ok -> {out_str.name} '
          f'(report_status={report_status})')
    return True, report_status


def _nef_release_eligible(report_path: Path, conversion_id: int) -> bool:
    """True if the converted NMR-STAR can be released as NEF: the final report's
    first input source is 'nmr-data-str' and every content_subtype it carries is
    NEF-representable (_NEF_RELEASE_CONTENT_SUBTYPES). Best-effort — a missing or
    unexpected report returns False so NEF release is simply skipped."""
    try:
        report = json.loads(report_path.read_text())
    except Exception as exc:  # noqa: BLE001
        print(f'[{conversion_id}] NEF eligibility skipped ({report_path.name}: {exc})')
        return False
    sources = report.get('information', {}).get('input_sources')
    if not isinstance(sources, list) or not sources:
        return False
    first = sources[0]
    if not isinstance(first, dict) or first.get('content_type') != 'nmr-data-str':
        return False
    subtype = first.get('content_subtype')
    if not isinstance(subtype, dict) or not subtype:
        return False
    return all(key in _NEF_RELEASE_CONTENT_SUBTYPES for key in subtype)


def _generate_nef_release(
    conversion_id: int, run_number: int, workspace_base: str, *,
    src: Path, cif: Path, out_dir: Path, log_d: Path, work_d: Path, cache_d: Path,
    entry_id: str,
) -> Path | None:
    """Release a NEF file from the converted NMR-STAR via nmr-str2nef-release.
    Writes out_dir/C_<id>_nmr-data.nef and log/C_<id>_nmr-data-nef_release.json.
    Best-effort: a failure is logged and returns None (the NMR-STAR is the primary
    output, so this never affects the run's success)."""
    out_nef = out_dir / f'C_{conversion_id}_nmr-data.nef'
    nef_report = log_d / f'C_{conversion_id}_nmr-data-nef_release.json'
    driver_text = _nmr_nef_release_driver_script(
        src=str(src), cif=str(cif), report_log=str(nef_report), out_nef=str(out_nef),
        entry_id=entry_id, work_dir=str(work_d), cache_dir=str(cache_d),
    )
    work_d.mkdir(parents=True, exist_ok=True)
    driver = work_d / 'nef_release_driver.py'
    driver.write_text(driver_text)

    stdout_log = log_d / f'C_{conversion_id}_nmr-data-nef_release.stdout.log'
    try:
        cmd = [
            'docker', 'run', '--rm',
            '-u', f'{os.getuid()}:{os.getgid()}',
            '-v', f'{os.environ["WORKSPACE_VOL_DIR"]}:{WORKSPACE_BASE_PATH}',
            UTILS_NMR_IMAGE,
            'python', '-u', str(driver),
        ]
        with open(stdout_log, 'w') as fh:
            proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, text=True, timeout=3600)
        if proc.returncode != 0:
            tail = stdout_log.read_text(errors='ignore')[-400:].strip() if stdout_log.exists() else ''
            print(f'[{conversion_id}] NEF release FAILED (exit {proc.returncode}): {tail}')
            return None
    except Exception as exc:  # noqa: BLE001
        print(f'[{conversion_id}] NEF release docker error: {exc}')
        return None

    if not out_nef.exists() or out_nef.stat().st_size == 0:
        print(f'[{conversion_id}] NEF release produced no output file')
        return None
    print(f'[{conversion_id}] NEF release ok -> {out_nef.name}')
    return out_nef


@task(name='nmr-data-conversion', retries=0)
def nmr_data_conversion(
    token: str,
    conversion_id: int,
    run_number: int,
    archive_base: str = ARCHIVE_BASE_PATH,
    workspace_base: str = WORKSPACE_BASE_PATH,
) -> tuple[bool, bool]:
    """Convert NMR data to NMR-STAR with py-wwpdb_utils_nmr (NmrDpUtility).

    OneDep / Replacing-CS deposition, validated against the model mmCIF from
    coordinate_conversion (output/C_<id>_model.cif), producing
    output/C_<id>_nmr-data.str via `docker run python <driver>`:

    - OneDep combined: a single NMR unified data file (nm-uni-nef/str) ->
      nmr-(nef/str)-consistency-check then nmr-(nef/str)2str-deposit.
    - OneDep separated: chemical shifts (nm-shi) + restraints (nm-res-*), topology
      (nm-aux-*) and peak lists (nm-pea-*) -> nmr-cs-mr-merge (into one NMR-STAR)
      then nmr-str2str-deposit. An 'nm-res-oth' file goes to
      restraint_file_path_list (file_type 'nmr-star') when its syntax looks like
      NMR-STAR, else stays in atypical_restraint_file_path_list as 'nm-res-oth'.
    - repl_cs: replace the assigned chemical shifts in the OneDep-processed
      NMR-STAR unified data file (nm-uni-str, setSource) with the correct nm-shi
      files -> single op nmr-str-replace-cs.
    - bmrbdep (BMRB-only, no coordinates): merge chemical shifts (nm-uni-*,
      nm-shi, nm-shi-*) and optional topology (nm-aux-*) -> single op
      nmr-cs-mr-merge with conversion_server=True.

    Drives the convert_nmr_data workflow row (processing -> completed/failed).
    Returns (ok, attempt_nef): `ok` is True on success (and when there is nothing
    to convert), False on failure; `attempt_nef` is True when a deferred NEF
    release should run (the flow runs it after the session is marked completed, so
    it stays off the summary critical path).
    """
    manifest = json.loads((Path(archive_base) / token / 'manifest.json').read_text())
    target = manifest.get('target_depsys')
    files = manifest['files']
    uni = next((f for f in files if f['file_type'].startswith('nm-uni-')), None)
    cs_files = [f for f in files if f['file_type'] == 'nm-shi']
    cs_variant_files = [f for f in files
                        if f['file_type'].startswith('nm-shi-')
                        or f['file_type'].startswith('nm-csp-')]  # perturbed chemical shifts
    aux_files = [f for f in files if f['file_type'].startswith(('nm-res-', 'nm-aux-', 'nm-pea-'))]

    if target not in ('onedep', 'repl_cs', 'bmrbdep'):
        print(f'[{conversion_id}] NMR conversion for target={target} not implemented '
              f'in this pilot; skipping')
        return True, False
    if uni is None and not cs_files and not cs_variant_files and not aux_files:
        print(f'[{conversion_id}] No NMR data files — skipping NMR conversion')
        return True, False

    in_dir = ws.input_dir(conversion_id, run_number, workspace_base)
    out_dir = ws.output_dir(conversion_id, run_number, workspace_base)
    log_d = ws.log_dir(conversion_id, run_number, workspace_base)
    work_d = ws.work_dir(conversion_id, run_number, workspace_base)
    cache_d = ws.cache_dir(conversion_id, workspace_base)
    model_cif = out_dir / f'C_{conversion_id}_model.cif'
    deposit_log = log_d / f'C_{conversion_id}_nmr-data-str_deposit.json'
    out_str = out_dir / f'C_{conversion_id}_nmr-data.str'
    entry_id = f'C_{conversion_id}'

    # OneDep / repl_cs validate against coordinates; bmrbdep has none.
    if target in ('onedep', 'repl_cs') and not model_cif.exists():
        reason = 'model mmCIF (C_<id>_model.cif) not found — coordinate conversion did not produce it'
        print(f'[{conversion_id}] NMR data conversion FAILED — {reason}')
        asyncio.run(_update_workflow_status(
            conversion_id, run_number, WfTaskCode.convert_nmr_data, WfStatusCode.failed,
            finished=True, log_path=str(deposit_log), detail=reason,
        ))
        return False, False

    def _cs_dict_list(shift_files):
        return [
            {'file_name': str(in_dir / f['original_name']), 'file_type': 'nmr-star',
             'original_file_name': f['original_name']}
            for f in shift_files
        ]

    def _dict_list(file_list, *, file_type=None):
        return [
            {'file_name': str(in_dir / f['original_name']),
             'file_type': file_type or f['file_type'],
             'original_file_name': f['original_name']}
            for f in file_list
        ]

    # Both OneDep combined and repl_cs treat any report error as a blocker; other modes only the
    # selected error types. report_path is the FIRST-task report to analyze.
    onedep_combined = (target == 'onedep' and uni is not None) or target == 'repl_cs'

    if target == 'bmrbdep':
        # BMRB-only: merge chemical shifts (+ optional topology) into NMR-STAR with
        # no coordinates. 2 step ops: nmr-cs-mr-merge + nmr-str-consistency-check with conversion_server=True.
        merge_log = log_d / f'C_{conversion_id}_nmr-data-str_bmrb_only.json'
        nmr_log = log_d / f'C_{conversion_id}_nmr-data-str_consist.json'
        report_path = merge_log  # first task report
        cs_list = []
        for f in files:
            ft = f['file_type']
            if ft in ('nm-uni-str', 'nm-shi'):
                cs_list.append({'file_name': str(in_dir / f['original_name']),
                                'file_type': 'nmr-star', 'original_file_name': f['original_name']})
            elif ft == 'nm-uni-nef':
                cs_list.append({'file_name': str(in_dir / f['original_name']),
                                'file_type': 'nef', 'original_file_name': f['original_name']})
        atypical_cs_list = _dict_list(cs_variant_files)  # nm-shi-* and nm-csp-* kept as-is
        atypical_restraint_list = _dict_list(
            [f for f in files if f['file_type'].startswith('nm-aux-')])  # topology, as-is
        if not cs_list and not atypical_cs_list:
            reason = 'bmrbdep requires at least one assigned chemical shift file (nm-uni-*, nm-shi, or nm-shi-*)'
            print(f'[{conversion_id}] NMR data conversion FAILED — {reason}')
            asyncio.run(_update_workflow_status(
                conversion_id, run_number, WfTaskCode.convert_nmr_data, WfStatusCode.failed,
                finished=True, log_path=str(nmr_log), detail=reason,
            ))
            return False, False
        driver_text = _nmr_bmrbdep_driver_script(
            cs_list=cs_list, atypical_cs_list=atypical_cs_list,
            atypical_restraint_list=atypical_restraint_list,
            merge_log=str(merge_log), consist_log=str(nmr_log), out_str=str(out_str),
            entry_id=entry_id, work_dir=str(work_d), cache_dir=str(cache_d),
        )
    elif target == 'repl_cs':
        # Replacing CS: replace the assigned chemical shifts in the OneDep-processed
        # NMR-STAR unified data file (nm-uni-str) with the correct ones (nm-shi),
        # against the coordinates. 2 step ops: nmr-str-replace-cs + nmr-str-consistency-check.
        replace_log = log_d / f'C_{conversion_id}_nmr-data-str_repl_cs.json'
        nmr_log = log_d / f'C_{conversion_id}_nmr-data-str_consist.json'
        report_path = replace_log  # first task report
        if uni is None or uni['file_type'] != 'nm-uni-str' or not cs_files:
            reason = ('repl_cs requires an NMR-STAR unified data file (nm-uni-str) '
                      'and at least one assigned chemical shift (nm-shi) file')
            print(f'[{conversion_id}] NMR data conversion FAILED — {reason}')
            asyncio.run(_update_workflow_status(
                conversion_id, run_number, WfTaskCode.convert_nmr_data, WfStatusCode.failed,
                finished=True, log_path=str(nmr_log), detail=reason,
            ))
            return False, False
        driver_text = _nmr_replace_cs_driver_script(
            src=str(in_dir / uni['original_name']), cif=str(model_cif),
            cs_list=_cs_dict_list(cs_files), replace_log=str(replace_log),
            consist_log=str(nmr_log), out_str=str(out_str), entry_id=entry_id,
            work_dir=str(work_d), cache_dir=str(cache_d),
        )
    elif uni is not None:
        # OneDep combined: single NMR unified data file.
        nmr_log = deposit_log
        is_nef = uni['file_type'] == 'nm-uni-nef'
        consist_log = log_d / f'C_{conversion_id}_nmr-data-{"nef" if is_nef else "str"}_consist.json'
        report_path = consist_log  # first task (consistency-check) report
        next_src = work_d / f'C_{conversion_id}_nmr-data-next.{"nef" if is_nef else "str"}'
        driver_text = _nmr_driver_script(
            is_nef=is_nef, src=str(in_dir / uni['original_name']), cif=str(model_cif),
            consist_log=str(consist_log), deposit_log=str(deposit_log),
            out_str=str(out_str), next_src=str(next_src), entry_id=entry_id,
            work_dir=str(work_d), cache_dir=str(cache_d),
        )
    else:
        # OneDep separated: merge chemical shifts + restraints/topology/peaks, then deposit.
        nmr_log = deposit_log
        merge_log = log_d / f'C_{conversion_id}_nmr-data-str_merge.json'
        report_path = merge_log  # first task (cs-mr-merge) report
        merged_str = work_d / f'C_{conversion_id}_cs-mr-merged.str'
        atypical_list, restraint_list = [], []
        for f in aux_files:
            name = f['original_name']
            entry = {'file_name': str(in_dir / name), 'original_file_name': name}
            if f['file_type'] == 'nm-res-oth' and _looks_like_nmr_star(in_dir / name):
                restraint_list.append({**entry, 'file_type': 'nmr-star'})
            else:
                atypical_list.append({**entry, 'file_type': f['file_type']})
        driver_text = _nmr_merge_driver_script(
            cif=str(model_cif), cs_list=_cs_dict_list(cs_files), atypical_list=atypical_list,
            restraint_list=restraint_list, merge_log=str(merge_log),
            merged_str=str(merged_str), deposit_log=str(deposit_log),
            out_str=str(out_str), entry_id=entry_id,
            work_dir=str(work_d), cache_dir=str(cache_d),
        )

    ok, report_status = _run_nmr_driver(
        conversion_id, run_number, workspace_base, work_d, driver_text, out_str, nmr_log,
        report_path, onedep_combined,
    )

    # NEF release applies to OneDep (combined OR separated) and repl_cs — any run
    # that produces a coordinate + NMR-STAR deposition. When that NMR-STAR is clean
    # (non-blocking) and its content is fully NEF-representable, a NEF release file
    # is emitted as an additional output. (onedep_combined is intentionally NOT used
    # here — it is kept only for the report-blocker semantics above; NEF eligibility
    # no longer requires a unified input file.) The actual generation is DEFERRED to
    # the flow (after the session is marked completed) so it stays off the summary
    # critical path; here we only decide whether it applies. nmr_log is the *final*
    # report (str_deposit.json / repl_cs.json).
    nef_target = target in ('onedep', 'repl_cs')
    attempt_nef = bool(
        ok
        and report_status != 'Error'
        and nef_target
        and _nef_release_eligible(nmr_log, conversion_id)
    )
    if nef_target and ok and report_status != 'Error' and not attempt_nef:
        print(f'[{conversion_id}] NEF release skipped (content not fully NEF-representable)')

    return ok, attempt_nef


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


async def _start_nef_workflow(conversion_id: int, run_number: int, log_path: str) -> None:
    """Create (or reset) the nef_release workflow row = processing so the download
    page can surface a 'NEF still generating' state while the deferred NEF release
    runs. Inserts with the next ordinal, or resets an existing row on re-run.
    log_path is the NEF release report path (workflow.log_path is NOT NULL)."""
    engine = create_async_engine(SERVICE_DATABASE_URL, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            existing = (
                await db.execute(
                    select(Workflow).where(
                        Workflow.conversion_id == conversion_id,
                        Workflow.run_number == run_number,
                        Workflow.task == WfTaskCode.nef_release,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                max_ord = (
                    await db.execute(
                        select(func.max(Workflow.ordinal)).where(
                            Workflow.conversion_id == conversion_id,
                            Workflow.run_number == run_number,
                        )
                    )
                ).scalar_one_or_none() or 0
                await db.execute(
                    Workflow.__table__.insert().values(
                        conversion_id=conversion_id,
                        run_number=run_number,
                        ordinal=max_ord + 1,
                        task=WfTaskCode.nef_release,
                        status=WfStatusCode.processing,
                        started_at=func.now(),
                        log_path=log_path,
                    )
                )
            else:
                await db.execute(
                    update(Workflow)
                    .where(
                        Workflow.conversion_id == conversion_id,
                        Workflow.run_number == run_number,
                        Workflow.task == WfTaskCode.nef_release,
                    )
                    .values(status=WfStatusCode.processing, started_at=func.now(), finished_at=None)
                )
            await db.commit()
    finally:
        await engine.dispose()


def _run_nef_release(conversion_id: int, run_number: int, workspace_base: str) -> None:
    """Deferred NMR-STAR -> NEF release: runs AFTER the session is marked completed
    (off the summary critical path). Tracks a nef_release workflow row, generates
    the NEF, then re-harvests so the .nef output (and its report) are recorded.
    Best-effort — the run outcome is already fixed, so this never changes it."""
    out_dir = ws.output_dir(conversion_id, run_number, workspace_base)
    log_d = ws.log_dir(conversion_id, run_number, workspace_base)
    work_d = ws.work_dir(conversion_id, run_number, workspace_base)
    cache_d = ws.cache_dir(conversion_id, workspace_base)
    ws.ensure_run_dirs(conversion_id, run_number, workspace_base)  # deferred step needs work/
    nef_report = log_d / f'C_{conversion_id}_nmr-data-nef_release.json'

    asyncio.run(_start_nef_workflow(conversion_id, run_number, str(nef_report)))
    nef = _generate_nef_release(
        conversion_id, run_number, workspace_base,
        src=out_dir / f'C_{conversion_id}_nmr-data.str',
        cif=out_dir / f'C_{conversion_id}_model.cif',
        out_dir=out_dir, log_d=log_d, work_d=work_d, cache_d=cache_d,
        entry_id=f'C_{conversion_id}',
    )
    if nef is not None:
        # Re-harvest so the new .nef appears in output_file (full replace).
        _harvest_output_files(conversion_id, run_number, workspace_base)
    asyncio.run(_update_workflow_status(
        conversion_id, run_number, WfTaskCode.nef_release,
        WfStatusCode.completed if nef is not None else WfStatusCode.failed,
        finished=True, log_path=str(nef_report),
        detail=None if nef is not None else 'NEF release generation failed',
    ))


async def _start_pdf_workflow(conversion_id: int, run_number: int, log_path: str) -> None:
    """Create (or reset) the convert_pdf workflow row = processing so the download
    page can surface a 'PDF still generating' state while the deferred report
    build runs. Mirrors _start_nef_workflow. log_path is the build stdout log."""
    engine = create_async_engine(SERVICE_DATABASE_URL, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            existing = (
                await db.execute(
                    select(Workflow).where(
                        Workflow.conversion_id == conversion_id,
                        Workflow.run_number == run_number,
                        Workflow.task == WfTaskCode.convert_pdf,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                max_ord = (
                    await db.execute(
                        select(func.max(Workflow.ordinal)).where(
                            Workflow.conversion_id == conversion_id,
                            Workflow.run_number == run_number,
                        )
                    )
                ).scalar_one_or_none() or 0
                await db.execute(
                    Workflow.__table__.insert().values(
                        conversion_id=conversion_id,
                        run_number=run_number,
                        ordinal=max_ord + 1,
                        task=WfTaskCode.convert_pdf,
                        status=WfStatusCode.processing,
                        started_at=func.now(),
                        log_path=log_path,
                    )
                )
            else:
                await db.execute(
                    update(Workflow)
                    .where(
                        Workflow.conversion_id == conversion_id,
                        Workflow.run_number == run_number,
                        Workflow.task == WfTaskCode.convert_pdf,
                    )
                    .values(status=WfStatusCode.processing, started_at=func.now(), finished_at=None)
                )
            await db.commit()
    finally:
        await engine.dispose()


async def _pdf_provenance(token: str, conversion_id: int, run_number: int) -> tuple[str | None, dict]:
    """Read the convert_nmr_data report path + the provenance the PDF title page
    needs (target deposition system, participating input files) from the DB, so
    the PDF container stays database-free. Returns (report_json_path, provenance)."""
    engine = create_async_engine(SERVICE_DATABASE_URL, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            report_path = (
                await db.execute(
                    select(Workflow.log_path).where(
                        Workflow.conversion_id == conversion_id,
                        Workflow.run_number == run_number,
                        Workflow.task == WfTaskCode.convert_nmr_data,
                    )
                )
            ).scalar_one_or_none()
            depsys = (
                await db.execute(select(Session.target_depsys).where(Session.token == token))
            ).scalar_one_or_none()
            rows = (
                (
                    await db.execute(
                        select(UploadFile)
                        .where(UploadFile.token == token, UploadFile.selected.is_(True))
                        .order_by(UploadFile.ordinal.asc())
                    )
                )
                .scalars()
                .all()
            )
            input_files = [
                {
                    'original_name': r.original_name,
                    'file_size': r.file_size,
                    'file_type': r.file_type,
                    'source': r.source,
                    'upload_date': r.uploaded_at.strftime('%Y-%m-%d') if r.uploaded_at else '',
                }
                for r in rows
            ]
    finally:
        await engine.dispose()

    provenance = {
        'conversion_id': conversion_id,
        'run_number': run_number,
        'target_depsys': depsys,
        'input_files': input_files,
    }
    return report_path, provenance


def _run_pdf_report(token: str, conversion_id: int, run_number: int, workspace_base: str) -> None:
    """Deferred JSON -> PDF conversion report: runs AFTER the session is marked
    completed (off the summary critical path), gated by the caller on a
    non-blocking run. Tracks a convert_pdf workflow row, runs the PDF image, then
    re-harvests so the pdf_report output is recorded. Best-effort — the run
    outcome is already fixed, so this never changes it. Mirrors _run_nef_release."""
    out_dir = ws.output_dir(conversion_id, run_number, workspace_base)
    log_d = ws.log_dir(conversion_id, run_number, workspace_base)
    work_d = ws.work_dir(conversion_id, run_number, workspace_base)
    ws.ensure_run_dirs(conversion_id, run_number, workspace_base)  # deferred step needs work/

    stdout_log = log_d / f'C_{conversion_id}_report-pdf.log'
    asyncio.run(_start_pdf_workflow(conversion_id, run_number, str(stdout_log)))

    report_path, provenance = asyncio.run(_pdf_provenance(token, conversion_id, run_number))
    ok = False
    if not report_path or not Path(report_path).is_file():
        print(f'[{conversion_id}] PDF report: no NMR data report to render ({report_path})')
    else:
        prov_path = work_d / 'provenance.json'
        prov_path.write_text(json.dumps(provenance), encoding='utf-8')
        out_pdf = out_dir / f'C_{conversion_id}_report.pdf'
        pdf_work = work_d / 'pdf'
        try:
            cmd = [
                'docker', 'run', '--rm',
                '-u', f'{os.getuid()}:{os.getgid()}',
                '-v', f'{os.environ["WORKSPACE_VOL_DIR"]}:{WORKSPACE_BASE_PATH}',
                PDF_REPORT_IMAGE,
                '--report', str(report_path), '--out', str(out_pdf),
                '--provenance', str(prov_path), '--work-dir', str(pdf_work),
            ]
            with open(stdout_log, 'w') as fh:
                proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, text=True, timeout=1800)
            if proc.returncode != 0:
                tail = stdout_log.read_text(errors='ignore')[-400:].strip() if stdout_log.exists() else ''
                print(f'[{conversion_id}] PDF report FAILED (exit {proc.returncode}): {tail}')
            elif out_pdf.exists() and out_pdf.stat().st_size > 0:
                ok = True
                # Re-harvest so the new .pdf appears in output_file (full replace).
                _harvest_output_files(conversion_id, run_number, workspace_base)
        except Exception as exc:  # noqa: BLE001
            print(f'[{conversion_id}] PDF report docker error: {exc}')

    asyncio.run(_update_workflow_status(
        conversion_id, run_number, WfTaskCode.convert_pdf,
        WfStatusCode.completed if ok else WfStatusCode.failed,
        finished=True, log_path=str(stdout_log),
        detail=None if ok else 'PDF report generation failed',
    ))


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

    success = False
    attempt_nef = False
    try:
        issue_conversion(token, conversion_id, run_number, archive_base, workspace_base)
        # First run only: notify the admin that a new conversion was issued.
        if run_number == 1:
            notify_new_conversion(token, conversion_id, run_number, archive_base)
        coord_ok = coordinate_conversion(token, conversion_id, run_number, archive_base, workspace_base)
        if coord_ok:
            nmr_ok, attempt_nef = nmr_data_conversion(
                token, conversion_id, run_number, archive_base, workspace_base)
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

        print(f'[{conversion_id}] Run #{run_number} complete — success={success}')

        # Harvest the produced output files (converted coordinate, NMR-STAR) into
        # the output_file table (best-effort: independent of the run outcome, so
        # the user can still download whatever partial output exists). The optional
        # NEF is added by a second harvest inside the deferred NEF step below.
        try:
            harvested = _harvest_output_files(conversion_id, run_number, workspace_base)
            print(f'[{conversion_id}] harvested {len(harvested)} output file(s): {harvested}')
        except Exception as exc:  # noqa: BLE001
            print(f'[{conversion_id}] output harvest FAILED ({exc})')

        # A blocking NMR report (report_status='Error') flags critical, user-blocking
        # issues: treat it as a failed run even though the conversion task completed.
        # (The blocker detail lives on the convert_nmr_data workflow row and is also
        # surfaced to the user via /api/progress.)
        blocked = False
        if success:
            try:
                blocked = asyncio.run(_nmr_report_status(conversion_id, run_number)) == 'Error'
            except Exception as exc:  # noqa: BLE001
                print(f'[{conversion_id}] could not read NMR report status ({exc})')

        # Record the session lifecycle outcome for this run. This is what makes the
        # Upload summary reachable (/api/progress `done`), so it happens BEFORE the
        # deferred NEF release — the summary never waits on NEF.
        session_status = (
            SessionStatusCode.completed if (success and not blocked) else SessionStatusCode.failed
        )
        try:
            asyncio.run(_update_session_status(token, session_status))
            print(f'[{conversion_id}] session status -> {session_status.value} (blocked={blocked})')
        except Exception as exc:  # noqa: BLE001
            print(f'[{conversion_id}] session status update FAILED ({exc})')

        # Refresh the footer's software/resource versions from the images just used,
        # so a fix delivered via an upstream image is immediately observable after a
        # verification run (best-effort; also captured on a schedule).
        try:
            from versions import capture_versions
            capture_versions(workspace_base)
        except Exception as exc:  # noqa: BLE001
            print(f'[{conversion_id}] version capture FAILED ({exc})')

        # Deferred NMR-STAR -> NEF release: runs only now (after the session is
        # marked completed) so the summary is reachable without waiting for it.
        # Best-effort — never changes the already-recorded run outcome.
        if success and not blocked and attempt_nef:
            try:
                _run_nef_release(conversion_id, run_number, workspace_base)
            except Exception as exc:  # noqa: BLE001
                print(f'[{conversion_id}] NEF release FAILED ({exc})')

        # Deferred JSON -> PDF conversion report: runs on any non-blocking run
        # (report_status OK or Warning), like the NEF release. Best-effort — the
        # run outcome and the summary page are unaffected. The download page
        # blocks the final Zip until this completes (pdf_generating flag).
        if success and not blocked:
            try:
                _run_pdf_report(token, conversion_id, run_number, workspace_base)
            except Exception as exc:  # noqa: BLE001
                print(f'[{conversion_id}] PDF report FAILED ({exc})')
    finally:
        # work/ is pure scratch — drop it last (the deferred NEF step above uses
        # it). output/ and log/ are kept for the validity period (download).
        scratch = ws.work_dir(conversion_id, run_number, workspace_base)
        if scratch.exists():
            shutil.rmtree(scratch, ignore_errors=True)

    return {'success': success, 'run_number': run_number}
