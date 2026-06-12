"""
Conversion workspace path scheme, keyed by conversion_id / run_number.

Layout (flat by conversion_id), kept separate from the git-managed upload
archive so conversions never contaminate it:

    <base>/<conversion_id>/<run_number>/
        input/    copies of the active (selected) upload files
        output/   converted output files
        work/     intermediate scratch (pickles, working dirs)
        log/      per-task log files

This convention is mirrored on the backend side (see backend/app/app.py).
Keep the two in sync.
"""

import shutil
from pathlib import Path

WORKSPACE_BASE_PATH = '/workspace'

SUBDIRS = ('input', 'output', 'work', 'log')


def run_dir(conversion_id: int, run_number: int, base: str = WORKSPACE_BASE_PATH) -> Path:
    """Return the workspace directory for one conversion run."""
    return Path(base) / str(conversion_id) / str(run_number)


def input_dir(conversion_id: int, run_number: int, base: str = WORKSPACE_BASE_PATH) -> Path:
    return run_dir(conversion_id, run_number, base) / 'input'


def output_dir(conversion_id: int, run_number: int, base: str = WORKSPACE_BASE_PATH) -> Path:
    return run_dir(conversion_id, run_number, base) / 'output'


def work_dir(conversion_id: int, run_number: int, base: str = WORKSPACE_BASE_PATH) -> Path:
    return run_dir(conversion_id, run_number, base) / 'work'


def log_dir(conversion_id: int, run_number: int, base: str = WORKSPACE_BASE_PATH) -> Path:
    return run_dir(conversion_id, run_number, base) / 'log'


def ensure_run_dirs(conversion_id: int, run_number: int, base: str = WORKSPACE_BASE_PATH) -> Path:
    """Create the run directory and all subdirs (idempotent); return the run dir."""
    rd = run_dir(conversion_id, run_number, base)
    for sub in SUBDIRS:
        (rd / sub).mkdir(parents=True, exist_ok=True)
    return rd


def reset_scratch(conversion_id: int, run_number: int, base: str = WORKSPACE_BASE_PATH) -> None:
    """Clear input/ and work/ so a retried run starts clean. Leaves output/ and log/."""
    for sub in ('input', 'work'):
        d = run_dir(conversion_id, run_number, base) / sub
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)
