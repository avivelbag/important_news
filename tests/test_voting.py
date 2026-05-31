"""Tests for the voting service, vote-aware scoring/search, and vote API."""

import datetime as dt
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import src.api as api
import src.scorer as scorer
from src.db import get_engine, get_session, init_db
from src.models import Story, Vote
from src.search import search_stories
from src.voting import VoteError, cast_vote, get_distribution

NOW = dt.datetime(2024, 6, 1, tzinfo=dt.timezone.utc)


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


def _make_story(session, **kwargs) -> Story:
    base = dict(
        title="A Title",
        url="https://example.com/article",
        source_name="hn",
        topic="ai",
        published_at=datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 1, 2, 3, 5, tzinfo=timezone.utc),
    )
    base.update(kwargs)
    story = Story(**base)
    session.add(story)
    session.commit()
    return story


# --- service: happy path ----------------------------------------------------


def test_upvote_sets_points_and_upvotes(session):
    story = _make_story(session)
    dist = cast_vote(session, story.id, "user-a", 1)
    assert dist["points"] == 1
    assert dist["vote_count"] == 1
    assert dist["upvotes"] == 1
    assert dist["downvotes"] == 0
    assert session.get(Story, story.id).vote_count == 1

    again = get_distribution(session, story.id)
    assert again == dist


def test_multiple_users_aggregate(session):
    story = _make_story(session)
    cast_vote(session, story.id, "u1", 1)
    cast_vote(session, story.id, "u2", 1)
    cast_vote(session, story.id, "u3", 1)
    dist = cast_vote(session, story.id, "u4", -1)
    assert dist["points"] == 2
    assert dist["upvotes"] == 3
    assert dist["downvotes"] == 1
    assert session.get(Story, story.id).downvotes == 1


# --- service: change / reversal ---------------------------------------------


def test_vote_change_updates_same_row(session):
    story = _make_story(session)
    cast_vote(session, story.id, "u1", 1)
    rows = session.scalars(select(Vote).where(Vote.story_id == story.id)).all()
    assert len(rows) == 1
    assert rows[0].updated_at is None

    dist = cast_vote(session, story.id, "u1", -1)
    rows = session.scalars(select(Vote).where(Vote.story_id == story.id)).all()
    assert len(rows) == 1  # no duplicate row created (unique constraint path)
    assert rows[0].vote_value == -1
    assert rows[0].updated_at is not None
    assert dist["points"] == -1
    assert dist["downvotes"] == 1

    reversed_dist = cast_vote(session, story.id, "u1", 0)
    rows = session.scalars(select(Vote).where(Vote.story_id == story.id)).all()
    assert len(rows) == 1
    assert reversed_dist["points"] == 0
    assert reversed_dist["upvotes"] == 0
    assert reversed_dist["downvotes"] == 0


def test_null_user_id_votes_are_distinct(session):
    """Anonymous (no user_id) votes do not collide on the unique constraint."""
    story = _make_story(session)
    now = NOW.replace(tzinfo=None)
    session.add(Vote(story_id=story.id, created_at=now))
    session.add(Vote(story_id=story.id, created_at=now))
    session.commit()
    rows = session.scalars(select(Vote).where(Vote.story_id == story.id)).all()
    assert len(rows) == 2


# --- service: failure modes -------------------------------------------------


def test_invalid_vote_value_raises(session):
    story = _make_story(session)
    with pytest.raises(VoteError):
        cast_vote(session, story.id, "u1", 2)


def test_vote_missing_story_raises(session):
    with pytest.raises(VoteError):
        cast_vote(session, 99999, "u1", 1)


def test_distribution_missing_story_raises(session):
    with pytest.raises(VoteError):
        get_distribution(session, 99999)


# --- ranking respects votes -------------------------------------------------


def test_votes_raise_compute_score_additively(session):
    weights = scorer.ScoreWeights()
    voted = Story(
        title="t",
        url="https://example.com/voted",
        source_name="src",
        topic="aerospace",
        vote_count=10,
        published_at=NOW.replace(tzinfo=None),
        fetched_at=NOW.replace(tzinfo=None),
    )
    unvoted = Story(
        title="t",
        url="https://example.com/unvoted",
        source_name="src",
        topic="aerospace",
        vote_count=0,
        published_at=NOW.replace(tzinfo=None),
        fetched_at=NOW.replace(tzinfo=None),
    )
    baseline = scorer.compute_score(unvoted, 1.0, NOW, weights)
    voted_score = scorer.compute_score(voted, 1.0, NOW, weights)
    assert baseline == pytest.approx(1.0)  # unchanged pre-vote baseline
    assert voted_score > baseline
    assert voted_score == pytest.approx(1.0 + 10 * weights.vote_weight)


def test_vote_weight_from_env():
    weights = scorer.ScoreWeights.from_env({"SCORER_VOTE_WEIGHT": "0.5"})
    assert weights.vote_weight == pytest.approx(0.5)


# --- search respects votes --------------------------------------------------


def test_search_vote_count_breaks_ties(session):
    published = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _make_story(
        session,
        title="rocket low",
        url="https://a/low",
        topic="aerospace",
        vote_count=1,
        published_at=published,
    )
    _make_story(
        session,
        title="rocket high",
        url="https://a/high",
        topic="aerospace",
        vote_count=50,
        published_at=published,
    )
    results = search_stories(session, "rocket")
    urls = [r["url"] for r in results]
    # Equal relevance and recency: the higher-voted story must not sort after.
    assert urls.index("https://a/high") < urls.index("https://a/low")


# --- API --------------------------------------------------------------------


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


def _seed_story(engine, **kwargs) -> int:
    sess = get_session(engine)
    try:
        base = dict(
            title="API story",
            url="https://example.com/api",
            source_name="hn",
            topic="ai",
            published_at=datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
            fetched_at=datetime(2026, 1, 2, 3, 5, tzinfo=timezone.utc),
        )
        base.update(kwargs)
        story = Story(**base)
        sess.add(story)
        sess.commit()
        return story.id
    finally:
        sess.close()


def test_api_vote_returns_distribution_and_sets_cookie(client, api_engine):
    story_id = _seed_story(api_engine)
    resp = client.post("/api/vote", json={"story_id": story_id, "vote_value": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["points"] == 1
    assert body["upvotes"] == 1
    assert "voter_id" in resp.cookies


def test_api_vote_accepts_article_id_alias(client, api_engine):
    story_id = _seed_story(api_engine, url="https://example.com/alias")
    resp = client.post("/api/vote", json={"article_id": story_id, "vote_value": -1})
    assert resp.status_code == 200
    assert resp.json()["downvotes"] == 1


def test_api_get_article_votes(client, api_engine):
    story_id = _seed_story(api_engine, url="https://example.com/get")
    client.post("/api/vote", json={"story_id": story_id, "vote_value": 1})
    resp = client.get(f"/api/articles/{story_id}/votes")
    assert resp.status_code == 200
    assert resp.json()["points"] == 1


def test_api_vote_invalid_value_is_400(client, api_engine):
    story_id = _seed_story(api_engine, url="https://example.com/bad")
    resp = client.post("/api/vote", json={"story_id": story_id, "vote_value": 2})
    assert resp.status_code == 400


def test_api_vote_missing_story_is_404(client, api_engine):
    resp = client.post("/api/vote", json={"story_id": 99999, "vote_value": 1})
    assert resp.status_code == 404


def test_api_get_votes_missing_story_is_404(client, api_engine):
    resp = client.get("/api/articles/99999/votes")
    assert resp.status_code == 404


# --- generated site shows distribution / buttons ----------------------------


def test_render_story_has_vote_controls_and_downvotes(session):
    from src.generate_site import render_story

    story = _make_story(session, vote_count=3, downvotes=2)
    html = render_story(story, 1)
    assert 'data-story-id=' in html
    assert 'class="vote up"' in html
    assert 'class="vote down"' in html
    assert "2 downvotes" in html


def test_vote_js_written_and_external(session, engine, tmp_path):
    from src.generate_site import generate_site

    _make_story(session, url="https://a/site")
    out = generate_site(engine=engine, out_dir=tmp_path / "docs")
    index = (out / "index.html").read_text(encoding="utf-8")
    js = (out / "vote.js").read_text(encoding="utf-8")
    assert '<script src="vote.js"></script>' in index
    assert "/api/vote" in js
    assert "localStorage" in js
