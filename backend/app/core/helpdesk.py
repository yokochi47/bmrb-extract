"""Help-desk attachment rules, shared by the Flask API (features/auth.py) and the
cleanup-sessions flow (prefect/flows/shared/core is a symlink to this package).

Storage: <WORKSPACE_BASE_PATH>/.helpdesk/<conversion_id>/<comm_ordinal>/<n>_<name>.
This is deliberately outside <ARCHIVE_BASE_PATH>/<token>/ (the git archive) and
outside <WORKSPACE_BASE_PATH>/<conversion_id>/ (conversion runs, and the
exchange-sessions rsync), so attachments never reach a conversion or the peer
site. They are served only by GET /api/help/attachment, which requires a login
session; mail mentions them by name and size only.
"""

import hashlib
import os
import shutil
from datetime import datetime, timedelta

import core.site_config as cfg
from core.filenames import UnsafeFileName, stored_basename, validate_upload_name

HELP_ATTACH_MAX_FILES = 3
HELP_ATTACH_MAX_BYTES = 10 * 1024 * 1024          # per file
# Early reject for the whole multipart body: all files at the limit plus form fields.
HELP_ATTACH_MAX_REQUEST = HELP_ATTACH_MAX_FILES * HELP_ATTACH_MAX_BYTES + 256 * 1024

# Read defensively so the backend and the flows still import before config.sh
# has re-rendered site_config.py with the new variable.
HELP_ATTACHMENT_VALIDITY_IN_DAYS = int(getattr(cfg, 'HELP_ATTACHMENT_VALIDITY_IN_DAYS', 30) or 30)

# Lower-case suffixes; matched against the end of the name, so compound ones
# ('.tar.gz', '.nef.gz') are accepted via their last suffix.
ALLOWED_SUFFIXES = (
    '.txt', '.log', '.csv', '.json', '.pdf', '.png', '.jpg', '.jpeg',
    '.str', '.nef', '.star', '.cif', '.mmcif', '.pdb', '.mr', '.tbl',
    '.zip', '.gz', '.tgz', '.tar',
)

_HELPDESK_DIRNAME = '.helpdesk'


def helpdesk_root(workspace_base: str | None = None) -> str:
    return os.path.join(workspace_base or cfg.WORKSPACE_BASE_PATH, _HELPDESK_DIRNAME)


def helpdesk_conversion_dir(conversion_id: int, workspace_base: str | None = None) -> str:
    """All attachments of one conversion's help-desk thread."""
    return os.path.join(helpdesk_root(workspace_base), str(int(conversion_id)))


def help_attachment_dir(conversion_id: int, comm_ordinal: int, workspace_base: str | None = None) -> str:
    """The attachments of one help-desk message."""
    return os.path.join(helpdesk_conversion_dir(conversion_id, workspace_base), str(int(comm_ordinal)))


def validate_attachment_name(name: str | None) -> str:
    """Return the name if it is a safe single path component with an allowed
    suffix; raise UnsafeFileName otherwise."""
    name = validate_upload_name(name)
    if not name.lower().endswith(ALLOWED_SUFFIXES):
        raise UnsafeFileName(
            'file type not allowed (allowed: ' + ', '.join(ALLOWED_SUFFIXES) + ')'
        )
    return name


class AttachmentTooLarge(ValueError):
    """Raised when one attachment exceeds HELP_ATTACH_MAX_BYTES."""


def save_attachment(stream, dest_dir: str, ordinal: int, original_name: str):
    """Stream one upload to dest_dir/<ordinal>_<name>, enforcing the size limit
    while writing. Returns (stored_path, size, sha256). A file over the limit is
    removed before AttachmentTooLarge is raised."""
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, stored_basename(ordinal, original_name))
    digest = hashlib.sha256()
    size = 0
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        with os.fdopen(fd, 'wb') as out:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > HELP_ATTACH_MAX_BYTES:
                    raise AttachmentTooLarge(
                        f'{original_name} is larger than {human_bytes(HELP_ATTACH_MAX_BYTES)}'
                    )
                digest.update(chunk)
                out.write(chunk)
    except BaseException:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    return path, size, digest.hexdigest()


def remove_dir(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


def attachment_expiry(now: datetime, session_expiry: datetime | None) -> datetime:
    """An attachment lives HELP_ATTACHMENT_VALIDITY_IN_DAYS, never past its session."""
    at = now + timedelta(days=HELP_ATTACHMENT_VALIDITY_IN_DAYS)
    if session_expiry is not None and session_expiry < at:
        at = session_expiry
    return at


def human_bytes(n) -> str:
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{int(n)} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} GB'


def format_attachment_notice(attachments, service_host: str) -> str:
    """Mail text announcing attachments by name and size only — never the file
    and never a download URL, just the help-desk page.

    `attachments` is an iterable of (original_name, file_size, expires_at)."""
    items = list(attachments)
    if not items:
        return ''
    until = min(a[2] for a in items)
    lines = [
        '',
        f'Attachments (available on the help desk page only, until {until:%Y-%m-%d}):',
    ]
    lines += [f'  - {name} ({human_bytes(size)})' for name, size, _ in items]
    lines.append(f'https://{service_host}/help')
    return '\n'.join(lines)
