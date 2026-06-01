"""API token issuance and Bearer-token authentication.

Tokens are opaque random strings of the form ``tok_<hex>``. The raw value is
returned to the caller exactly once at creation; only its SHA-256 hash is
stored, so the database never holds a usable credential. A short, non-secret
``prefix`` is kept alongside the hash so a management UI can show which token is
which. Authentication hashes a presented token, looks up the matching active,
unexpired row, and stamps ``last_used_at``.

Datetimes are normalised to naive UTC before any comparison: SQLite returns
stored timestamps without tzinfo, so mixing them with an aware ``now`` would
raise ``TypeError``. ``_as_naive_utc`` makes both sides comparable.
"""

import datetime as dt
import hashlib
import secrets

from sqlalchemy import select

from src.models import APIToken

_TOKEN_BYTES = 24
_PREFIX_LEN = 12
_MAX_NAME_LEN = 100


class AuthError(ValueError):
    """Raised for token-management and authentication failures.

    ``not_found`` flags a missing/foreign token (HTTP 404); ``unauthorized``
    flags a token that is absent, malformed, revoked, or expired (HTTP 401), so
    the API layer can choose a status without inspecting the message.
    """

    def __init__(
        self, message: str, *, not_found: bool = False, unauthorized: bool = False
    ) -> None:
        super().__init__(message)
        self.not_found = not_found
        self.unauthorized = unauthorized


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _as_naive_utc(value: dt.datetime) -> dt.datetime:
    """Return *value* as a naive UTC datetime.

    Aware datetimes are converted to UTC and stripped of tzinfo; naive ones are
    assumed already-UTC and returned unchanged. This lets values read back from
    SQLite (always naive) compare safely against a freshly computed ``now``.
    """
    if value.tzinfo is not None:
        value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value


def _clean_user(user_id: str) -> str:
    name = (user_id or "").strip()
    if not name:
        raise AuthError("user_id must not be empty")
    return name


def hash_token(raw_token: str) -> str:
    """Return the hex SHA-256 digest used to store and look up *raw_token*."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def generate_token() -> str:
    """Return a fresh opaque API token string (``tok_<hex>``)."""
    return f"tok_{secrets.token_hex(_TOKEN_BYTES)}"


def _serialize(token: APIToken, *, raw: str | None = None) -> dict:
    """Serialise *token* for the API; include the one-time *raw* value if given.

    ``token_hash`` is never serialised. The raw secret is only ever returned at
    creation time, when the caller passes it in explicitly.
    """
    data = {
        "id": token.id,
        "name": token.name,
        "prefix": token.prefix,
        "is_active": token.is_active,
        "created_at": token.created_at.isoformat() if token.created_at else None,
        "last_used_at": token.last_used_at.isoformat() if token.last_used_at else None,
        "expires_at": token.expires_at.isoformat() if token.expires_at else None,
    }
    if raw is not None:
        data["token"] = raw
    return data


def create_token(
    session,
    user_id: str,
    name: str,
    *,
    expires_in_seconds: int | None = None,
    now: dt.datetime | None = None,
) -> dict:
    """Create an API token for *user_id* and return it, including the raw secret.

    *name* is required and length-bounded. *expires_in_seconds*, if given, must
    be positive and sets ``expires_at`` relative to *now*; omit it for a
    non-expiring token. The returned dict carries the raw ``token`` value, which
    is shown only here and never again. Raises :class:`AuthError` for empty or
    over-long *name* or a non-positive expiry.
    """
    user = _clean_user(user_id)
    clean_name = (name or "").strip()
    if not clean_name:
        raise AuthError("token name must not be empty")
    if len(clean_name) > _MAX_NAME_LEN:
        raise AuthError(f"token name must be at most {_MAX_NAME_LEN} characters")

    moment = _as_naive_utc(now or _now())
    expires_at = None
    if expires_in_seconds is not None:
        if expires_in_seconds <= 0:
            raise AuthError("expires_in_seconds must be positive")
        expires_at = moment + dt.timedelta(seconds=expires_in_seconds)

    raw = generate_token()
    token = APIToken(
        user_id=user,
        name=clean_name,
        token_hash=hash_token(raw),
        prefix=raw[:_PREFIX_LEN],
        created_at=moment,
        expires_at=expires_at,
        is_active=True,
    )
    session.add(token)
    session.commit()
    session.refresh(token)
    return _serialize(token, raw=raw)


def list_tokens(session, user_id: str) -> list[dict]:
    """Return *user_id*'s tokens, newest first, without any raw secret.

    Revoked tokens are included (with ``is_active`` False) so the UI can show
    history. Returns an empty list for a user with none.
    """
    user = _clean_user(user_id)
    rows = session.scalars(
        select(APIToken)
        .where(APIToken.user_id == user)
        .order_by(APIToken.created_at.desc(), APIToken.id.desc())
    ).all()
    return [_serialize(row) for row in rows]


def revoke_token(session, user_id: str, token_id: int) -> dict:
    """Revoke token *token_id* owned by *user_id* and return its serialised form.

    Revocation clears ``is_active`` so the token can no longer authenticate;
    the row is kept for the audit trail. Idempotent — revoking an already
    revoked token is a no-op success. Raises :class:`AuthError` with
    ``not_found=True`` when no such token exists for this user, so a user can
    never revoke another user's token.
    """
    user = _clean_user(user_id)
    token = session.scalars(
        select(APIToken).where(
            APIToken.id == token_id, APIToken.user_id == user
        )
    ).first()
    if token is None:
        raise AuthError("token not found", not_found=True)
    token.is_active = False
    session.commit()
    session.refresh(token)
    return _serialize(token)


def _extract_bearer(authorization: str | None) -> str:
    """Return the raw token from an ``Authorization: Bearer <token>`` header.

    Raises :class:`AuthError` (unauthorized) when the header is missing or not a
    well-formed Bearer credential.
    """
    if not authorization:
        raise AuthError("missing Authorization header", unauthorized=True)
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise AuthError("malformed Authorization header", unauthorized=True)
    return parts[1].strip()


def validate_token(
    session,
    raw_token: str,
    *,
    now: dt.datetime | None = None,
    touch: bool = True,
) -> APIToken:
    """Resolve *raw_token* to its active, unexpired :class:`APIToken`.

    Hashes the presented token and looks up the matching row. Raises
    :class:`AuthError` (unauthorized) when the token is unknown, revoked, or past
    ``expires_at``. On success, stamps ``last_used_at`` with *now* (unless
    *touch* is False) so usage is tracked, and returns the ORM object.
    """
    if not raw_token or not raw_token.strip():
        raise AuthError("empty token", unauthorized=True)
    moment = _as_naive_utc(now or _now())
    token = session.scalars(
        select(APIToken).where(APIToken.token_hash == hash_token(raw_token.strip()))
    ).first()
    if token is None:
        raise AuthError("invalid token", unauthorized=True)
    if not token.is_active:
        raise AuthError("token has been revoked", unauthorized=True)
    if token.expires_at is not None and _as_naive_utc(token.expires_at) <= moment:
        raise AuthError("token has expired", unauthorized=True)
    if touch:
        token.last_used_at = moment
        session.commit()
    return token


def authenticate_header(
    session, authorization: str | None, *, now: dt.datetime | None = None
) -> APIToken:
    """Authenticate an ``Authorization`` header value, returning its token.

    Convenience wrapper over :func:`_extract_bearer` + :func:`validate_token`
    for request handlers. Raises :class:`AuthError` (unauthorized) on any
    missing/malformed/invalid/expired/revoked credential.
    """
    return validate_token(session, _extract_bearer(authorization), now=now)
