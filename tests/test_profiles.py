"""Tests for the user profile, activity history, karma, and leaderboard service."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import src.api as api
from src.comments import post_comment, vote_comment
from src.db import get_engine, get_session, init_db
from src.models import Story
from src.profiles import (
    ProfileError,
    compute_karma,
    get_or_create_profile,
    get_profile,
    get_user_articles,
    get_user_comments,
    leaderboard,
    refresh_profile_stats,
    set_private,
)
from src.voting import cast_vote

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


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
        published_at=NOW,
        fetched_at=NOW,
    )
    base.update(kwargs)
    story = Story(**base)
    session.add(story)
    session.commit()
    return story


# --- karma ------------------------------------------------------------------


def test_compute_karma_sums_comment_votes(session):
    story = _make_story(session)
    c1 = post_comment(session, story.id, "first", user_id="alice")
    c2 = post_comment(session, story.id, "second", user_id="alice")
    c1.vote_count = 5
    c2.vote_count = 3
    session.commit()
    assert compute_karma(session, "alice") == 8


def test_compute_karma_ignores_deleted_and_others(session):
    story = _make_story(session)
    a = post_comment(session, story.id, "a", user_id="alice")
    a.vote_count = 4
    b = post_comment(session, story.id, "b", user_id="alice")
    b.vote_count = 10
    b.deleted = True
    post_comment(session, story.id, "c", user_id="bob")
    session.commit()
    assert compute_karma(session, "alice") == 4


def test_compute_karma_zero_for_unknown_user(session):
    assert compute_karma(session, "ghost") == 0


def test_vote_comment_refreshes_author_karma(session):
    story = _make_story(session)
    c = post_comment(session, story.id, "hi", user_id="alice")
    vote_comment(session, c.id, 1)
    # The vote action created the profile row and cached the author's karma.
    refreshed = get_profile(session, "alice")
    assert refreshed["karma"] == 1


# --- profile stats ----------------------------------------------------------


def test_refresh_profile_stats_caches_all_counts(session):
    story = _make_story(session, submitted_by="alice")
    other = _make_story(session, url="https://example.com/other")
    cast_vote(session, other.id, "alice", 1)
    c = post_comment(session, story.id, "comment", user_id="alice")
    c.vote_count = 2
    session.commit()

    profile = refresh_profile_stats(session, "alice")
    assert profile.karma == 2
    assert profile.submission_count == 1
    assert profile.vote_count == 1
    assert profile.comment_count == 1


def test_get_or_create_profile_is_idempotent(session):
    p1 = get_or_create_profile(session, "alice")
    p2 = get_or_create_profile(session, "alice")
    assert p1.id == p2.id


def test_get_or_create_profile_rejects_empty(session):
    with pytest.raises(ProfileError):
        get_or_create_profile(session, "   ")


# --- get_profile ------------------------------------------------------------


def test_get_profile_public_returns_stats(session):
    story = _make_story(session, submitted_by="alice")
    post_comment(session, story.id, "c", user_id="alice")
    refresh_profile_stats(session, "alice")
    data = get_profile(session, "alice")
    assert data["is_private"] is False
    assert data["submission_count"] == 1
    assert data["comment_count"] == 1


def test_get_profile_private_hides_activity(session):
    story = _make_story(session)
    post_comment(session, story.id, "secret", user_id="alice")
    refresh_profile_stats(session, "alice")
    set_private(session, "alice", True)
    data = get_profile(session, "alice")
    assert data == {"username": "alice", "is_private": True}


def test_get_profile_builds_from_activity_without_row(session):
    story = _make_story(session)
    post_comment(session, story.id, "c", user_id="lazyloaded")
    data = get_profile(session, "lazyloaded")
    assert data["comment_count"] == 1


def test_get_profile_unknown_user_raises(session):
    with pytest.raises(ProfileError):
        get_profile(session, "nobody")


# --- activity history -------------------------------------------------------


def test_get_user_articles_combines_submitted_and_upvoted(session):
    submitted = _make_story(
        session, submitted_by="alice", url="https://example.com/sub"
    )
    upvoted = _make_story(session, url="https://example.com/up")
    cast_vote(session, upvoted.id, "alice", 1)
    result = get_user_articles(session, "alice")
    activities = {i["story_id"]: i["activity"] for i in result["items"]}
    assert activities[submitted.id] == "submitted"
    assert activities[upvoted.id] == "upvoted"
    assert result["total"] == 2
    assert all(i["timestamp"] is not None for i in result["items"])


def test_get_user_articles_paginates(session):
    for i in range(5):
        s = _make_story(session, url=f"https://example.com/a{i}")
        cast_vote(session, s.id, "alice", 1)
    page1 = get_user_articles(session, "alice", page=1, per_page=2)
    page2 = get_user_articles(session, "alice", page=2, per_page=2)
    assert page1["total"] == 5
    assert len(page1["items"]) == 2
    assert len(page2["items"]) == 2
    ids1 = {i["story_id"] for i in page1["items"]}
    ids2 = {i["story_id"] for i in page2["items"]}
    assert ids1.isdisjoint(ids2)


def test_get_user_articles_rejects_bad_page(session):
    story = _make_story(session)
    cast_vote(session, story.id, "alice", 1)
    with pytest.raises(ProfileError):
        get_user_articles(session, "alice", page=0)


def test_get_user_articles_private_raises(session):
    story = _make_story(session)
    cast_vote(session, story.id, "alice", 1)
    set_private(session, "alice", True)
    with pytest.raises(ProfileError):
        get_user_articles(session, "alice")


def test_get_user_comments_newest_first_and_excludes_deleted(session):
    story = _make_story(session)
    c1 = post_comment(session, story.id, "old", user_id="alice")
    c1.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    c2 = post_comment(session, story.id, "new", user_id="alice")
    c2.created_at = datetime(2026, 2, 1, tzinfo=timezone.utc)
    c3 = post_comment(session, story.id, "gone", user_id="alice")
    c3.deleted = True
    session.commit()
    result = get_user_comments(session, "alice")
    bodies = [i["body"] for i in result["items"]]
    assert bodies == ["new", "old"]
    assert result["total"] == 2


def test_get_user_comments_empty_for_user_with_only_votes(session):
    story = _make_story(session)
    cast_vote(session, story.id, "alice", 1)
    result = get_user_comments(session, "alice")
    assert result["total"] == 0
    assert result["items"] == []


# --- leaderboard ------------------------------------------------------------


def test_leaderboard_orders_by_karma(session):
    story = _make_story(session)
    for name, votes in [("alice", 5), ("bob", 10), ("carol", 1)]:
        c = post_comment(session, story.id, f"by {name}", user_id=name)
        c.vote_count = votes
        session.commit()
        refresh_profile_stats(session, name)
    board = leaderboard(session, limit=10)
    assert [e["username"] for e in board] == ["bob", "alice", "carol"]
    assert board[0]["rank"] == 1


def test_leaderboard_excludes_private(session):
    story = _make_story(session)
    for name, votes in [("alice", 5), ("bob", 10)]:
        c = post_comment(session, story.id, f"by {name}", user_id=name)
        c.vote_count = votes
        session.commit()
        refresh_profile_stats(session, name)
    set_private(session, "bob", True)
    board = leaderboard(session, limit=10)
    assert [e["username"] for e in board] == ["alice"]


def test_leaderboard_rejects_bad_limit(session):
    with pytest.raises(ProfileError):
        leaderboard(session, limit=0)


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


def _seed(engine):
    sess = get_session(engine)
    try:
        story = Story(
            title="API story",
            url="https://example.com/api",
            source_name="hn",
            topic="ai",
            submitted_by="alice",
            published_at=NOW,
            fetched_at=NOW,
        )
        sess.add(story)
        sess.commit()
        c = post_comment(sess, story.id, "hello", user_id="alice")
        c.vote_count = 7
        sess.commit()
        refresh_profile_stats(sess, "alice")
        return story.id
    finally:
        sess.close()


def test_api_profile_returns_stats(client, api_engine):
    _seed(api_engine)
    resp = client.get("/api/users/alice")
    assert resp.status_code == 200
    body = resp.json()
    assert body["karma"] == 7
    assert body["submission_count"] == 1


def test_api_profile_unknown_is_404(client, api_engine):
    resp = client.get("/api/users/nobody")
    assert resp.status_code == 404


def test_api_leaderboard_route_not_shadowed(client, api_engine):
    _seed(api_engine)
    resp = client.get("/api/users/leaderboard")
    assert resp.status_code == 200
    board = resp.json()
    assert board[0]["username"] == "alice"


def test_api_user_articles_and_comments(client, api_engine):
    _seed(api_engine)
    articles = client.get("/api/users/alice/articles").json()
    assert articles["total"] >= 1
    comments = client.get("/api/users/alice/comments").json()
    assert comments["total"] == 1
    assert comments["items"][0]["vote_count"] == 7


def test_api_privacy_toggle_hides_profile(client, api_engine):
    _seed(api_engine)
    resp = client.post("/api/users/alice/privacy", json={"is_private": True})
    assert resp.status_code == 200
    assert resp.json()["is_private"] is True
    profile = client.get("/api/users/alice").json()
    assert profile == {"username": "alice", "is_private": True}
    # A private profile is dropped from the leaderboard.
    board = client.get("/api/users/leaderboard").json()
    assert all(e["username"] != "alice" for e in board)


def test_api_privacy_requires_bool(client, api_engine):
    _seed(api_engine)
    resp = client.post("/api/users/alice/privacy", json={"is_private": "yes"})
    assert resp.status_code == 400
