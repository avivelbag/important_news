import datetime as dt

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.api as api
from src.auth import (
    AuthError,
    authenticate_header,
    create_token,
    generate_token,
    hash_token,
    list_tokens,
    revoke_token,
    validate_token,
)
from src.db import get_engine, get_session, init_db
from src.middleware import (
    RateLimitMiddleware,
    check_rate_limit,
    rate_limit_headers,
)

_T0 = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc)


@pytest.fixture()
def engine():
    eng = get_engine("sqlite://")
    init_db(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine):
    sess = get_session(engine)
    yield sess
    sess.close()


# --- token service ----------------------------------------------------------


def test_generate_and_hash_token_are_distinct_and_deterministic():
    a, b = generate_token(), generate_token()
    assert a.startswith("tok_") and b.startswith("tok_")
    assert a != b
    assert hash_token(a) == hash_token(a)
    assert hash_token(a) != hash_token(b)
    assert hash_token(a) != a  # the stored hash is never the raw token


def test_create_token_returns_raw_once_and_stores_only_hash(session):
    created = create_token(session, "alice", "ci-bot", now=_T0)
    assert created["token"].startswith("tok_")
    assert created["name"] == "ci-bot"
    assert created["is_active"] is True
    assert created["prefix"] == created["token"][:12]
    # A later listing never exposes the raw secret.
    listed = list_tokens(session, "alice")
    assert len(listed) == 1
    assert "token" not in listed[0]
    assert listed[0]["prefix"] == created["prefix"]


def test_validate_token_success_stamps_last_used(session):
    created = create_token(session, "alice", "k", now=_T0)
    later = _T0 + dt.timedelta(minutes=5)
    token = validate_token(session, created["token"], now=later)
    assert token.user_id == "alice"
    assert token.last_used_at is not None
    assert token.last_used_at.replace(tzinfo=None) == later.replace(tzinfo=None)


def test_validate_unknown_token_is_unauthorized(session):
    with pytest.raises(AuthError) as exc:
        validate_token(session, "tok_does_not_exist")
    assert exc.value.unauthorized is True


def test_validate_revoked_token_is_unauthorized(session):
    created = create_token(session, "alice", "k", now=_T0)
    revoke_token(session, "alice", created["id"])
    with pytest.raises(AuthError) as exc:
        validate_token(session, created["token"], now=_T0)
    assert exc.value.unauthorized is True


def test_validate_expired_token_is_unauthorized(session):
    created = create_token(session, "alice", "k", expires_in_seconds=60, now=_T0)
    after = _T0 + dt.timedelta(seconds=61)
    with pytest.raises(AuthError) as exc:
        validate_token(session, created["token"], now=after)
    assert exc.value.unauthorized is True
    # Still valid one second before expiry.
    before = _T0 + dt.timedelta(seconds=59)
    assert validate_token(session, created["token"], now=before).user_id == "alice"


def test_create_token_rejects_empty_name_and_bad_expiry(session):
    with pytest.raises(AuthError):
        create_token(session, "alice", "   ", now=_T0)
    with pytest.raises(AuthError):
        create_token(session, "alice", "ok", expires_in_seconds=0, now=_T0)


def test_revoke_is_scoped_to_owner(session):
    mine = create_token(session, "alice", "k", now=_T0)
    with pytest.raises(AuthError) as exc:
        revoke_token(session, "mallory", mine["id"])
    assert exc.value.not_found is True
    # Token still usable by its real owner.
    assert validate_token(session, mine["token"], now=_T0).user_id == "alice"


def test_authenticate_header_variants(session):
    created = create_token(session, "alice", "k", now=_T0)
    ok = authenticate_header(session, f"Bearer {created['token']}", now=_T0)
    assert ok.user_id == "alice"
    for bad in [None, "", "Basic xyz", "Bearer", "Bearer "]:
        with pytest.raises(AuthError) as exc:
            authenticate_header(session, bad, now=_T0)
        assert exc.value.unauthorized is True


# --- rate limit core --------------------------------------------------------


def test_check_rate_limit_counts_then_blocks(session):
    last = None
    for i in range(3):
        last = check_rate_limit(session, "ip:1.2.3.4", limit=3, now=_T0)
        assert last.allowed is True
        assert last.remaining == 3 - (i + 1)
    blocked = check_rate_limit(session, "ip:1.2.3.4", limit=3, now=_T0)
    assert blocked.allowed is False
    assert blocked.remaining == 0
    assert blocked.retry_after >= 1


def test_check_rate_limit_window_rolls_over(session):
    for _ in range(3):
        check_rate_limit(session, "ip:9", limit=3, window_seconds=3600, now=_T0)
    assert check_rate_limit(session, "ip:9", limit=3, now=_T0).allowed is False
    # After the window passes, the counter resets and requests flow again.
    later = _T0 + dt.timedelta(seconds=3601)
    fresh = check_rate_limit(session, "ip:9", limit=3, now=later)
    assert fresh.allowed is True
    assert fresh.remaining == 2


def test_check_rate_limit_isolates_identifiers(session):
    a = check_rate_limit(session, "user:alice", limit=1000, now=_T0)
    b = check_rate_limit(session, "ip:5.5.5.5", limit=100, now=_T0)
    assert a.limit == 1000 and b.limit == 100
    # Exhausting one identifier does not affect the other.
    check_rate_limit(session, "ip:5.5.5.5", limit=1, now=_T0)
    assert check_rate_limit(session, "ip:5.5.5.5", limit=1, now=_T0).allowed is False
    assert check_rate_limit(session, "user:alice", limit=1000, now=_T0).allowed is True


def test_rate_limit_headers_shape():
    from src.middleware import RateLimitResult

    res = RateLimitResult(
        allowed=False, limit=100, remaining=0, reset_at=_T0.replace(tzinfo=None),
        retry_after=42,
    )
    headers = rate_limit_headers(res, include_retry=True)
    assert headers["X-RateLimit-Limit"] == "100"
    assert headers["X-RateLimit-Remaining"] == "0"
    assert headers["Retry-After"] == "42"
    assert "X-RateLimit-Reset" in headers


# --- middleware integration -------------------------------------------------


@pytest.fixture()
def mini_client(tmp_path):
    # A file-backed engine so the middleware's worker thread can open its own
    # connection (an in-memory sqlite is bound to the creating thread).
    eng = get_engine(f"sqlite:///{tmp_path / 'mini.db'}")
    init_db(eng)
    mini = FastAPI()
    mini.add_middleware(
        RateLimitMiddleware,
        session_factory=lambda: get_session(eng),
        anon_limit=2,
        window_seconds=3600,
    )

    @mini.get("/api/ping")
    def ping():
        return {"ok": True}

    @mini.get("/health")
    def health():
        return {"ok": True}

    return TestClient(mini)


def test_middleware_adds_headers_and_blocks_over_limit(mini_client):
    first = mini_client.get("/api/ping")
    assert first.status_code == 200
    assert first.headers["X-RateLimit-Limit"] == "2"
    assert first.headers["X-RateLimit-Remaining"] == "1"

    assert mini_client.get("/api/ping").status_code == 200
    blocked = mini_client.get("/api/ping")
    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"]
    assert blocked.headers["X-RateLimit-Remaining"] == "0"


def test_middleware_ignores_non_api_paths(mini_client):
    # /health is not guarded, so it never carries rate-limit headers or 429s.
    for _ in range(5):
        resp = mini_client.get("/health")
        assert resp.status_code == 200
        assert "X-RateLimit-Limit" not in resp.headers


# --- API endpoints ----------------------------------------------------------


@pytest.fixture()
def api_engine(tmp_path, monkeypatch):
    eng = get_engine(f"sqlite:///{tmp_path / 'api.db'}")
    init_db(eng)
    monkeypatch.setattr(api, "_engine", eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def client(api_engine):
    return TestClient(api.app)


def test_token_endpoints_create_list_revoke_flow(client):
    created = client.post("/api/user/tokens", json={"name": "mobile"})
    assert created.status_code == 201
    body = created.json()
    assert body["token"].startswith("tok_")
    token_id = body["id"]

    listed = client.get("/api/user/tokens")
    assert listed.status_code == 200
    rows = listed.json()
    assert [r["id"] for r in rows] == [token_id]
    assert "token" not in rows[0]

    revoked = client.delete(f"/api/user/tokens/{token_id}")
    assert revoked.status_code == 200
    assert revoked.json() == {"revoked": token_id}
    assert client.get("/api/user/tokens").json()[0]["is_active"] is False


def test_validate_endpoint_accepts_valid_and_rejects_invalid(client):
    created = client.post("/api/user/tokens", json={"name": "k"})
    raw = created.json()["token"]

    ok = client.get("/api/auth/validate", headers={"Authorization": f"Bearer {raw}"})
    assert ok.status_code == 200
    assert ok.json()["valid"] is True

    missing = client.get("/api/auth/validate")
    assert missing.status_code == 401

    bad = client.get(
        "/api/auth/validate", headers={"Authorization": "Bearer tok_nope"}
    )
    assert bad.status_code == 401


def test_revoked_token_is_rejected_by_validate_endpoint(client):
    created = client.post("/api/user/tokens", json={"name": "k"})
    raw = created.json()["token"]
    token_id = created.json()["id"]
    client.delete(f"/api/user/tokens/{token_id}")
    resp = client.get(
        "/api/auth/validate", headers={"Authorization": f"Bearer {raw}"}
    )
    assert resp.status_code == 401


def test_list_tokens_empty_without_cookie(client):
    resp = client.get("/api/user/tokens")
    assert resp.status_code == 200
    assert resp.json() == []


def test_revoke_without_cookie_is_404(client):
    assert client.delete("/api/user/tokens/1").status_code == 404


def test_search_response_carries_rate_limit_headers(client):
    resp = client.get("/api/search", params={"q": "anything"})
    # Whatever the search result, the limiter annotated the response.
    assert "X-RateLimit-Limit" in resp.headers
    assert resp.headers["X-RateLimit-Limit"] == "100"
