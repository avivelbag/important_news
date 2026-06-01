"""Tests for the personalized recommendation feed engine and its API."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import src.api as api
from src.db import get_engine, get_session, init_db
from src.models import Story, Topic
from src.recommendation import (
    RecommendationError,
    build_profile,
    get_preferences,
    personalized_feed,
    set_preferences,
)
from src.topics import follow_topic, seed_topics, tag_story
from src.voting import cast_vote

_BASE = datetime(2024, 1, 1, tzinfo=timezone.utc)


@pytest.fixture()
def engine():
    eng = get_engine("sqlite://")
    init_db(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine):
    sess = get_session(engine)
    seed_topics(sess)
    yield sess
    sess.close()


def _story(
    session,
    title,
    *,
    source="hn",
    score=0.0,
    days_old=0,
    topic="ai",
    votes=0,
) -> Story:
    published = _BASE + timedelta(days=days_old)
    story = Story(
        title=title,
        url=f"https://example.com/{title.replace(' ', '-')}-{days_old}-{score}",
        source_name=source,
        topic=topic,
        computed_score=score,
        vote_count=votes,
        published_at=published,
        fetched_at=published,
    )
    session.add(story)
    session.commit()
    return story


def _topic_id(session, slug):
    return session.scalars(select(Topic).where(Topic.slug == slug)).first().id


# ---- preferences -----------------------------------------------------------


def test_get_preferences_creates_defaults(session):
    prefs = get_preferences(session, "alice")
    assert prefs["algorithm"] == "balanced"
    assert prefs["topic_weight"] == 0.5
    assert prefs["source_weight"] == 0.3
    assert prefs["recency_weight"] == 0.2
    assert prefs["min_score_threshold"] == 0.0


def test_set_preferences_partial_update(session):
    get_preferences(session, "alice")
    updated = set_preferences(session, "alice", algorithm="recent")
    assert updated["algorithm"] == "recent"
    # Untouched weights keep their defaults.
    assert updated["topic_weight"] == 0.5

    tuned = set_preferences(
        session, "alice", topic_weight=0.8, source_weight=0.1, recency_weight=0.1
    )
    assert tuned["topic_weight"] == 0.8
    assert tuned["algorithm"] == "recent"


def test_set_preferences_rejects_unknown_algorithm(session):
    with pytest.raises(RecommendationError):
        set_preferences(session, "alice", algorithm="psychic")


def test_set_preferences_rejects_all_zero_weights(session):
    with pytest.raises(RecommendationError):
        set_preferences(
            session,
            "alice",
            topic_weight=0.0,
            source_weight=0.0,
            recency_weight=0.0,
        )


def test_set_preferences_rejects_negative_weight(session):
    with pytest.raises(RecommendationError):
        set_preferences(session, "alice", topic_weight=-1.0)


def test_empty_user_id_raises(session):
    with pytest.raises(RecommendationError):
        get_preferences(session, "  ")
    with pytest.raises(RecommendationError):
        personalized_feed(session, "")


# ---- profile ---------------------------------------------------------------


def test_build_profile_from_follows_and_votes(session):
    s1 = _story(session, "GPT breakthrough", source="hn")
    tag_story(session, s1.id, ["llms"])
    cast_vote(session, s1.id, "alice", 1)
    follow_topic(session, "alice", "robotics")

    profile = build_profile(session, "alice")
    assert s1.id in profile["upvoted_story_ids"]
    assert profile["source_counts"]["hn"] == 1
    # topic_ids unions followed (robotics) with upvoted-story topics (llms).
    assert _topic_id(session, "llms") in profile["topic_ids"]
    assert _topic_id(session, "robotics") in profile["topic_ids"]


# ---- feed ranking ----------------------------------------------------------


def test_feed_ranks_followed_topic_first(session):
    follow_topic(session, "alice", "llms")
    match = _story(session, "New language model", score=1.0, days_old=0)
    other = _story(session, "Random rocket news", score=5.0, days_old=1, topic="aero")
    tag_story(session, match.id, ["llms"])
    tag_story(session, other.id, ["launch-systems"])

    feed = personalized_feed(session, "alice", algorithm="balanced")
    ids = [s["id"] for s in feed["stories"]]
    assert ids[0] == match.id
    assert feed["total"] == 2


def test_feed_respects_min_score_threshold(session):
    follow_topic(session, "alice", "llms")
    low = _story(session, "Low score model", score=0.5)
    high = _story(session, "High score model", score=9.0, days_old=1)
    tag_story(session, low.id, ["llms"])
    tag_story(session, high.id, ["llms"])
    set_preferences(session, "alice", min_score_threshold=1.0)

    feed = personalized_feed(session, "alice")
    ids = [s["id"] for s in feed["stories"]]
    assert high.id in ids
    assert low.id not in ids


def test_upvoted_content_influences_and_is_excluded(session):
    # Alice upvotes a satellites story; a *different* satellites story should
    # then rank above an unrelated one, and the upvoted story itself drops out.
    upvoted = _story(session, "Starlink launch", topic="aero", days_old=2)
    tag_story(session, upvoted.id, ["satellites"])
    cast_vote(session, upvoted.id, "alice", 1)

    related = _story(session, "New satellite constellation", topic="aero", days_old=0)
    tag_story(session, related.id, ["satellites"])
    unrelated = _story(session, "Chatbot update", days_old=1)
    tag_story(session, unrelated.id, ["llms"])

    feed = personalized_feed(session, "alice", algorithm="balanced")
    ids = [s["id"] for s in feed["stories"]]
    assert upvoted.id not in ids
    assert ids[0] == related.id


def test_feed_pagination_no_duplicates(session):
    follow_topic(session, "alice", "llms")
    stories = []
    for i in range(5):
        s = _story(session, f"Model {i}", score=float(i), days_old=i)
        tag_story(session, s.id, ["llms"])
        stories.append(s)

    page1 = personalized_feed(session, "alice", limit=2, offset=0)
    page2 = personalized_feed(session, "alice", limit=2, offset=2)
    page3 = personalized_feed(session, "alice", limit=2, offset=4)
    seen = (
        [s["id"] for s in page1["stories"]]
        + [s["id"] for s in page2["stories"]]
        + [s["id"] for s in page3["stories"]]
    )
    assert page1["total"] == 5
    assert len(seen) == 5
    assert len(set(seen)) == 5


def test_followed_algorithm_filters_to_followed_topics(session):
    follow_topic(session, "alice", "llms")
    match = _story(session, "Language model", days_old=0)
    tag_story(session, match.id, ["llms"])
    off_topic = _story(session, "Rocket engine", days_old=1, topic="aero")
    tag_story(session, off_topic.id, ["propulsion"])

    feed = personalized_feed(session, "alice", algorithm="followed")
    ids = [s["id"] for s in feed["stories"]]
    assert ids == [match.id]


def test_followed_algorithm_empty_when_no_interests(session):
    _story(session, "Some story")
    feed = personalized_feed(session, "bob", algorithm="followed")
    assert feed["total"] == 0
    assert feed["stories"] == []


def test_recent_algorithm_orders_by_recency(session):
    follow_topic(session, "alice", "llms")
    old = _story(session, "Old model", score=9.0, days_old=0)
    new = _story(session, "New model", score=0.1, days_old=10)
    tag_story(session, old.id, ["llms"])
    tag_story(session, new.id, ["llms"])

    feed = personalized_feed(session, "alice", algorithm="recent")
    assert feed["stories"][0]["id"] == new.id


def test_trending_algorithm_favours_votes(session):
    popular = _story(session, "Hyped model", days_old=0, votes=100)
    quiet = _story(session, "Quiet model", days_old=1, votes=0)
    tag_story(session, popular.id, ["llms"])
    tag_story(session, quiet.id, ["llms"])

    feed = personalized_feed(session, "carol", algorithm="trending")
    assert feed["stories"][0]["id"] == popular.id


def test_invalid_algorithm_in_feed_raises(session):
    with pytest.raises(RecommendationError):
        personalized_feed(session, "alice", algorithm="bogus")


def test_empty_database_returns_empty_feed(session):
    feed = personalized_feed(session, "alice")
    assert feed["total"] == 0
    assert feed["stories"] == []


# ---- API -------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # A file-backed DB (not in-memory) so the TestClient's request thread sees
    # the same schema/data, mirroring tests/test_topics.py.
    db_path = tmp_path / "recommendation.db"
    eng = get_engine(f"sqlite:///{db_path}")
    init_db(eng)
    monkeypatch.setattr(api, "_engine", eng)
    monkeypatch.setattr(api, "get_session", lambda e=None: get_session(eng))
    sess = get_session(eng)
    seed_topics(sess)
    match = _story(sess, "Language model news", days_old=0)
    match_id = match.id
    tag_story(sess, match_id, ["llms"])
    follow_topic(sess, "alice", "llms")
    sess.close()
    with TestClient(api.app) as test_client:
        test_client._match_story_id = match_id
        yield test_client
    eng.dispose()


def test_api_anonymous_feed_is_empty(client):
    client.cookies.clear()
    resp = client.get("/api/user/feed")
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] is None
    assert body["stories"] == []


def test_api_feed_personalized_for_logged_in_user(client):
    client.cookies.set("voter_id", "alice")
    resp = client.get("/api/user/feed")
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "alice"
    assert body["stories"][0]["id"] == client._match_story_id


def test_api_preferences_roundtrip(client):
    client.cookies.set("voter_id", "dave")
    set_resp = client.post(
        "/api/user/preferences",
        json={"algorithm": "trending", "min_score_threshold": 2.0},
    )
    assert set_resp.status_code == 200
    assert set_resp.json()["algorithm"] == "trending"

    get_resp = client.get("/api/user/preferences")
    assert get_resp.json()["algorithm"] == "trending"
    assert get_resp.json()["min_score_threshold"] == 2.0


def test_api_preferences_rejects_bad_algorithm(client):
    client.cookies.set("voter_id", "dave")
    resp = client.post("/api/user/preferences", json={"algorithm": "nope"})
    assert resp.status_code == 400
