"""Outgoing mail for the local deployment.

Upstream opens ``smtplib.SMTP(SMTP_SERVER, 25)`` directly at four call sites
(``app.py``, ``core/process_session.py``, ``core/cleanup.py``,
``core/exchange.py``), which assumes an unauthenticated relay on the plain SMTP
port. This module is the one place those call sites now go through, so a local
instance can point at an authenticated submission service — or at nothing at
all.

Everything is read from ``core.site_config``, which is rendered from the
environment on every container start, so a restart re-reads the settings.

``MAIL_BACKEND='log'`` short-circuits delivery and prints the whole message to
stdout instead. That is how you get a magic-link login URL out of an instance
with no mail server::

    docker compose logs -f backend
"""

import smtplib
import sys

from core import site_config as _cfg


def _cfg_value(name, default):
    return getattr(_cfg, name, default)


def send_message(msg, timeout=None):
    """Deliver an :class:`email.message.EmailMessage`.

    Raises whatever ``smtplib`` raises; every caller already wraps this in a
    try/except that records a 'failed' delivery status.
    """
    sender = _cfg_value('SMTP_FROM', '') or _cfg_value('SERVICE_ADMIN_EMAIL', '')
    if sender:
        del msg['From']
        msg['From'] = sender

    if _cfg_value('MAIL_BACKEND', 'smtp') == 'log':
        print(
            '=== MAIL_BACKEND=log — message NOT sent ===\n'
            f'{msg}\n'
            '=== end of message ===',
            file=sys.stdout,
            flush=True,
        )
        return

    host = _cfg_value('SMTP_SERVER', '')
    port = int(_cfg_value('SMTP_PORT', 25))
    if timeout is None:
        timeout = int(_cfg_value('SMTP_TIMEOUT', 30))

    connect = smtplib.SMTP_SSL if _cfg_value('SMTP_SSL', False) else smtplib.SMTP
    with connect(host, port, timeout=timeout) as smtp:
        if _cfg_value('SMTP_STARTTLS', False):
            smtp.starttls()
        user = _cfg_value('SMTP_USER', '')
        if user:
            smtp.login(user, _cfg_value('SMTP_PASSWORD', ''))
        smtp.send_message(msg)
