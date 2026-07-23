"""Passwordless email login + annotator (admin) authentication.

Security model (see the design plan):
- Users log in with an emailed single-use, short-TTL **magic link**; an account is
  created on first login. No passwords.
- Annotators (email in SERVICE_ANNOT_EMAILS) get the 'annotator' role and MUST
  additionally pass **TOTP** (authenticator app) before their admin authority
  (access to any session) is granted (auth_session.totp_ok).
- The login session is **server-side** (auth_session row), addressed by an opaque
  high-entropy id in an httpOnly + SameSite cookie (Secure in production), so it is
  revocable (logout / expiry / account disable). A per-session **CSRF token** must
  accompany every state-changing request (custom header).
- The legacy anonymous capability-URL flow (?token=) is untouched; login is additive.

This module exposes the /api/auth/* blueprint plus helpers reused by app.py for
authorization (`current_auth`, `require_csrf`, `authorize_session`, `record_admin_access`).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta
from functools import wraps

import pyotp
import qrcode
import redis.asyncio as aioredis
from cryptography.fernet import Fernet, InvalidToken
from flask import Blueprint, jsonify, request
from io import BytesIO
from sqlalchemy import func, select, update

from core.models import (
    AdminAccessAudit,
    AppUser,
    AuthSession,
    Communication,
    LoginChallenge,
    Session,
)
from core.site_config import (
    AUTH_SECRET,
    SERVICE_ANNOT_EMAILS,
    SERVICE_HELP_EMAIL,
    SERVICE_HOST,
    SERVICE_LEVEL,
)

auth_bp = Blueprint('auth', __name__)

# --- tunables ---------------------------------------------------------------- #
LOGIN_LINK_TTL = timedelta(minutes=15)      # magic-link validity
SESSION_IDLE = timedelta(hours=8)           # inactivity timeout
SESSION_ABSOLUTE = timedelta(days=7)        # hard cap regardless of activity
COOKIE_NAME = 'bmrbx_auth'
CSRF_HEADER = 'X-CSRF-Token'
TOTP_ISSUER = 'bmrb_extract'
# Rate limits: (max, window seconds) keyed in Redis.
RL_REQUEST_LOGIN_IP = (20, 3600)
RL_REQUEST_LOGIN_EMAIL = (5, 3600)
RL_VERIFY_IP = (30, 3600)
RL_TOTP_SESSION = (8, 900)                  # TOTP attempts per login session

_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

# Annotator allowlist (normalized).
ANNOT_EMAILS = {e.strip().lower() for e in (SERVICE_ANNOT_EMAILS or '').split(',') if e.strip()}

# --- module state (set by init_auth) ---------------------------------------- #
_session_factory = None   # async_sessionmaker
_send_email = None        # app.py's _send_email(to, subject, content) -> status str
_redis = None


def init_auth(app, session_factory, send_email):
    """Wire the blueprint: DB session factory + app's mailer (injected to avoid a
    circular import) + a Redis client for rate limiting."""
    global _session_factory, _send_email, _redis
    _session_factory = session_factory
    _send_email = send_email
    _redis = aioredis.Redis(
        host=os.environ.get('AUTH_REDIS_HOST', 'redis'),
        port=int(os.environ.get('AUTH_REDIS_PORT', '6379')),
        db=int(os.environ.get('AUTH_REDIS_DB', '1')),
        decode_responses=True,
    )
    app.register_blueprint(auth_bp)


# --- crypto / helpers -------------------------------------------------------- #

def _fernet() -> Fernet:
    """Fernet keyed from AUTH_SECRET (for encrypting TOTP secrets at rest)."""
    key = base64.urlsafe_b64encode(hashlib.sha256(AUTH_SECRET.encode()).digest())
    return Fernet(key)


def _encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def _decrypt(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode()).decode()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _norm_email(email: str) -> str:
    return (email or '').strip().lower()


def _now() -> datetime:
    return datetime.now()  # naive, matches the TIMESTAMP columns used elsewhere


def _secure_cookie() -> bool:
    # Secure in production; relaxed in development so it works without HTTPS.
    return SERVICE_LEVEL == 'production'


async def _rate_ok(key: str, limit: int, window: int) -> bool:
    """Redis fixed-window counter. Fails OPEN if Redis is unavailable (single-use
    short-TTL tokens + TOTP remain the primary controls)."""
    if _redis is None:
        return True
    try:
        n = await _redis.incr(key)
        if n == 1:
            await _redis.expire(key, window)
        return n <= limit
    except Exception:  # noqa: BLE001 — availability over strictness
        return True


def _set_cookie(resp, sid: str):
    resp.set_cookie(
        COOKIE_NAME, sid, max_age=int(SESSION_ABSOLUTE.total_seconds()),
        httponly=True, secure=_secure_cookie(), samesite='Lax', path='/',
    )


def _clear_cookie(resp):
    resp.set_cookie(COOKIE_NAME, '', max_age=0, httponly=True,
                    secure=_secure_cookie(), samesite='Lax', path='/')


# --- shared auth resolution / CSRF (reused by app.py) ------------------------ #

async def current_auth(db):
    """Resolve the caller's login from the cookie. Returns (auth_session, app_user)
    or (None, None). Enforces revocation, absolute + idle expiry; refreshes
    last_seen_at. Does NOT enforce TOTP — callers check `totp_ok` for admin power."""
    sid = request.cookies.get(COOKIE_NAME)
    if not sid:
        return None, None
    row = (
        await db.execute(select(AuthSession).where(AuthSession.id == sid))
    ).scalar_one_or_none()
    if row is None or row.revoked:
        return None, None
    now = _now()
    if row.absolute_expiry < now or (row.last_seen_at and row.last_seen_at + SESSION_IDLE < now):
        return None, None
    user = (
        await db.execute(select(AppUser).where(AppUser.id == row.user_id))
    ).scalar_one_or_none()
    if user is None or user.disabled:
        return None, None
    await db.execute(update(AuthSession).where(AuthSession.id == sid).values(last_seen_at=now))
    await db.commit()
    return row, user


def csrf_valid(auth_session) -> bool:
    """Constant-time compare of the request's CSRF header to the session token."""
    if auth_session is None:
        return False
    sent = request.headers.get(CSRF_HEADER, '')
    return bool(sent) and hmac.compare_digest(sent, auth_session.csrf_token or '')


def is_admin(auth_session, user) -> bool:
    """Full annotator (admin) authority = annotator role AND TOTP satisfied."""
    return bool(user and user.role == 'annotator' and auth_session and auth_session.totp_ok)


async def record_admin_access(db, user, session_row, action: str):
    """Audit one annotator access to a session they do not own."""
    await db.execute(AdminAccessAudit.__table__.insert().values(
        annotator_id=user.id,
        session_token=session_row.token if session_row is not None else None,
        conversion_id=session_row.conversion_id if session_row is not None else None,
        action=action,
        client_ip=request.remote_addr,
    ))
    await db.commit()


async def session_by_token(db, token, action='access'):
    """Resolve a session for a token-scoped request. First match session.token
    (the normal capability — anonymous, owner, or anyone the token was shared
    with, unchanged). If that misses, and the caller is a logged-in annotator
    (TOTP-satisfied), match session.token_admin — the admin per-session handle —
    and audit it. Returns the Session row or None. Used by app.py's read
    endpoints to give annotators token_admin access without a shared secret."""
    row = (
        await db.execute(select(Session).where(Session.token == token))
    ).scalar_one_or_none()
    if row is not None:
        return row
    auth_session, user = await current_auth(db)
    if is_admin(auth_session, user):
        row = (
            await db.execute(select(Session).where(Session.token_admin == token))
        ).scalar_one_or_none()
        if row is not None:
            await record_admin_access(db, user, row, action)
            return row
    return None


async def authorize_session(db, session_row, auth_session, user, action='access'):
    """Authorization decision for a session-scoped request. Allow when:
      - the session is anonymous (user_id NULL) — legacy capability flow, or
      - the caller owns it, or
      - the caller is an admin annotator (audited).
    Returns True/False. (The caller must already have matched the session token.)"""
    if session_row is None:
        return False
    if session_row.user_id is None:
        return True
    if user is not None and str(session_row.user_id) == str(user.id):
        return True
    if is_admin(auth_session, user):
        await record_admin_access(db, user, session_row, action)
        return True
    return False


# --- endpoints --------------------------------------------------------------- #

@auth_bp.route('/api/auth/request_login', methods=['POST'])
async def request_login():
    """Email a single-use magic link. Always returns a generic 200 (no account
    enumeration). Rate-limited per IP and per email."""
    body = request.get_json(silent=True) or {}
    email = _norm_email(body.get('email'))
    # Optional session the requester wants adopted once they authenticate — carried
    # with the challenge so it survives the login round-trip (even a magic link
    # opened on another device). Only acted on at verify, for an unowned session.
    claim_token = (body.get('claim_token') or '').strip() or None
    generic = {'ok': True, 'message': 'If that address can sign in, a login link has been sent.'}

    if not _EMAIL_RE.match(email):
        return generic, 200  # never reveal validity
    ip = request.remote_addr or 'unknown'
    if not await _rate_ok(f'auth:rl:reqip:{ip}', *RL_REQUEST_LOGIN_IP):
        return generic, 200
    if not await _rate_ok(f'auth:rl:reqemail:{email}', *RL_REQUEST_LOGIN_EMAIL):
        return generic, 200

    token = secrets.token_urlsafe(32)
    async with _session_factory() as db:
        await db.execute(LoginChallenge.__table__.insert().values(
            email=email, token_hash=_hash_token(token), purpose='login',
            expires_at=_now() + LOGIN_LINK_TTL, claim_token=claim_token,
        ))
        await db.commit()

    link = f'https://{SERVICE_HOST}/login/verify?c={token}'
    subject = 'bmrb_extract sign-in link'
    content = (
        f'Use the link below to sign in to bmrb_extract. It is valid for '
        f'{int(LOGIN_LINK_TTL.total_seconds() // 60)} minutes and can be used once.\n\n'
        f'{link}\n\n'
        'If you did not request this, you can ignore this email.'
    )
    if _send_email is not None:
        _send_email(email, subject, content)
    return generic, 200


@auth_bp.route('/api/auth/verify', methods=['POST'])
async def verify():
    """Consume a magic-link token, create/find the user, and issue a login session
    cookie. Annotators still need TOTP before admin authority (totp_ok stays False)."""
    ip = request.remote_addr or 'unknown'
    if not await _rate_ok(f'auth:rl:verifyip:{ip}', *RL_VERIFY_IP):
        return {'error': 'too many attempts, please retry later'}, 429

    body = request.get_json(silent=True) or {}
    token = body.get('c') or request.args.get('c') or ''
    if not token:
        return {'error': 'invalid or expired link'}, 400
    token_hash = _hash_token(token)

    async with _session_factory() as db:
        now = _now()
        ch = (
            await db.execute(
                select(LoginChallenge).where(
                    LoginChallenge.token_hash == token_hash,
                    LoginChallenge.consumed_at.is_(None),
                    LoginChallenge.expires_at > now,
                )
            )
        ).scalar_one_or_none()
        if ch is None:
            return {'error': 'invalid or expired link'}, 400
        # Single-use: consume immediately.
        await db.execute(update(LoginChallenge).where(LoginChallenge.id == ch.id)
                         .values(consumed_at=now))

        email = _norm_email(ch.email)
        user = (
            await db.execute(select(AppUser).where(AppUser.email == email))
        ).scalar_one_or_none()
        role = 'annotator' if email in ANNOT_EMAILS else 'user'
        if user is None:
            await db.execute(AppUser.__table__.insert().values(email=email, role=role))
            user = (
                await db.execute(select(AppUser).where(AppUser.email == email))
            ).scalar_one()
        else:
            # Keep the role in sync with the current allowlist.
            if user.role != role:
                await db.execute(update(AppUser).where(AppUser.id == user.id).values(role=role))
                user.role = role
            if user.disabled:
                await db.commit()
                return {'error': 'account disabled'}, 403
        await db.execute(update(AppUser).where(AppUser.id == user.id).values(last_login_at=now))

        # Fresh login session (anti-fixation: brand-new id). Users are fully
        # authenticated immediately; annotators require the TOTP step next.
        sid = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        await db.execute(AuthSession.__table__.insert().values(
            id=sid, user_id=user.id, csrf_token=csrf,
            totp_ok=(role != 'annotator'),
            absolute_expiry=now + SESSION_ABSOLUTE,
            client_ip=request.remote_addr, user_agent=request.headers.get('User-Agent', ''),
        ))

        # Adopt the session the requester bound to this challenge (they held its
        # token when requesting the link). Survives the login round-trip / a link
        # opened on another device. Only an unowned, non-expired session is claimed.
        claimed_session = False
        if ch.claim_token:
            s = (
                await db.execute(select(Session).where(Session.token == ch.claim_token))
            ).scalar_one_or_none()
            if (s is not None and s.user_id is None
                    and s.status != 'expired' and s.token_expiry >= now):
                await db.execute(update(Session).where(Session.token == ch.claim_token)
                                 .values(user_id=user.id))
                claimed_session = True
        await db.commit()

        totp_required = role == 'annotator'
        payload = {
            'authenticated': True,
            'email': email,
            'role': role,
            'csrf_token': csrf,
            'totp_required': totp_required,
            'totp_enrolled': bool(user.totp_enrolled),
            'claimed_session': claimed_session,
        }
    resp = jsonify(payload)
    _set_cookie(resp, sid)
    return resp


@auth_bp.route('/api/auth/me', methods=['GET'])
async def me():
    """Current auth state for the frontend."""
    async with _session_factory() as db:
        auth_session, user = await current_auth(db)
        if user is None:
            return {'authenticated': False}, 200
        return {
            'authenticated': True,
            'email': user.email,
            'role': user.role,
            'csrf_token': auth_session.csrf_token,
            'totp_required': user.role == 'annotator' and not auth_session.totp_ok,
            'totp_enrolled': bool(user.totp_enrolled),
        }, 200


@auth_bp.route('/api/auth/totp/enroll', methods=['POST'])
async def totp_enroll():
    """Begin TOTP enrollment for an annotator who has not enrolled yet: generate a
    secret (stored encrypted, not yet active) and return the otpauth URI + QR PNG."""
    async with _session_factory() as db:
        auth_session, user = await current_auth(db)
        if user is None or user.role != 'annotator':
            return {'error': 'not authorized'}, 403
        if not csrf_valid(auth_session):
            return {'error': 'invalid CSRF token'}, 403
        if user.totp_enrolled:
            return {'error': 'already enrolled'}, 409
        secret = pyotp.random_base32()
        await db.execute(update(AppUser).where(AppUser.id == user.id)
                         .values(totp_secret=_encrypt(secret)))
        await db.commit()
        uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name=TOTP_ISSUER)
    img = qrcode.make(uri)
    buf = BytesIO()
    img.save(buf, format='PNG')
    qr_data_uri = 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()
    return {'otpauth_uri': uri, 'qr': qr_data_uri}, 200


@auth_bp.route('/api/auth/totp/verify', methods=['POST'])
async def totp_verify():
    """Verify a 6-digit TOTP code; on success mark the login session totp_ok (and
    finalize enrollment on first use). Rate-limited per session."""
    async with _session_factory() as db:
        auth_session, user = await current_auth(db)
        if user is None or user.role != 'annotator' or auth_session is None:
            return {'error': 'not authorized'}, 403
        if not csrf_valid(auth_session):
            return {'error': 'invalid CSRF token'}, 403
        if not await _rate_ok(f'auth:rl:totp:{auth_session.id}', *RL_TOTP_SESSION):
            return {'error': 'too many attempts, please retry later'}, 429
        if not user.totp_secret:
            return {'error': 'not enrolled'}, 409
        code = str((request.get_json(silent=True) or {}).get('code', '')).strip()
        try:
            secret = _decrypt(user.totp_secret)
        except InvalidToken:
            return {'error': 'server misconfiguration'}, 500
        if not code or not pyotp.TOTP(secret).verify(code, valid_window=1):
            return {'error': 'invalid code'}, 401
        if not user.totp_enrolled:
            await db.execute(update(AppUser).where(AppUser.id == user.id)
                             .values(totp_enrolled=True))
        await db.execute(update(AuthSession).where(AuthSession.id == auth_session.id)
                         .values(totp_ok=True))
        await db.commit()
    return {'ok': True, 'role': 'annotator'}, 200


@auth_bp.route('/api/auth/logout', methods=['POST'])
async def logout():
    """Revoke the current login session and clear the cookie."""
    sid = request.cookies.get(COOKIE_NAME)
    if sid:
        async with _session_factory() as db:
            await db.execute(update(AuthSession).where(AuthSession.id == sid).values(revoked=True))
            await db.commit()
    resp = jsonify({'ok': True})
    _clear_cookie(resp)
    return resp


@auth_bp.route('/api/auth/claim_session', methods=['POST'])
async def claim_session():
    """Link the caller's account to a session they started anonymously.

    The caller must be logged in and possess the session's own capability `token`
    (proving they are the legitimate anonymous holder). Only an unowned, non-expired
    session can be claimed; a session already owned by someone else is never
    reassigned. Idempotent when the caller already owns it.

    Matches the real `token` only (never `token_admin`): claiming is the anonymous
    holder adopting their own session, not audited admin access."""
    async with _session_factory() as db:
        auth_session, user = await current_auth(db)
        if user is None:
            return {'error': 'authentication required'}, 401
        if not csrf_valid(auth_session):
            return {'error': 'invalid CSRF token'}, 403
        token = (request.get_json(silent=True) or {}).get('token')
        if not token:
            return {'error': 'token is required'}, 400
        row = (
            await db.execute(select(Session).where(Session.token == token))
        ).scalar_one_or_none()
        if row is None:
            return {'error': 'session not found'}, 404
        if str(row.user_id or '') == str(user.id):
            return {'ok': True, 'already_owned': True}, 200
        if row.user_id is not None:
            return {'error': 'session already belongs to another account'}, 409
        if row.status == 'expired' or row.token_expiry < _now():
            return {'error': 'session has expired'}, 410
        await db.execute(update(Session).where(Session.token == token).values(user_id=user.id))
        await db.commit()
    return {'ok': True}, 200


def _session_view(row, *, admin=False):
    """Serialize a session for the listing. Own rows expose the token (to reopen);
    admin (scope=all) rows expose token_admin instead (the audited admin handle)."""
    view = {
        'conversion_id': row.conversion_id,
        'public_id': f'C_{row.conversion_id}' if row.conversion_id is not None else None,
        'status': row.status,
        'target_depsys': row.target_depsys,
        'created_at': row.created_at.isoformat() if row.created_at else None,
        'token_expiry': row.token_expiry.isoformat() if row.token_expiry else None,
        'approved': bool(row.approved),
        'downloaded': bool(row.downloaded),
    }
    view['token_admin' if admin else 'token'] = str(row.token_admin if admin else row.token)
    return view


@auth_bp.route('/api/sessions', methods=['GET'])
async def list_sessions():
    """List the caller's own sessions. An annotator may pass ?scope=all to list
    every session (audited) — each row carries token_admin for the audited admin
    open, not the user's token."""
    async with _session_factory() as db:
        auth_session, user = await current_auth(db)
        if user is None:
            return {'error': 'authentication required'}, 401
        scope_all = request.args.get('scope') == 'all' and is_admin(auth_session, user)
        if scope_all:
            rows = (
                await db.execute(select(Session).order_by(Session.created_at.desc()).limit(500))
            ).scalars().all()
            await record_admin_access(db, user, None, 'list_all_sessions')
            return {'sessions': [_session_view(r, admin=True) for r in rows], 'scope': 'all'}, 200
        rows = (
            await db.execute(
                select(Session).where(Session.user_id == user.id).order_by(Session.created_at.desc())
            )
        ).scalars().all()
        return {'sessions': [_session_view(r) for r in rows], 'scope': 'own'}, 200


async def _next_ordinal(db, conversion_id):
    n = (
        await db.execute(
            select(func.max(Communication.ordinal)).where(Communication.conversion_id == conversion_id)
        )
    ).scalar_one_or_none()
    return (n or 0) + 1


@auth_bp.route('/api/help/inquiry', methods=['POST'])
async def help_inquiry():
    """A logged-in user files a help-desk inquiry about one of their sessions
    (Terms #5). Logged as a communication row and emailed to the help address.
    Requires the session to have a conversion_id (i.e. it has been processed)."""
    async with _session_factory() as db:
        auth_session, user = await current_auth(db)
        if user is None:
            return {'error': 'authentication required'}, 401
        if not csrf_valid(auth_session):
            return {'error': 'invalid CSRF token'}, 403
        body = request.get_json(silent=True) or {}
        token = body.get('token')
        subject = (body.get('subject') or '').strip()
        content = (body.get('content') or '').strip()
        if not token or not subject or not content:
            return {'error': 'token, subject and content are required'}, 400
        row = (
            await db.execute(select(Session).where(Session.token == token))
        ).scalar_one_or_none()
        if row is None:
            return {'error': 'session not found'}, 404
        # The inquiry must be about the caller's own session.
        if row.user_id is None or str(row.user_id) != str(user.id):
            return {'error': 'not authorized for this session'}, 403
        if row.conversion_id is None:
            return {'error': 'process the session before contacting support'}, 409
        ordinal = await _next_ordinal(db, row.conversion_id)
        await db.execute(Communication.__table__.insert().values(
            conversion_id=row.conversion_id, ordinal=ordinal,
            subject=subject, content=content, email_address=user.email,
            from_admin=False, is_help_desk=True,
        ))
        # The user is up to date with their own thread after posting.
        await db.execute(update(Session).where(Session.token == token)
                         .values(help_user_seen_at=_now()))
        await db.commit()
    if _send_email is not None:
        _send_email(SERVICE_HELP_EMAIL, f'[Help desk] C_{row.conversion_id}: {subject}',
                    f'From: {user.email}\nConversion: C_{row.conversion_id}\n\n{content}')
    return {'ok': True}, 200


@auth_bp.route('/api/help/inquiries', methods=['GET'])
async def help_inquiries():
    """Annotator view: all help-desk threads (communication rows)."""
    async with _session_factory() as db:
        auth_session, user = await current_auth(db)
        if not is_admin(auth_session, user):
            return {'error': 'not authorized'}, 403
        rows = (
            await db.execute(
                select(Communication)
                .where(Communication.is_help_desk.is_(True))
                .order_by(Communication.conversion_id.desc(), Communication.ordinal.asc())
                .limit(1000)
            )
        ).scalars().all()
        items = [_message_view(r) for r in rows]
        return {'inquiries': items}, 200


def _message_view(r):
    """Serialize one help-desk communication row for the frontend."""
    return {
        'conversion_id': r.conversion_id,
        'public_id': f'C_{r.conversion_id}',
        'ordinal': r.ordinal,
        'subject': r.subject,
        'content': r.content,
        'email_address': r.email_address,
        'sent_at': r.sent_at.isoformat() if r.sent_at else None,
        'from_admin': bool(r.from_admin),
    }


@auth_bp.route('/api/help/thread', methods=['GET'])
async def help_thread():
    """Return the help-desk message thread (the caller's inquiries + annotator
    replies) for one of the caller's own sessions, newest first. Owner-only."""
    async with _session_factory() as db:
        auth_session, user = await current_auth(db)
        if user is None:
            return {'error': 'authentication required'}, 401
        token = request.args.get('token')
        if not token:
            return {'error': 'token is required'}, 400
        row = (
            await db.execute(select(Session).where(Session.token == token))
        ).scalar_one_or_none()
        if row is None:
            return {'error': 'session not found'}, 404
        if row.user_id is None or str(row.user_id) != str(user.id):
            return {'error': 'not authorized for this session'}, 403
        if row.conversion_id is None:
            return {'messages': []}, 200
        rows = (
            await db.execute(
                select(Communication)
                .where(
                    Communication.conversion_id == row.conversion_id,
                    Communication.is_help_desk.is_(True),
                )
                .order_by(Communication.ordinal.desc())
            )
        ).scalars().all()
        # Opening the thread marks all replies seen (clears the new-reply badge).
        await db.execute(update(Session).where(Session.token == token)
                         .values(help_user_seen_at=_now()))
        await db.commit()
        return {'messages': [_message_view(r) for r in rows]}, 200


@auth_bp.route('/api/help/unread', methods=['GET'])
async def help_unread():
    """Notification state for the bell badge + row highlighting (per site).

    - annotator: sessions whose latest help-desk message is a user inquiry with no
      reply after it (awaiting a reply on this site).
    - user: own sessions with an annotator reply newer than help_user_seen_at.

    Returns {count, conversion_ids}. Anonymous callers get zeros (no error)."""
    async with _session_factory() as db:
        auth_session, user = await current_auth(db)
        if user is None:
            return {'count': 0, 'conversion_ids': []}, 200

        if is_admin(auth_session, user):
            rows = (
                await db.execute(
                    select(
                        Communication.conversion_id,
                        Communication.ordinal,
                        Communication.from_admin,
                    ).where(Communication.is_help_desk.is_(True))
                )
            ).all()
            latest = {}  # conversion_id -> (max ordinal, from_admin of that row)
            for cid, ordi, from_admin in rows:
                if cid not in latest or ordi > latest[cid][0]:
                    latest[cid] = (ordi, from_admin)
            ids = sorted(cid for cid, (_, from_admin) in latest.items() if not from_admin)
            return {'count': len(ids), 'conversion_ids': ids}, 200

        # Regular user: own sessions with an unseen annotator reply.
        sessions = (
            await db.execute(
                select(Session.conversion_id, Session.help_user_seen_at).where(
                    Session.user_id == user.id, Session.conversion_id.isnot(None)
                )
            )
        ).all()
        ids = []
        for cid, seen_at in sessions:
            latest_reply = (
                await db.execute(
                    select(func.max(Communication.sent_at)).where(
                        Communication.conversion_id == cid,
                        Communication.is_help_desk.is_(True),
                        Communication.from_admin.is_(True),
                    )
                )
            ).scalar_one_or_none()
            if latest_reply is not None and (seen_at is None or latest_reply > seen_at):
                ids.append(cid)
        return {'count': len(ids), 'conversion_ids': sorted(ids)}, 200


@auth_bp.route('/api/help/reply', methods=['POST'])
async def help_reply():
    """Annotator replies to an inquiry thread; logged and emailed to the user."""
    async with _session_factory() as db:
        auth_session, user = await current_auth(db)
        if not is_admin(auth_session, user):
            return {'error': 'not authorized'}, 403
        if not csrf_valid(auth_session):
            return {'error': 'invalid CSRF token'}, 403
        body = request.get_json(silent=True) or {}
        conversion_id = body.get('conversion_id')
        content = (body.get('content') or '').strip()
        subject = (body.get('subject') or 'Re: your bmrb_extract inquiry').strip()
        if not conversion_id or not content:
            return {'error': 'conversion_id and content are required'}, 400
        # Recipient: the most recent user address recorded for this conversation.
        recipient = (
            await db.execute(
                select(Communication.email_address).where(
                    Communication.conversion_id == conversion_id,
                    Communication.is_help_desk.is_(True),
                    Communication.from_admin.is_(False),
                ).order_by(Communication.ordinal.desc()).limit(1)
            )
        ).scalar_one_or_none()
        ordinal = await _next_ordinal(db, conversion_id)
        await db.execute(Communication.__table__.insert().values(
            conversion_id=conversion_id, ordinal=ordinal,
            subject=subject, content=content,
            email_address=recipient or SERVICE_HELP_EMAIL,
            from_admin=True, is_help_desk=True,
        ))
        await record_admin_access(db, user, None, f'help_reply:C_{conversion_id}')
        await db.commit()
    if _send_email is not None and recipient:
        _send_email(recipient, f'[bmrb_extract] {subject}',
                    f'{content}\n\n(Regarding C_{conversion_id})')
    return {'ok': True}, 200
