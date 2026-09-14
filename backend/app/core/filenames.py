"""Validation and naming rules for user-supplied upload file names.

Shared by the Flask API and the Prefect flows (prefect/flows/shared/core is a
symlink to this package), so both ends agree on what a stored file is called.

Why this exists: a browser-supplied multipart filename is attacker-controlled
and Werkzeug hands it over verbatim — separators, '..' and NUL bytes included.
Historically nothing rejected those. What kept them harmless was the unrelated
'<ordinal>_' prefix in the archive path: it always occupies the first path
component, so '<session>/1_../../etc/x' needed a real directory named '1_..'
and failed with ENOENT instead of escaping. That prefix exists for name-collision
avoidance (see the stored_path comment in postgres/init-service.sql.template),
not for containment, so the protection was accidental and would disappear the
moment someone reordered the f-string or dropped the prefix — as the workspace
copy in prefect/flows/core/process_session.py already does.

So: reject the dangerous shapes explicitly at ingress, and keep the prefixed
basename as the single documented way to name a file on disk.
"""

MAX_NAME_LENGTH = 200


class UnsafeFileName(ValueError):
    """Raised for an upload name that must never be used to build a path."""


def validate_upload_name(name: str | None) -> str:
    """Return `name` unchanged if it is safe as a single path component.

    Raises UnsafeFileName otherwise. The name is *not* rewritten: it is what the
    depositor sees in reports and in the UI, so a silent mangle would be worse
    than a clear rejection.
    """
    if name is None or not name.strip():
        raise UnsafeFileName('file name is empty')
    if '\x00' in name:
        raise UnsafeFileName('file name contains a NUL byte')
    if '/' in name or '\\' in name:
        raise UnsafeFileName('file name must not contain a path separator')
    if name in ('.', '..'):
        raise UnsafeFileName("file name must not be '.' or '..'")
    # Control characters survive a filesystem write but corrupt logs and reports.
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise UnsafeFileName('file name contains a control character')
    if len(name.encode()) > MAX_NAME_LENGTH:
        raise UnsafeFileName(
            f'file name is longer than {MAX_NAME_LENGTH} bytes'
        )
    return name


def is_safe_upload_name(name: str | None) -> bool:
    """Non-raising form of validate_upload_name()."""
    try:
        validate_upload_name(name)
    except UnsafeFileName:
        return False
    return True


def stored_basename(ordinal: int, original_name: str) -> str:
    """The on-disk basename for an upload, in the archive and in the workspace.

    Two uploads in one session may share an original_name (upload_file's primary
    key is (token, ordinal); nothing constrains the name), so the ordinal is what
    keeps them distinct. Both the archive write and the per-run workspace copy
    must use this, or the second file silently overwrites the first.
    """
    return f'{ordinal}_{original_name}'


def sanitize_upload_name(name: str | None) -> str:
    """Coerce `name` into a safe single path component.

    For callers that cannot reject their input the way the upload endpoint can —
    notably the cross-site exchange, which replicates rows straight out of the
    peer site's database and so bypasses validate_upload_name() entirely. Prefer
    validate_upload_name() wherever refusing is an option; a coerced name no
    longer matches what the depositor typed.
    """
    if not name:
        return 'unnamed'
    # Keep the last component only: '../x' and '/a/b/x' both become 'x'.
    cleaned = name.replace('\\', '/').split('/')[-1]
    cleaned = ''.join(
        ch for ch in cleaned if ord(ch) >= 32 and ord(ch) != 127
    ).strip()
    if cleaned in ('', '.', '..'):
        return 'unnamed'
    encoded = cleaned.encode()[:MAX_NAME_LENGTH]
    # A truncation may land mid-codepoint; drop the partial tail.
    return encoded.decode(errors='ignore') or 'unnamed'
