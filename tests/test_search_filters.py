from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import src.api as api
from src.db import get_engine, get_session, init_db
from src.models import Story
from src.search import (
    SearchError,
    SearchFilters,
    build_filters,
    search_stories,
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
        title="rocket headline",
        url="https://example.com/article",
        source_name="hn",
        topic="aerospace",
        raw_score=10,
        comment_count=0,
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


# --- score filters ----------------------------------------------------------


def test_min_score_excludes_low_scoring(session):
    _seed(
        session,
        [
            _make_story(title="rocket low", url="https://a/low", raw_score=5),
            _make_story(title="rocket high", url="https://a/high", raw_score=50),
        ],
    )
    results = search_stories(
        session, "rocket", filters=SearchFilters(min_score=10)
    )
    assert [r["url"] for r in results] == ["https://a/high"]


def test_max_score_excludes_high_scoring(session):
    _seed(
        session,
        [
            _make_story(title="rocket low", url="https://a/low", raw_score=5),
            _make_story(title="rocket high", url="https://a/high", raw_score=50),
        ],
    )
    results = search_stories(
        session, "rocket", filters=SearchFilters(max_score=10)
    )
    assert [r["url"] for r in results] == ["https://a/low"]


def test_min_comments_filters_quiet_posts(session):
    _seed(
        session,
        [
            _make_story(title="rocket quiet", url="https://a/q", comment_count=1),
            _make_story(title="rocket hot", url="https://a/h", comment_count=40),
        ],
    )
    results = search_stories(
        session, "rocket", filters=SearchFilters(min_comments=10)
    )
    assert [r["url"] for r in results] == ["https://a/h"]


# --- source / topic filters -------------------------------------------------


def test_sources_filter_is_or_set_and_case_insensitive(session):
    _seed(
        session,
        [
            _make_story(title="rocket a", url="https://a/1", source_name="HN"),
            _make_story(title="rocket b", url="https://a/2", source_name="Reddit"),
            _make_story(title="rocket c", url="https://a/3", source_name="Verge"),
        ],
    )
    results = search_stories(
        session, "rocket", filters=SearchFilters(sources=frozenset({"hn", "reddit"}))
    )
    assert {r["url"] for r in results} == {"https://a/1", "https://a/2"}


def test_topics_filter_expands_both(session):
    _seed(
        session,
        [
            _make_story(title="rocket ai", url="https://a/ai", topic="ai"),
            _make_story(title="rocket aero", url="https://a/aero", topic="aerospace"),
            _make_story(title="rocket both", url="https://a/both", topic="both"),
        ],
    )
    results = search_stories(
        session, "rocket", filters=SearchFilters(topics=frozenset({"ai"}))
    )
    # The pure-aerospace story is excluded; "both" counts as ai.
    assert {r["url"] for r in results} == {"https://a/ai", "https://a/both"}


# --- date filters -----------------------------------------------------------


def test_date_range_inclusive(session):
    _seed(
        session,
        [
            _make_story(
                title="rocket jan",
                url="https://a/jan",
                published_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
            ),
            _make_story(
                title="rocket mar",
                url="https://a/mar",
                published_at=datetime(2026, 3, 15, tzinfo=timezone.utc),
            ),
            _make_story(
                title="rocket may",
                url="https://a/may",
                published_at=datetime(2026, 5, 15, tzinfo=timezone.utc),
            ),
        ],
    )
    filters = build_filters(date_from="2026-02-01", date_to="2026-04-01")
    results = search_stories(session, "rocket", filters=filters)
    assert [r["url"] for r in results] == ["https://a/mar"]


def test_date_to_is_end_of_day_inclusive(session):
    _seed(
        session,
        [
            _make_story(
                title="rocket noon",
                url="https://a/noon",
                published_at=datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
            )
        ],
    )
    # A bare date_to of the same day must still include a midday story.
    filters = build_filters(date_from="2026-03-15", date_to="2026-03-15")
    results = search_stories(session, "rocket", filters=filters)
    assert [r["url"] for r in results] == ["https://a/noon"]


# --- combined filters (AND) -------------------------------------------------


def test_combined_filters_apply_with_and_logic(session):
    _seed(
        session,
        [
            # passes all
            _make_story(
                title="rocket win",
                url="https://a/win",
                source_name="hn",
                raw_score=30,
                published_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
            ),
            # wrong source
            _make_story(
                title="rocket src",
                url="https://a/src",
                source_name="reddit",
                raw_score=30,
                published_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
            ),
            # score too low
            _make_story(
                title="rocket low",
                url="https://a/low",
                source_name="hn",
                raw_score=5,
                published_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
            ),
            # out of date range
            _make_story(
                title="rocket old",
                url="https://a/old",
                source_name="hn",
                raw_score=30,
                published_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            ),
        ],
    )
    filters = build_filters(
        sources="hn", min_score=10, date_from="2026-01-01"
    )
    results = search_stories(session, "rocket", filters=filters)
    assert [r["url"] for r in results] == ["https://a/win"]


# --- sort modes -------------------------------------------------------------


def test_sort_recent_orders_by_date(session):
    _seed(
        session,
        [
            _make_story(
                title="rocket alpha match match",
                url="https://a/relevant",
                published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
            _make_story(
                title="rocket",
                url="https://a/new",
                published_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
            ),
        ],
    )
    results = search_stories(
        session, "rocket", filters=build_filters(sort="recent")
    )
    assert [r["url"] for r in results] == ["https://a/new", "https://a/relevant"]


def test_sort_score_orders_by_raw_score(session):
    _seed(
        session,
        [
            _make_story(title="rocket a", url="https://a/lo", raw_score=1),
            _make_story(title="rocket b", url="https://a/hi", raw_score=99),
        ],
    )
    results = search_stories(
        session, "rocket", filters=build_filters(sort="score")
    )
    assert [r["url"] for r in results] == ["https://a/hi", "https://a/lo"]


# --- build_filters validation -----------------------------------------------


def test_build_filters_parses_csv_and_dates():
    f = build_filters(
        sources="HN, Reddit",
        topics="ai,aerospace",
        min_score="3",
        date_from="2026-01-01",
    )
    assert f.sources == frozenset({"hn", "reddit"})
    assert f.topics == frozenset({"ai", "aerospace"})
    assert f.min_score == 3
    assert f.date_from == datetime(2026, 1, 1)


def test_build_filters_empty_passthrough():
    f = build_filters()
    assert f.sources == frozenset()
    assert f.min_score is None
    assert f.sort == "relevance"


def test_build_filters_rejects_bad_sort():
    with pytest.raises(SearchError):
        build_filters(sort="popularity")


def test_build_filters_rejects_bad_int():
    with pytest.raises(SearchError):
        build_filters(min_score="ten")


def test_build_filters_rejects_bad_date():
    with pytest.raises(SearchError):
        build_filters(date_from="not-a-date")


# --- API endpoint tests -----------------------------------------------------


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


def _seed_engine(engine, stories):
    sess = get_session(engine)
    try:
        sess.add_all(stories)
        sess.commit()
    finally:
        sess.close()


def test_api_search_min_score_filter(client, api_engine):
    _seed_engine(
        api_engine,
        [
            _make_story(title="rocket low", url="https://a/low", raw_score=1),
            _make_story(title="rocket high", url="https://a/high", raw_score=99),
        ],
    )
    resp = client.get("/api/search", params={"q": "rocket", "min_score": 10})
    assert resp.status_code == 200
    assert [r["url"] for r in resp.json()] == ["https://a/high"]


def test_api_search_combined_filters(client, api_engine):
    _seed_engine(
        api_engine,
        [
            _make_story(
                title="rocket win",
                url="https://a/win",
                source_name="hn",
                raw_score=30,
            ),
            _make_story(
                title="rocket lose",
                url="https://a/lose",
                source_name="reddit",
                raw_score=30,
            ),
        ],
    )
    resp = client.get(
        "/api/search",
        params={"q": "rocket", "sources": "hn", "min_score": 10, "sort": "score"},
    )
    assert resp.status_code == 200
    assert [r["url"] for r in resp.json()] == ["https://a/win"]


def test_api_search_rejects_bad_sort(client):
    resp = client.get("/api/search", params={"q": "rocket", "sort": "bogus"})
    assert resp.status_code == 400


def test_api_search_rejects_bad_date(client):
    resp = client.get("/api/search", params={"q": "rocket", "date_from": "nope"})
    assert resp.status_code == 400


# --- static-site filter UI --------------------------------------------------


def test_site_renders_filter_controls(engine, tmp_path):
    from src.generate_site import generate_site

    out = generate_site(engine=engine, out_dir=tmp_path / "docs")
    index = (out / "index.html").read_text(encoding="utf-8")
    assert 'id="filter-min-score"' in index
    assert 'id="filter-sort"' in index
    assert 'id="filter-clear"' in index


def test_search_js_appends_filter_params_and_syncs_url(engine, tmp_path):
    from src.generate_site import generate_site

    out = generate_site(engine=engine, out_dir=tmp_path / "docs")
    js = (out / "search.js").read_text(encoding="utf-8")
    assert "filterParams" in js
    assert "min_score" in js
    assert "replaceState" in js
