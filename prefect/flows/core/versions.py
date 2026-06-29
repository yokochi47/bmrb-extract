"""Capture the live software/resource versions of the conversion images.

The maxit-ccd and py-wwpdb_utils_nmr images are updated in place to deliver
upstream bug fixes, so the footer must reflect the versions currently deployed
on the service (not a setup-time snapshot). This flow reads the version env vars
baked into each image with `docker inspect` (no container run; reflects the local
`:main` image that the conversion `docker run …:main` actually uses) and writes a
small versions.json into the shared workspace volume, which the backend serves at
GET /api/versions.

Runs in the prefect-worker (the only container with the Docker socket); the public
backend never gets Docker access.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from prefect import flow

# Make the shared service config (prefect/flows/shared/core -> backend/app/core,
# mounted read-only in the worker) importable, mirroring process_session.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'shared'))

from core.site_config import (  # noqa: E402
    MAXIT_CCD_IMAGE,
    UTILS_NMR_IMAGE,
    WORKSPACE_BASE_PATH,
)


def _image_env(image):
    """Runtime env of a Docker image as a {KEY: VALUE} dict, via `docker run --rm
    <image> env`. The version vars (MAXIT_VER, DIC_VER, CCD_REL, UTILS_NMR_VER,
    CS_STAT_REL, …) are exported by the image entrypoint at runtime (from its
    .ver_info), so they are NOT in the static image config — running `env` is the
    way to read them. Reflects the local `:main` image (what conversions use).
    Returns {} if the run fails (e.g. image absent)."""
    try:
        proc = subprocess.run(
            ['docker', 'run', '--rm', image, 'env'],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            print(f'docker run {image} env failed: {proc.stderr.strip()}')
            return {}
        env = {}
        for line in proc.stdout.splitlines():
            key, sep, value = line.partition('=')
            if sep:
                env[key] = value
        return env
    except Exception as exc:  # noqa: BLE001
        print(f'docker run {image} env error: {exc}')
        return {}


def capture_versions(workspace_base=WORKSPACE_BASE_PATH):
    """Read versions from the live conversion images and write versions.json into
    the shared workspace. Best-effort: if neither image can be inspected, the
    existing file is left untouched rather than overwritten with empties."""
    maxit = _image_env(MAXIT_CCD_IMAGE)
    nmr = _image_env(UTILS_NMR_IMAGE)
    if not maxit and not nmr:
        print('no image env captured; keeping existing versions.json')
        return None

    data = {
        'software': {
            'maxit': maxit.get('MAXIT_VER'),
            'utils_nmr': nmr.get('UTILS_NMR_VER'),
        },
        'resource': {
            'pdbx_dict': maxit.get('DIC_VER'),
            'ccd_co': maxit.get('CCD_REL'),
            'ccd_nmr': nmr.get('CCD_REL'),
            'cs_stat': nmr.get('CS_STAT_REL'),
        },
    }

    path = Path(workspace_base) / 'versions.json'
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)
    print(f'wrote {path}: {data}')
    return data


@flow(name='capture-versions')
def capture_versions_flow():
    """Scheduled (and on-demand) refresh of versions.json from the live images."""
    return capture_versions(WORKSPACE_BASE_PATH)
