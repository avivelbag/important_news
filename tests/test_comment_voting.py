"""Tests for per-user comment voting: scoring, dedup, toggle, collapse, sort."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import src.api as api
from src.comments import (
    CommentBadInput,
    CommentNotFound,
    cast_comment_vote,
    get_comment_votes,
    get_thread,
    post_comment,
)
from src.db import get_engine, get_session, init_db
from src.models import CommentVote, Story


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


# --- service: scoring + dedup ------------------------------------------------


def test_upvote_increments_score(session):
    story = _make_story(session)
    c = post_comment(session, story.id, "hi", user_id="author")
    state = cast_comment_vote(session, c.id, "voter", 1)
    assert state["score"] == 1
    assert state["upvotes"] == 1
    assert state["downvotes"] == 0
    assert state["user_vote"] == 1


def test_score_is_upvotes_minus_downvotes(session):
    story = _make_story(session)
    c = post_comment(session, story.id, "hi", user_id="author")
    cast_comment_vote(session, c.id, "u1", 1)
    cast_comment_vote(session, c.id, "u2", 1)
    cast_comment_vote(session, c.id, "u3", -1)
    state = get_comment_votes(session, c.id)
    assert state["upvotes"] == 2
    assert state["downvotes"] == 1
    assert state["score"] == 1
    assert session.get(type(c), c.id).vote_count == 1


def test_duplicate_vote_updates_single_row(session):
    story = _make_story(session)
    c = post_comment(session, story.id, "hi", user_id="author")
    cast_comment_vote(session, c.id, "voter", 1)
    cast_comment_vote(session, c.id, "voter", 1)
    rows = session.scalars(
        select(CommentVote).where(CommentVote.comment_id == c.id)
    ).all()
    assert len(rows) == 1
    assert get_comment_votes(session, c.id)["score"] == 1


def test_changing_vote_flips_direction(session):
    story = _make_story(session)
    c = post_comment(session, story.id, "hi", user_id="author")
    cast_comment_vote(session, c.id, "voter", 1)
    state = cast_comment_vote(session, c.id, "voter", -1)
    assert state["score"] == -1
    assert state["user_vote"] == -1
    rows = session.scalars(
        select(CommentVote).where(CommentVote.comment_id == c.id)
    ).all()
    assert len(rows) == 1


# --- service: toggle / reversal ----------------------------------------------


def test_zero_vote_removes_existing(session):
    story = _make_story(session)
    c = post_comment(session, story.id, "hi", user_id="author")
    cast_comment_vote(session, c.id, "voter", 1)
    state = cast_comment_vote(session, c.id, "voter", 0)
    assert state["score"] == 0
    assert state["user_vote"] == 0
    assert (
        session.scalars(
            select(CommentVote).where(CommentVote.comment_id == c.id)
        ).all()
        == []
    )


def test_zero_vote_on_unvoted_is_noop(session):
    story = _make_story(session)
    c = post_comment(session, story.id, "hi", user_id="author")
    state = cast_comment_vote(session, c.id, "voter", 0)
    assert state["score"] == 0


# --- service: permissions / failure modes ------------------------------------


def test_cannot_vote_on_own_comment(session):
    story = _make_story(session)
    c = post_comment(session, story.id, "mine", user_id="author")
    with pytest.raises(CommentBadInput):
        cast_comment_vote(session, c.id, "author", 1)


def test_anonymous_comment_is_votable(session):
    story = _make_story(session)
    c = post_comment(session, story.id, "anon", user_id=None)
    state = cast_comment_vote(session, c.id, "voter", 1)
    assert state["score"] == 1


def test_vote_missing_comment_raises(session):
    with pytest.raises(CommentNotFound):
        cast_comment_vote(session, 99999, "voter", 1)


def test_invalid_vote_value_raises(session):
    story = _make_story(session)
    c = post_comment(session, story.id, "hi", user_id="author")
    with pytest.raises(CommentBadInput):
        cast_comment_vote(session, c.id, "voter", 2)


def test_empty_user_id_raises(session):
    story = _make_story(session)
    c = post_comment(session, story.id, "hi", user_id="author")
    with pytest.raises(CommentBadInput):
        cast_comment_vote(session, c.id, "", 1)


def test_get_votes_missing_comment_raises(session):
    with pytest.raises(CommentNotFound):
        get_comment_votes(session, 99999)


def test_get_votes_reports_user_state(session):
    story = _make_story(session)
    c = post_comment(session, story.id, "hi", user_id="author")
    cast_comment_vote(session, c.id, "voter", -1)
    assert get_comment_votes(session, c.id, "voter")["user_vote"] == -1
    # A different (or absent) user sees no personal vote.
    assert get_comment_votes(session, c.id, "other")["user_vote"] == 0
    assert get_comment_votes(session, c.id)["user_vote"] == 0


# --- collapse threshold ------------------------------------------------------


def test_low_score_comment_is_collapsed(session):
    story = _make_story(session)
    c = post_comment(session, story.id, "spam", user_id="author")
    cast_comment_vote(session, c.id, "u1", -1)
    cast_comment_vote(session, c.id, "u2", -1)
    node = get_thread(session, story.id)[0]
    assert node["score"] == -2
    assert node["collapsed"] is True


def test_mildly_negative_comment_not_collapsed(session):
    story = _make_story(session)
    c = post_comment(session, story.id, "meh", user_id="author")
    cast_comment_vote(session, c.id, "u1", -1)
    node = get_thread(session, story.id)[0]
    assert node["score"] == -1
    assert node["collapsed"] is False


# --- thread: user vote state + OP flag + sorting -----------------------------


def test_thread_includes_user_vote(session):
    story = _make_story(session)
    c = post_comment(session, story.id, "hi", user_id="author")
    cast_comment_vote(session, c.id, "voter", 1)
    node = get_thread(session, story.id, user_id="voter")[0]
    assert node["user_vote"] == 1
    # Without a user_id the personal vote defaults to 0.
    assert get_thread(session, story.id)[0]["user_vote"] == 0


def test_thread_flags_op_comment(session):
    story = _make_story(session, submitted_by="op_user")
    op = post_comment(session, story.id, "by op", user_id="op_user")
    other = post_comment(session, story.id, "by other", user_id="someone")
    nodes = {n["id"]: n for n in get_thread(session, story.id)}
    assert nodes[op.id]["is_op"] is True
    assert nodes[other.id]["is_op"] is False


def test_thread_sort_newest_and_oldest(session):
    story = _make_story(session)
    first = post_comment(session, story.id, "first", user_id="a")
    first.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    second = post_comment(session, story.id, "second", user_id="b")
    second.created_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
    session.commit()

    newest = [n["id"] for n in get_thread(session, story.id, sort="newest")]
    oldest = [n["id"] for n in get_thread(session, story.id, sort="oldest")]
    assert newest == [second.id, first.id]
    assert oldest == [first.id, second.id]


def test_thread_sort_by_score(session):
    story = _make_story(session)
    low = post_comment(session, story.id, "low", user_id="a")
    high = post_comment(session, story.id, "high", user_id="b")
    cast_comment_vote(session, high.id, "v1", 1)
    cast_comment_vote(session, high.id, "v2", 1)
    ids = [n["id"] for n in get_thread(session, story.id, sort="score")]
    assert ids == [high.id, low.id]


def test_thread_invalid_sort_raises(session):
    story = _make_story(session)
    post_comment(session, story.id, "x")
    with pytest.raises(CommentBadInput):
        get_thread(session, story.id, sort="bananas")


# --- API ---------------------------------------------------------------------


@pytest.fixture()
def api_engine(tmp_path, monkeypatch):
    eng = get_engine(f"sqlite:///{tmp_path / 'cv.db'}")
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


def _post_comment_as_other(engine, story_id: int, body: str) -> int:
    """Insert a comment owned by a fixed non-client user so the client may vote."""
    sess = get_session(engine)
    try:
        from src.comments import post_comment as pc

        return pc(sess, story_id, body, user_id="other_author").id
    finally:
        sess.close()


def test_api_upvote_then_toggle_off(client, api_engine):
    sid = _seed_story(api_engine, url="https://example.com/up")
    cid = _post_comment_as_other(api_engine, sid, "hi")
    resp = client.post(f"/api/comments/{cid}/upvote")
    assert resp.status_code == 200
    body = resp.json()
    assert body["score"] == 1
    assert body["user_vote"] == 1
    # Re-clicking the same arrow toggles the vote off.
    again = client.post(f"/api/comments/{cid}/upvote").json()
    assert again["score"] == 0
    assert again["user_vote"] == 0


def test_api_downvote_and_votes_endpoint(client, api_engine):
    sid = _seed_story(api_engine, url="https://example.com/down")
    cid = _post_comment_as_other(api_engine, sid, "hi")
    client.post(f"/api/comments/{cid}/downvote")
    votes = client.get(f"/api/comments/{cid}/votes")
    assert votes.status_code == 200
    data = votes.json()
    assert data["score"] == -1
    assert data["user_vote"] == -1
    assert data["downvotes"] == 1


def test_api_cannot_vote_on_own_comment(client, api_engine):
    sid = _seed_story(api_engine, url="https://example.com/own")
    # Posting mints the client's voter_id cookie; the same client now owns it.
    cid = client.post("/api/comments", json={"story_id": sid, "body": "mine"}).json()[
        "id"
    ]
    resp = client.post(f"/api/comments/{cid}/upvote")
    assert resp.status_code == 400


def test_api_vote_missing_comment_is_404(client, api_engine):
    resp = client.post("/api/comments/99999/upvote")
    assert resp.status_code == 404


def test_api_votes_missing_comment_is_404(client, api_engine):
    resp = client.get("/api/comments/99999/votes")
    assert resp.status_code == 404


def test_api_comments_sort_param(client, api_engine):
    sid = _seed_story(api_engine, url="https://example.com/sort")
    resp = client.get(f"/api/articles/{sid}/comments?sort=newest")
    assert resp.status_code == 200


def test_api_comments_bad_sort_is_400(client, api_engine):
    sid = _seed_story(api_engine, url="https://example.com/badsort")
    resp = client.get(f"/api/articles/{sid}/comments?sort=nope")
    assert resp.status_code == 400


# --- generated assets --------------------------------------------------------


def test_comments_js_has_vote_and_collapse_affordances():
    from src.generate_site import render_comments_js

    js = render_comments_js()
    assert "cvote" in js
    assert "upvote" in js
    assert "downvote" in js
    assert "comment-collapse-toggle" in js
    assert "comment-sort-select" in js
    assert "comment-score" in js


def test_css_styles_collapsed_comments():
    from src.generate_site import render_css

    css = render_css()
    assert ".comment.collapsed" in css
    assert "button.cvote" in css
    assert ".comment-op" in css
