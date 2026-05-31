"""Tests for the comment service, comment API, and comment-aware site output."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import src.api as api
from src.comments import (
    DELETED_BODY,
    CommentBadInput,
    CommentError,
    CommentNotFound,
    delete_comment,
    get_thread,
    post_comment,
    vote_comment,
)
from src.db import get_engine, get_session, init_db
from src.models import Story


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


# --- service: posting + threading -------------------------------------------


def test_post_top_level_comment(session):
    story = _make_story(session)
    c = post_comment(session, story.id, "first post", user_id="u1")
    assert c.id is not None
    assert c.parent_comment_id is None
    assert c.body == "first post"
    assert session.get(Story, story.id).comment_count == 1


def test_post_reply_builds_parent_child(session):
    story = _make_story(session)
    parent = post_comment(session, story.id, "parent", user_id="u1")
    reply = post_comment(
        session, story.id, "reply", user_id="u2", parent_comment_id=parent.id
    )
    assert reply.parent_comment_id == parent.id
    thread = get_thread(session, story.id)
    assert len(thread) == 1
    assert thread[0]["id"] == parent.id
    assert len(thread[0]["replies"]) == 1
    assert thread[0]["replies"][0]["id"] == reply.id


def test_thread_ordered_by_vote_count_desc(session):
    story = _make_story(session)
    low = post_comment(session, story.id, "low")
    high = post_comment(session, story.id, "high")
    vote_comment(session, high.id, 1)
    vote_comment(session, high.id, 1)
    thread = get_thread(session, story.id)
    assert [n["id"] for n in thread] == [high.id, low.id]


def test_replies_ordered_by_vote_within_parent(session):
    story = _make_story(session)
    parent = post_comment(session, story.id, "p")
    r1 = post_comment(session, story.id, "r1", parent_comment_id=parent.id)
    r2 = post_comment(session, story.id, "r2", parent_comment_id=parent.id)
    vote_comment(session, r2.id, 1)
    thread = get_thread(session, story.id)
    reply_ids = [r["id"] for r in thread[0]["replies"]]
    assert reply_ids == [r2.id, r1.id]


# --- service: soft delete ----------------------------------------------------


def test_soft_delete_shows_stub_and_keeps_replies(session):
    story = _make_story(session)
    parent = post_comment(session, story.id, "secret", user_id="alice")
    post_comment(session, story.id, "child", parent_comment_id=parent.id)
    delete_comment(session, parent.id)

    thread = get_thread(session, story.id)
    node = thread[0]
    assert node["deleted"] is True
    assert node["body"] == DELETED_BODY
    assert node["user_id"] is None
    # The reply survives under the deleted stub.
    assert len(node["replies"]) == 1
    assert node["replies"][0]["body"] == "child"
    # Deleted comments drop out of the denormalised count.
    assert session.get(Story, story.id).comment_count == 1


def test_double_delete_is_noop(session):
    story = _make_story(session)
    c = post_comment(session, story.id, "x")
    delete_comment(session, c.id)
    again = delete_comment(session, c.id)
    assert again.deleted is True
    assert session.get(Story, story.id).comment_count == 0


# --- service: voting ---------------------------------------------------------


def test_vote_comment_adjusts_count(session):
    story = _make_story(session)
    c = post_comment(session, story.id, "x")
    assert vote_comment(session, c.id, 1) == 1
    assert vote_comment(session, c.id, 1) == 2
    assert vote_comment(session, c.id, -1) == 1


def test_vote_comment_invalid_value_raises(session):
    story = _make_story(session)
    c = post_comment(session, story.id, "x")
    with pytest.raises(CommentError):
        vote_comment(session, c.id, 0)
    with pytest.raises(CommentError):
        vote_comment(session, c.id, 2)


# --- service: failure modes --------------------------------------------------


def test_empty_body_raises(session):
    story = _make_story(session)
    with pytest.raises(CommentError):
        post_comment(session, story.id, "   ")


def test_oversize_body_raises(session):
    story = _make_story(session)
    with pytest.raises(CommentError):
        post_comment(session, story.id, "x" * 10_001)


def test_post_to_missing_story_raises(session):
    with pytest.raises(CommentError):
        post_comment(session, 99999, "hi")


def test_reply_to_missing_parent_raises(session):
    story = _make_story(session)
    with pytest.raises(CommentError):
        post_comment(session, story.id, "hi", parent_comment_id=99999)


def test_reply_across_stories_raises(session):
    a = _make_story(session, url="https://example.com/a")
    b = _make_story(session, url="https://example.com/b")
    parent = post_comment(session, a.id, "on a")
    with pytest.raises(CommentError):
        post_comment(session, b.id, "wrong", parent_comment_id=parent.id)


def test_delete_missing_comment_raises(session):
    with pytest.raises(CommentError):
        delete_comment(session, 99999)


def test_thread_missing_story_raises(session):
    with pytest.raises(CommentError):
        get_thread(session, 99999)


def test_empty_thread_returns_empty_list(session):
    story = _make_story(session)
    assert get_thread(session, story.id) == []


# --- API ---------------------------------------------------------------------


@pytest.fixture()
def api_engine(tmp_path, monkeypatch):
    eng = get_engine(f"sqlite:///{tmp_path / 'capi.db'}")
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


def test_api_post_and_get_comments(client, api_engine):
    sid = _seed_story(api_engine)
    resp = client.post("/api/comments", json={"story_id": sid, "body": "hello"})
    assert resp.status_code == 201
    assert "voter_id" in resp.cookies
    cid = resp.json()["id"]

    thread = client.get(f"/api/articles/{sid}/comments")
    assert thread.status_code == 200
    body = thread.json()
    assert len(body) == 1
    assert body[0]["id"] == cid
    assert body[0]["body"] == "hello"


def test_api_post_reply_via_article_alias(client, api_engine):
    sid = _seed_story(api_engine, url="https://example.com/alias")
    parent = client.post("/api/comments", json={"article_id": sid, "body": "p"}).json()
    reply = client.post(
        "/api/comments",
        json={"article_id": sid, "body": "r", "parent_comment_id": parent["id"]},
    )
    assert reply.status_code == 201
    assert reply.json()["parent_comment_id"] == parent["id"]


def test_api_delete_comment_shows_stub(client, api_engine):
    sid = _seed_story(api_engine, url="https://example.com/del")
    cid = client.post("/api/comments", json={"story_id": sid, "body": "x"}).json()["id"]
    resp = client.delete(f"/api/comments/{cid}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    thread = client.get(f"/api/articles/{sid}/comments").json()
    assert thread[0]["body"] == DELETED_BODY


def test_api_vote_comment(client, api_engine):
    sid = _seed_story(api_engine, url="https://example.com/cvote")
    cid = client.post("/api/comments", json={"story_id": sid, "body": "x"}).json()["id"]
    resp = client.post(f"/api/comments/{cid}/vote", json={"vote_value": 1})
    assert resp.status_code == 200
    assert resp.json()["vote_count"] == 1


def test_api_post_empty_body_is_400(client, api_engine):
    sid = _seed_story(api_engine, url="https://example.com/empty")
    resp = client.post("/api/comments", json={"story_id": sid, "body": "  "})
    assert resp.status_code == 400


def test_api_post_missing_story_is_404(client, api_engine):
    resp = client.post("/api/comments", json={"story_id": 99999, "body": "x"})
    assert resp.status_code == 404


def test_api_get_comments_missing_story_is_404(client, api_engine):
    resp = client.get("/api/articles/99999/comments")
    assert resp.status_code == 404


def test_api_vote_comment_missing_is_404(client, api_engine):
    resp = client.post("/api/comments/99999/vote", json={"vote_value": 1})
    assert resp.status_code == 404


def test_api_delete_missing_comment_is_404(client, api_engine):
    resp = client.delete("/api/comments/99999")
    assert resp.status_code == 404


# --- generated site ----------------------------------------------------------


def test_render_story_shows_comment_count(session):
    from src.generate_site import render_story

    story = _make_story(session)
    post_comment(session, story.id, "a")
    post_comment(session, story.id, "b")
    session.refresh(story)
    html = render_story(story, 1)
    assert "2 comments" in html
    assert 'class="comments"' in html
    assert "comments-toggle" in html


def test_render_story_singular_comment_label(session):
    from src.generate_site import render_story

    story = _make_story(session)
    post_comment(session, story.id, "only")
    session.refresh(story)
    assert "1 comment" in render_story(story, 1)


def test_comments_js_written_and_linked(session, engine, tmp_path):
    from src.generate_site import generate_site

    _make_story(session, url="https://a/site")
    out = generate_site(engine=engine, out_dir=tmp_path / "docs")
    index = (out / "index.html").read_text(encoding="utf-8")
    js = (out / "comments.js").read_text(encoding="utf-8")
    assert '<script src="comments.js"></script>' in index
    assert "/api/articles/" in js
    assert "/api/comments" in js


# --- reviewer follow-ups -----------------------------------------------------


def test_comments_js_renders_reply_affordances():
    from src.generate_site import render_comments_js

    js = render_comments_js()
    assert "comment-reply-toggle" in js
    assert "comment-reply-form" in js
    assert "data-parent-id" in js
    assert "parent_comment_id" in js


def test_comments_js_reply_toggle_wired_to_parent():
    from src.generate_site import render_comments_js

    js = render_comments_js()
    # The reply form carries the comment id as its parent and submit reads it.
    assert "data-parent-id" in js
    assert 'form.getAttribute("data-parent-id")' in js
    assert "payload.parent_comment_id = Number(parent)" in js


def test_not_found_subclasses_set_flag():
    assert CommentNotFound("x").not_found is True
    assert CommentBadInput("x").not_found is False
    assert isinstance(CommentNotFound("x"), CommentError)
    assert isinstance(CommentBadInput("x"), CommentError)


def test_post_comment_raises_typed_errors(session):
    story = _make_story(session)
    with pytest.raises(CommentBadInput):
        post_comment(session, story.id, "   ")
    with pytest.raises(CommentNotFound):
        post_comment(session, 999999, "hi")


def test_comment_status_uses_flag_not_message():
    from src.api import _comment_status

    # A not-found error worded WITHOUT "does not exist" still maps to 404.
    assert _comment_status(CommentNotFound("unknown id 5")) == 404
    # A bad-input error that happens to contain "does not exist" stays 400.
    assert _comment_status(CommentBadInput("value does not exist here")) == 400
