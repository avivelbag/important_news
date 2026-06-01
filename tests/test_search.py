from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import src.api as api
from src.db import get_engine, get_session, init_db
from src.models import Story
from src.search import (
    MAX_QUERY_LEN,
    MIN_QUERY_LEN,
    SearchError,
    search_stories,
    validate_query,
)


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


def _make_story(**kwargs) -> Story:
    base = dict(
        title="A Title",
        url="https://example.com/article",
        source_name="hn",
        topic="ai",
        raw_score=10,
        vote_count=5,
        computed_score=1.0,
        published_at=datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 1, 2, 3, 5, tzinfo=timezone.utc),
    )
    base.update(kwargs)
    return Story(**base)


def _seed(session, stories):
    session.add_all(stories)
    session.commit()


def test_exact_title_match(session):
    _seed(
        session,
        [_make_story(title="SpaceX launches rocket", url="https://a/1", topic="aerospace")],
    )
    results = search_stories(session, "rocket")
    assert len(results) == 1
    assert results[0]["title"] == "SpaceX launches rocket"
    assert results[0]["url"] == "https://a/1"
    assert results[0]["source"] == "hn"
    assert results[0]["score"] >= 3


def test_partial_word_match_is_substring(session):
    _seed(session, [_make_story(title="Transformers explained", url="https://a/2")])
    # "former" is a substring of "Transformers".
    results = search_stories(session, "former")
    assert len(results) == 1
    assert results[0]["title"] == "Transformers explained"


def test_title_outranks_secondary_match(session):
    _seed(
        session,
        [
            _make_story(
                title="Mars mission update",
                url="https://a/title",
                source_name="genericfeed",
                topic="aerospace",
            ),
            _make_story(
                title="Unrelated headline",
                url="https://a/source",
                source_name="mars-daily",
                topic="aerospace",
            ),
        ],
    )
    results = search_stories(session, "mars")
    assert [r["url"] for r in results] == ["https://a/title", "https://a/source"]
    assert results[0]["score"] > results[1]["score"]


def test_recency_breaks_score_ties(session):
    _seed(
        session,
        [
            _make_story(
                title="rocket older",
                url="https://a/old",
                topic="aerospace",
                published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
            _make_story(
                title="rocket newer",
                url="https://a/new",
                topic="aerospace",
                published_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
            ),
        ],
    )
    results = search_stories(session, "rocket")
    assert [r["url"] for r in results] == ["https://a/new", "https://a/old"]


def test_category_filter(session):
    _seed(
        session,
        [
            _make_story(title="rocket ai story", url="https://a/ai", topic="ai"),
            _make_story(title="rocket aero story", url="https://a/aero", topic="aerospace"),
            _make_story(title="rocket both story", url="https://a/both", topic="both"),
        ],
    )
    aero = search_stories(session, "rocket", category="aerospace")
    urls = {r["url"] for r in aero}
    # "both" stories count as aerospace; the pure-ai story is excluded.
    assert urls == {"https://a/aero", "https://a/both"}


def test_no_matches_returns_empty(session):
    _seed(session, [_make_story(title="Quantum computing", url="https://a/q")])
    assert search_stories(session, "zzz nonexistent") == []


def test_too_short_query_raises(session):
    with pytest.raises(SearchError):
        search_stories(session, "a")


def test_whitespace_only_query_raises(session):
    with pytest.raises(SearchError):
        search_stories(session, "   ")


def test_too_long_query_raises(session):
    with pytest.raises(SearchError):
        search_stories(session, "x" * (MAX_QUERY_LEN + 1))


def test_validate_query_trims_and_returns():
    assert validate_query("  hello  ") == "hello"
    assert len(validate_query("ab")) == MIN_QUERY_LEN


def test_limit_caps_results(session):
    _seed(
        session,
        [_make_story(title=f"rocket {i}", url=f"https://a/{i}") for i in range(10)],
    )
    results = search_stories(session, "rocket", limit=3)
    assert len(results) == 3


def test_result_has_full_contract_shape(session):
    _seed(session, [_make_story(title="rocket here", url="https://a/c")])
    result = search_stories(session, "rocket")[0]
    assert set(result.keys()) == {
        "id",
        "title",
        "description",
        "url",
        "source",
        "date",
        "score",
        "credibility_score",
        "credibility_badge",
    }
    # SQLite stores naive datetimes, so the ISO string carries no tz offset.
    assert result["date"] == "2026-01-02T03:04:00"


# --- API endpoint tests -----------------------------------------------------


@pytest.fixture()
def api_engine(tmp_path, monkeypatch):
    # A file-backed DB so the TestClient's worker thread sees the same tables
    # the test seeds (an in-memory SQLite DB is per-connection/per-thread).
    eng = get_engine(f"sqlite:///{tmp_path / 'api.db'}")
    init_db(eng)
    monkeypatch.setattr(api, "_engine", eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def client(api_engine):
    return TestClient(api.app)


def _seed_engine(engine, stories):
    sess = get_session(engine)
    try:
        sess.add_all(stories)
        sess.commit()
    finally:
        sess.close()


def test_api_search_returns_results(client, api_engine):
    _seed_engine(api_engine, [_make_story(title="rocket api story", url="https://a/api")])
    resp = client.get("/api/search", params={"q": "rocket"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["title"] == "rocket api story"


def test_api_search_category_filter(client, api_engine):
    _seed_engine(
        api_engine,
        [
            _make_story(title="rocket ai", url="https://a/ai", topic="ai"),
            _make_story(title="rocket aero", url="https://a/aero", topic="aerospace"),
        ],
    )
    resp = client.get("/api/search", params={"q": "rocket", "category": "aerospace"})
    assert resp.status_code == 200
    assert [r["url"] for r in resp.json()] == ["https://a/aero"]


def test_api_search_rejects_short_query(client):
    resp = client.get("/api/search", params={"q": "a"})
    assert resp.status_code == 400


def test_api_search_requires_query(client):
    resp = client.get("/api/search")
    assert resp.status_code == 422


# --- Static-site UI tests ---------------------------------------------------


def test_site_renders_search_box(session, engine, tmp_path):
    from src.generate_site import generate_site

    _seed(session, [_make_story(title="rocket", url="https://a/ui")])
    out = generate_site(engine=engine, out_dir=tmp_path / "docs")
    index = (out / "index.html").read_text(encoding="utf-8")
    assert 'id="search-box"' in index
    assert '<script src="search.js">' in index
    assert (out / "search.js").exists()


def test_search_js_debounces_and_calls_api(session, engine, tmp_path):
    from src.generate_site import generate_site

    out = generate_site(engine=engine, out_dir=tmp_path / "docs")
    js = (out / "search.js").read_text(encoding="utf-8")
    assert "300" in js  # debounce interval
    assert "setTimeout" in js and "clearTimeout" in js
    assert "/api/search?q=" in js
    assert "No results" in js
    assert 'e.key === "Enter"' in js
