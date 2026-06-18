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
        "u.addOutput(name='entry_id', value=ENTRY_ID, type='param')\n"
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
        f"ENTRY_ID = {entry_id!r}\n"
        f"WORK_DIR = {work_dir!r}\n"
        f"CACHE_DIR = {cache_dir!r}\n"
        "u = NmrDpUtility()\n"
        "# Step 1: merge chemical shifts + restraints/topology/peaks into NMR-STAR\n"
        "u.setWorkspace(WORK_DIR, CACHE_DIR)\n"
        "u.addInput(name='chem_shift_file_path_list', value=CS_LIST, type='file_dict_list')\n"
        "u.addInput(name='atypical_restraint_file_path_list', value=ATYPICAL, type='file_dict_list')\n"
        f"{restraint_input}"
        "u.addInput(name='coordinate_file_path', value=CIF, type='file')\n"
        "u.addInput(name='nonblk_anomalous_cs', value=True, type='param')\n"
        "u.addInput(name='nonblk_bad_nterm', value=True, type='param')\n"
        "u.addInput(name='resolve_conflict', value=True, type='param')\n"
        "u.addInput(name='check_mandatory_tag', value=False, type='param')\n"
        "u.addInput(name='remediation', value=True, type='param')\n"
        "u.setLog(MERGE_LOG)\n"
        "u.setDestination(MERGED_STR)\n"
        "u.setVerbose(True)\n"
        "u.op('nmr-cs-mr-merge')\n"
        "# Step 2: deposit the merged NMR-STAR (same as the combined str case)\n"
        "u.setWorkspace(WORK_DIR, CACHE_DIR)\n"
        "u.setSource(MERGED_STR)\n"
        "u.addInput(name='coordinate_file_path', value=CIF, type='file')\n"
        "u.addInput(name='report_file_path', value=MERGE_LOG, type='file')\n"
        "u.addInput(name='nonblk_anomalous_cs', value=True, type='param')\n"
        "u.addInput(name='nonblk_bad_nterm', value=True, type='param')\n"
        "u.addInput(name='resolve_conflict', value=True, type='param')\n"
        "u.addInput(name='check_mandatory_tag', value=True, type='param')\n"
        "u.setLog(DEP_LOG)\n"
        "u.setDestination(OUT_STR)\n"
        "u.addOutput(name='entry_id', value=ENTRY_ID, type='param')\n"
        "u.addOutput(name='leave_intl_note', value=False, type='param')\n"
        "u.setVerbose(True)\n"
        "u.op('nmr-str2str-deposit')\n"
    )


def _nmr_replace_cs_driver_script(
    *, src: str, cif: str, cs_list: list, report_log: str, out_str: str,
    work_dir: str, cache_dir: str,
) -> str:
    """Driver for OneDep repl_cs (replacing assigned chemical shifts): replace the
    chemical shifts in the OneDep-processed NMR-STAR unified data file (setSource)
    with the correct ones (chem_shift_file_path_list), against the coordinates,
    writing the report (setLog) and the resulting NMR-STAR (setDestination). A
    single op: nmr-str-replace-cs. Same input params as the OneDep case."""
    return (
        "from nmr.NmrDpUtility import NmrDpUtility\n"
        f"SRC = {src!r}\n"
        f"CIF = {cif!r}\n"
        f"CS_LIST = {cs_list!r}\n"
        f"REPORT_LOG = {report_log!r}\n"
        f"OUT_STR = {out_str!r}\n"
        f"WORK_DIR = {work_dir!r}\n"
        f"CACHE_DIR = {cache_dir!r}\n"
        "u = NmrDpUtility()\n"
        "u.setWorkspace(WORK_DIR, CACHE_DIR)\n"
        "u.setSource(SRC)\n"
        "u.addInput(name='chem_shift_file_path_list', value=CS_LIST, type='file_dict_list')\n"
        "u.addInput(name='coordinate_file_path', value=CIF, type='file')\n"
        "u.addInput(name='nonblk_anomalous_cs', value=True, type='param')\n"
        "u.addInput(name='nonblk_bad_nterm', value=True, type='param')\n"
        "u.addInput(name='resolve_conflict', value=True, type='param')\n"
        "u.addInput(name='check_mandatory_tag', value=True, type='param')\n"
        "u.setLog(REPORT_LOG)\n"
        "u.setDestination(OUT_STR)\n"
        "u.setVerbose(True)\n"
        "u.op('nmr-str-replace-cs')\n"
    )


def _nmr_bmrbdep_driver_script(
    *, cs_list: list, atypical_cs_list: list, atypical_restraint_list: list,
    bmrb_id: int, log: str, out_str: str, work_dir: str, cache_dir: str,
) -> str:
    """Driver for BMRBdep (BMRB-only) deposition: merge chemical shifts (NMR-STAR
    nm-uni-str/nm-shi and NEF nm-uni-nef in chem_shift_file_path_list, plus any
    nm-shi-* variants in atypical_chem_shift_file_path_list) and optional topology
    (nm-aux-* in atypical_restraint_file_path_list) into one NMR-STAR. No
    coordinates. Single op: nmr-cs-mr-merge with conversion_server=True."""
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
        f"BMRB_ID = {bmrb_id!r}\n"
        f"LOG = {log!r}\n"
        f"OUT_STR = {out_str!r}\n"
        f"WORK_DIR = {work_dir!r}\n"
        f"CACHE_DIR = {cache_dir!r}\n"
        "u = NmrDpUtility()\n"
        "u.setWorkspace(WORK_DIR, CACHE_DIR)\n"
        "u.addInput(name='chem_shift_file_path_list', value=CS_LIST, type='file_dict_list')\n"
        f"{atypical_cs_input}"
        f"{atypical_r_input}"
        "u.addInput(name='nonblk_anomalous_cs', value=True, type='param')\n"
        "u.addInput(name='nonblk_bad_nterm', value=True, type='param')\n"
        "u.addInput(name='resolve_conflict', value=True, type='param')\n"
        "u.addInput(name='check_mandatory_tag', value=False, type='param')\n"
        "u.addInput(name='remediation', value=True, type='param')\n"
        "u.addInput(name='conversion_server', value=True, type='param')\n"
        # conversion_server mode derives entry_id = C_<conversion_id> from this
        # (the conversion_id matches CNV_ID_PAT ^C_[1-9]\\d{6}$ as C_<id>).
        "u.addInput(name='bmrb_id', value=BMRB_ID, type='param')\n"
        "u.setLog(LOG)\n"
        "u.setDestination(OUT_STR)\n"
        "u.setVerbose(True)\n"
        "u.op('nmr-cs-mr-merge')\n"
    )


def _run_nmr_driver(
    conversion_id: int, run_number: int, workspace_base: str,
    work_d: Path, driver_text: str, out_str: Path, deposit_log: Path,
) -> bool:
    """Mark convert_nmr_data processing, run the NmrDpUtility driver in the
    py-wwpdb_utils_nmr image (docker run python <driver>), then mark
    completed/failed by the exit code and whether the NMR-STAR output exists.
    Shared by the combined and separated branches."""
    asyncio.run(_update_workflow_status(
        conversion_id, run_number, WfTaskCode.convert_nmr_data, WfStatusCode.processing,
        started=True, log_path=str(deposit_log),
    ))

    work_d.mkdir(parents=True, exist_ok=True)
    driver = work_d / 'nmr_driver.py'
    driver.write_text(driver_text)

    failed_reason = None
    try:
        cmd = [
            'docker', 'run', '--rm',
            '-u', f'{os.getuid()}:{os.getgid()}',
            '-v', f'{os.environ["WORKSPACE_VOL_DIR"]}:{WORKSPACE_BASE_PATH}',
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


@task(name='nmr-data-conversion', retries=0)
def nmr_data_conversion(
    token: str,
    conversion_id: int,
    run_number: int,
    archive_base: str = ARCHIVE_BASE_PATH,
    workspace_base: str = WORKSPACE_BASE_PATH,
) -> bool:
    """Convert NMR data to NMR-STAR with py-wwpdb_utils_nmr (NmrDpUtility).

    OneDep / Replacing-CS deposition, validated against the model mmCIF from
    coordinate_conversion (output/C_<id>_model.cif), producing
    output/C_<id>-nmr-data.str via `docker run python <driver>`:

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
    Returns True on success (and when there is nothing to convert), False on
    failure.
    """
    manifest = json.loads((Path(archive_base) / token / 'manifest.json').read_text())
    target = manifest.get('target_depsys')
    files = manifest['files']
    uni = next((f for f in files if f['file_type'].startswith('nm-uni-')), None)
    cs_files = [f for f in files if f['file_type'] == 'nm-shi']
    shi_variant_files = [f for f in files if f['file_type'].startswith('nm-shi-')]
    aux_files = [f for f in files if f['file_type'].startswith(('nm-res-', 'nm-aux-', 'nm-pea-'))]

    if target not in ('onedep', 'repl_cs', 'bmrbdep'):
        print(f'[{conversion_id}] NMR conversion for target={target} not implemented '
              f'in this pilot; skipping')
        return True
    if uni is None and not cs_files and not shi_variant_files and not aux_files:
        print(f'[{conversion_id}] No NMR data files — skipping NMR conversion')
        return True

    in_dir = ws.input_dir(conversion_id, run_number, workspace_base)
    out_dir = ws.output_dir(conversion_id, run_number, workspace_base)
    log_d = ws.log_dir(conversion_id, run_number, workspace_base)
    work_d = ws.work_dir(conversion_id, run_number, workspace_base)
    cache_d = ws.cache_dir(conversion_id, workspace_base)
    model_cif = out_dir / f'C_{conversion_id}_model.cif'
    deposit_log = log_d / f'C_{conversion_id}-nmr-data-str_deposit.json'
    out_str = out_dir / f'C_{conversion_id}-nmr-data.str'
    entry_id = f'C_{conversion_id}'

    # OneDep / repl_cs validate against coordinates; bmrbdep has none.
    if target in ('onedep', 'repl_cs') and not model_cif.exists():
        reason = 'model mmCIF (C_<id>_model.cif) not found — coordinate conversion did not produce it'
        print(f'[{conversion_id}] NMR data conversion FAILED — {reason}')
        asyncio.run(_update_workflow_status(
            conversion_id, run_number, WfTaskCode.convert_nmr_data, WfStatusCode.failed,
            finished=True, log_path=str(deposit_log), detail=reason,
        ))
        return False

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

    if target == 'bmrbdep':
        # BMRB-only: merge chemical shifts (+ optional topology) into NMR-STAR with
        # no coordinates. Single op: nmr-cs-mr-merge with conversion_server=True.
        nmr_log = log_d / f'C_{conversion_id}-nmr-data-bmrb_only.json'
        cs_list = []
        for f in files:
            ft = f['file_type']
            if ft in ('nm-uni-str', 'nm-shi'):
                cs_list.append({'file_name': str(in_dir / f['original_name']),
                                'file_type': 'nmr-star', 'original_file_name': f['original_name']})
            elif ft == 'nm-uni-nef':
                cs_list.append({'file_name': str(in_dir / f['original_name']),
                                'file_type': 'nef', 'original_file_name': f['original_name']})
        atypical_cs_list = _dict_list(shi_variant_files)  # nm-shi-* kept as-is
        atypical_restraint_list = _dict_list(
            [f for f in files if f['file_type'].startswith('nm-aux-')])  # topology, as-is
        if not cs_list and not atypical_cs_list:
            reason = 'bmrbdep requires at least one assigned chemical shift file (nm-uni-*, nm-shi, or nm-shi-*)'
            print(f'[{conversion_id}] NMR data conversion FAILED — {reason}')
            asyncio.run(_update_workflow_status(
                conversion_id, run_number, WfTaskCode.convert_nmr_data, WfStatusCode.failed,
                finished=True, log_path=str(nmr_log), detail=reason,
            ))
            return False
        driver_text = _nmr_bmrbdep_driver_script(
            cs_list=cs_list, atypical_cs_list=atypical_cs_list,
            atypical_restraint_list=atypical_restraint_list, bmrb_id=conversion_id,
            log=str(nmr_log), out_str=str(out_str),
            work_dir=str(work_d), cache_dir=str(cache_d),
        )
    elif target == 'repl_cs':
        # Replacing CS: replace the assigned chemical shifts in the OneDep-processed
        # NMR-STAR unified data file (nm-uni-str) with the correct ones (nm-shi),
        # against the coordinates. Single op: nmr-str-replace-cs.
        nmr_log = log_d / f'C_{conversion_id}-nmr-data-repl_cs.json'
        if uni is None or uni['file_type'] != 'nm-uni-str' or not cs_files:
            reason = ('repl_cs requires an NMR-STAR unified data file (nm-uni-str) '
                      'and at least one assigned chemical shift (nm-shi) file')
            print(f'[{conversion_id}] NMR data conversion FAILED — {reason}')
            asyncio.run(_update_workflow_status(
                conversion_id, run_number, WfTaskCode.convert_nmr_data, WfStatusCode.failed,
                finished=True, log_path=str(nmr_log), detail=reason,
            ))
            return False
        driver_text = _nmr_replace_cs_driver_script(
            src=str(in_dir / uni['original_name']), cif=str(model_cif),
            cs_list=_cs_dict_list(cs_files), report_log=str(nmr_log),
            out_str=str(out_str), work_dir=str(work_d), cache_dir=str(cache_d),
        )
    elif uni is not None:
        # OneDep combined: single NMR unified data file.
        nmr_log = deposit_log
        is_nef = uni['file_type'] == 'nm-uni-nef'
        consist_log = log_d / f'C_{conversion_id}-nmr-data-{"nef" if is_nef else "str"}_consist.json'
        next_src = work_d / f'C_{conversion_id}-nmr-data-next.{"nef" if is_nef else "str"}'
        driver_text = _nmr_driver_script(
            is_nef=is_nef, src=str(in_dir / uni['original_name']), cif=str(model_cif),
            consist_log=str(consist_log), deposit_log=str(deposit_log),
            out_str=str(out_str), next_src=str(next_src), entry_id=entry_id,
            work_dir=str(work_d), cache_dir=str(cache_d),
        )
    else:
        # OneDep separated: merge chemical shifts + restraints/topology/peaks, then deposit.
        nmr_log = deposit_log
        merge_log = log_d / f'C_{conversion_id}-cs_mr_merge.json'
        merged_str = work_d / f'C_{conversion_id}-cs-mr-merged.str'
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

    return _run_nmr_driver(
        conversion_id, run_number, workspace_base, work_d, driver_text, out_str, nmr_log,
    )


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
