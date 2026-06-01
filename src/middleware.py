"""Rate limiting: a rolling fixed-window counter and the ASGI middleware that
enforces it on ``/api/*`` requests.

Each distinct caller gets one :class:`~src.models.RateLimitStats` row keyed by
an identifier: ``user:<id>`` for a request carrying a valid Bearer token, or
``ip:<addr>`` for an anonymous one. Authenticated callers get a higher quota.
When the wall clock passes the row's ``reset_at`` the window rolls over — the
count resets to zero and a fresh window opens.

The middleware is intentionally *fail-open*: if no database/session is
available, or identification raises, the request proceeds without limiting. This
keeps a transient infrastructure problem from taking the whole API offline, at
the cost of not enforcing the limit during the outage.
"""

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from src.auth import AuthError, validate_token
from src.models import RateLimitStats

ANON_LIMIT = 100
AUTH_LIMIT = 1000
WINDOW_SECONDS = 3600

# Only these path prefixes are rate limited; static assets and HTML pages are
# left untouched so page rendering is never throttled.
_GUARDED_PREFIX = "/api/"


@dataclass
class RateLimitResult:
    """Outcome of a single rate-limit check.

    ``allowed`` is False when the caller is over quota. ``remaining`` is how many
    requests are left in the current window (0 once blocked). ``reset_at`` is the
    naive-UTC end of the window; ``retry_after`` is whole seconds until then.
    """

    allowed: bool
    limit: int
    remaining: int
    reset_at: dt.datetime
    retry_after: int


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _as_naive_utc(value: dt.datetime) -> dt.datetime:
    """Return *value* as naive UTC (see :func:`src.auth._as_naive_utc`)."""
    if value.tzinfo is not None:
        value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value


def check_rate_limit(
    session,
    identifier: str,
    *,
    limit: int,
    window_seconds: int = WINDOW_SECONDS,
    now: dt.datetime | None = None,
) -> RateLimitResult:
    """Count one request from *identifier* against its rolling window.

    Loads or creates the counter row for *identifier*. If the window has expired
    (or this is the first request) a new window of *window_seconds* opens with a
    zero count. When the count is already at *limit* the request is rejected
    (``allowed=False``) and the count is not incremented; otherwise the count is
    incremented and the request allowed. The row is committed before returning so
    the count survives across requests/sessions.
    """
    moment = _as_naive_utc(now or _now())
    row = session.scalars(
        select(RateLimitStats).where(RateLimitStats.identifier == identifier)
    ).first()

    if row is None:
        row = RateLimitStats(
            identifier=identifier,
            request_count=0,
            reset_at=moment + dt.timedelta(seconds=window_seconds),
        )
        session.add(row)
    elif row.reset_at is None or _as_naive_utc(row.reset_at) <= moment:
        row.request_count = 0
        row.reset_at = moment + dt.timedelta(seconds=window_seconds)

    reset_at = _as_naive_utc(row.reset_at)
    seconds_left = max(0, int((reset_at - moment).total_seconds()))

    if row.request_count >= limit:
        session.commit()
        return RateLimitResult(
            allowed=False,
            limit=limit,
            remaining=0,
            reset_at=reset_at,
            retry_after=max(1, seconds_left),
        )

    row.request_count += 1
    session.commit()
    return RateLimitResult(
        allowed=True,
        limit=limit,
        remaining=max(0, limit - row.request_count),
        reset_at=reset_at,
        retry_after=seconds_left,
    )


def rate_limit_headers(result: RateLimitResult, *, include_retry: bool = False) -> dict:
    """Build the ``X-RateLimit-*`` (and optional ``Retry-After``) headers.

    ``X-RateLimit-Reset`` is the window end as a Unix epoch second; the naive-UTC
    ``reset_at`` is treated as UTC for that conversion. ``Retry-After`` is only
    included for a blocked (429) response.
    """
    reset_epoch = int(result.reset_at.replace(tzinfo=dt.timezone.utc).timestamp())
    headers = {
        "X-RateLimit-Limit": str(result.limit),
        "X-RateLimit-Remaining": str(result.remaining),
        "X-RateLimit-Reset": str(reset_epoch),
    }
    if include_retry:
        headers["Retry-After"] = str(result.retry_after)
    return headers


class RateLimitMiddleware(BaseHTTPMiddleware):
    """ASGI middleware enforcing per-IP and per-user rate limits on ``/api/*``.

    A request is identified by its Bearer token (counted per user, ``AUTH_LIMIT``)
    when one is present and valid, otherwise by client IP (``ANON_LIMIT``). An
    over-quota request short-circuits with ``429 Too Many Requests`` carrying
    ``Retry-After`` and ``X-RateLimit-*`` headers; an allowed request proceeds and
    has the same ``X-RateLimit-*`` headers attached to its response. Non-``/api/``
    paths bypass limiting entirely.
    """

    def __init__(
        self,
        app,
        session_factory,
        *,
        anon_limit: int = ANON_LIMIT,
        auth_limit: int = AUTH_LIMIT,
        window_seconds: int = WINDOW_SECONDS,
    ) -> None:
        super().__init__(app)
        self._session_factory = session_factory
        self._anon_limit = anon_limit
        self._auth_limit = auth_limit
        self._window_seconds = window_seconds

    def _identify(self, request, session) -> tuple[str, int]:
        """Return the ``(identifier, limit)`` for *request*.

        A valid Bearer token yields ``user:<id>`` at the authenticated limit; a
        missing or invalid token falls back to ``ip:<addr>`` at the anonymous
        limit (an invalid token is not rejected here — that is the protected
        endpoint's job; here it merely fails to upgrade the quota).
        """
        authorization = request.headers.get("authorization")
        if authorization:
            try:
                token = validate_token(session, _bearer(authorization), touch=True)
                return f"user:{token.user_id}", self._auth_limit
            except AuthError:
                pass
        client = request.client.host if request.client else "unknown"
        return f"ip:{client}", self._anon_limit

    async def dispatch(self, request, call_next):
        if not request.url.path.startswith(_GUARDED_PREFIX):
            return await call_next(request)

        session = None
        try:
            session = self._session_factory()
            identifier, limit = self._identify(request, session)
            result = check_rate_limit(
                session,
                identifier,
                limit=limit,
                window_seconds=self._window_seconds,
            )
        except Exception:
            # Fail open: never let a rate-limit infra error break the API.
            return await call_next(request)
        finally:
            if session is not None:
                session.close()

        if not result.allowed:
            return JSONResponse(
                {"detail": "rate limit exceeded"},
                status_code=429,
                headers=rate_limit_headers(result, include_retry=True),
            )

        response = await call_next(request)
        for key, value in rate_limit_headers(result).items():
            response.headers[key] = value
        return response


def _bearer(authorization: str) -> str:
    """Extract the credential from a Bearer header, or '' if not Bearer-shaped."""
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1].strip()
