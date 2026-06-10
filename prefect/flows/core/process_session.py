"""
Prefect flow for NMR data conversion of one session.

Deploy with:
  prefect deploy prefect/flows/core/process_session.py:process_session \
      --name default --pool local-pool

Then trigger via the Prefect API or directly:
  prefect run deployment process-session/default \
      --param token=<uuid> --param conversion_id=<int>
"""

import json
from pathlib import Path

from prefect import flow, task


@task(name='coordinate-conversion', retries=1)
def coordinate_conversion(token: str, conversion_id: int, archive_base: str = '/archive') -> bool:
    """Convert coordinate file using the maxit-ccd Docker Swarm service.

    Reads the coordinate file path from manifest.json and calls the
    maxit-ccd HTTP endpoint. Output is written to /archive/<token>/output/.

    TODO: implement HTTP call to maxit-ccd service.
    """
    session_dir = Path(archive_base) / token
    manifest = json.loads((session_dir / 'manifest.json').read_text())

    coord_files = [f for f in manifest['files'] if f['file_type'].startswith('co-')]
    if not coord_files:
        print(f'[{conversion_id}] No coordinate file found — skipping coordinate conversion')
        return True

    print(f'[{conversion_id}] TODO: call maxit-ccd for {[f["original_name"] for f in coord_files]}')
    return True


@task(name='nmr-data-conversion', retries=1)
def nmr_data_conversion(token: str, conversion_id: int, archive_base: str = '/archive') -> bool:
    """Convert NMR data files using the py-wwpdb_utils_nmr Docker Swarm service.

    Reads NMR file paths from manifest.json and calls the py-wwpdb_utils_nmr
    HTTP endpoint. Output is written to /archive/<token>/output/.

    TODO: implement HTTP call to py-wwpdb_utils_nmr service.
    """
    session_dir = Path(archive_base) / token
    manifest = json.loads((session_dir / 'manifest.json').read_text())

    nmr_files = [f for f in manifest['files'] if not f['file_type'].startswith('co-')]
    if not nmr_files:
        print(f'[{conversion_id}] No NMR data files found — skipping NMR conversion')
        return True

    print(f'[{conversion_id}] TODO: call py-wwpdb_utils_nmr for {[f["original_name"] for f in nmr_files]}')
    return True


@flow(name='process-session')
def process_session(token: str, conversion_id: int, archive_base: str = '/archive') -> dict:
    """Orchestrate NMR data conversion for one session.

    Reads /archive/<token>/manifest.json (written by POST /api/process) and
    runs the coordinate and NMR data conversion pipelines in sequence.
    The archive directory is a git repo; each POST /api/process call creates
    one commit, so git log shows the full history of processing runs.

    Args:
        token:          Session token (UUID string) — also the archive subdirectory name.
        conversion_id:  Numeric conversion ID (e.g. C_8000001 → 8000001).
        archive_base:   Base directory of the archive volume (default /archive).
    """
    session_dir = Path(archive_base) / token
    manifest = json.loads((session_dir / 'manifest.json').read_text())

    print(
        f'[{conversion_id}] Starting run #{manifest["run_number"]} '
        f'({len(manifest["files"])} selected files, target={manifest["target_depsys"]})'
    )

    coord_ok = coordinate_conversion(token, conversion_id, archive_base)
    nmr_ok = nmr_data_conversion(token, conversion_id, archive_base)

    success = coord_ok and nmr_ok
    print(f'[{conversion_id}] Run #{manifest["run_number"]} complete — success={success}')

    # TODO: update session status in DB (completed / failed) and insert output_file rows
    return {'success': success, 'run_number': manifest['run_number']}
