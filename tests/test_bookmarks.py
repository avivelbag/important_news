"""Tests for the bookmark service, bookmark API, and generated bookmark UI."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import src.api as api
from src.bookmarks import (
    BookmarkError,
    bulk_remove_bookmarks,
    is_bookmarked,
    list_bookmarks,
    remove_bookmark,
    toggle_bookmark,
)
from src.db import get_engine, get_session, init_db
from src.models import Bookmark, Story


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


def test_toggle_creates_bookmark_and_bumps_count(session):
    story = _make_story(session)
    result = toggle_bookmark(session, story.id, "user-a")
    assert result["bookmarked"] is True
    assert result["bookmark_count"] == 1
    assert story.bookmark_count == 1
    assert is_bookmarked(session, story.id, "user-a") is True


def test_toggle_twice_removes_bookmark(session):
    story = _make_story(session)
    toggle_bookmark(session, story.id, "user-a")
    result = toggle_bookmark(session, story.id, "user-a")
    assert result["bookmarked"] is False
    assert result["bookmark_count"] == 0
    assert story.bookmark_count == 0
    assert is_bookmarked(session, story.id, "user-a") is False


def test_count_reflects_distinct_users(session):
    story = _make_story(session)
    toggle_bookmark(session, story.id, "user-a")
    toggle_bookmark(session, story.id, "user-b")
    assert story.bookmark_count == 2


def test_unique_constraint_prevents_duplicates(session):
    story = _make_story(session)
    toggle_bookmark(session, story.id, "user-a")
    # A second toggle removes it; re-adding then trying a manual duplicate insert
    # must violate the (user_id, story_id) unique constraint.
    toggle_bookmark(session, story.id, "user-a")
    toggle_bookmark(session, story.id, "user-a")
    session.add(
        Bookmark(
            user_id="user-a",
            story_id=story.id,
            created_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
        )
    )
    with pytest.raises(Exception):
        session.commit()
    session.rollback()


# --- service: remove / bulk -------------------------------------------------


def test_remove_is_idempotent_noop_when_absent(session):
    story = _make_story(session)
    result = remove_bookmark(session, story.id, "user-a")
    assert result["bookmarked"] is False
    assert result["bookmark_count"] == 0


def test_remove_deletes_existing_bookmark(session):
    story = _make_story(session)
    toggle_bookmark(session, story.id, "user-a")
    result = remove_bookmark(session, story.id, "user-a")
    assert result["bookmarked"] is False
    assert story.bookmark_count == 0


def test_bulk_remove_only_targets_caller(session):
    s1 = _make_story(session, url="https://example.com/1")
    s2 = _make_story(session, url="https://example.com/2")
    toggle_bookmark(session, s1.id, "user-a")
    toggle_bookmark(session, s2.id, "user-a")
    toggle_bookmark(session, s1.id, "user-b")
    result = bulk_remove_bookmarks(session, [s1.id, s2.id], "user-a")
    assert result["removed"] == 2
    # user-b's bookmark on s1 survives.
    assert is_bookmarked(session, s1.id, "user-b") is True
    assert s1.bookmark_count == 1


def test_bulk_remove_empty_list_is_noop(session):
    assert bulk_remove_bookmarks(session, [], "user-a") == {"removed": 0}


# --- service: listing / privacy ---------------------------------------------


def test_list_returns_only_callers_bookmarks(session):
    s1 = _make_story(session, url="https://example.com/1")
    s2 = _make_story(session, url="https://example.com/2")
    toggle_bookmark(session, s1.id, "user-a")
    toggle_bookmark(session, s2.id, "user-b")
    listing = list_bookmarks(session, "user-a")
    assert listing["total"] == 1
    assert [it["story_id"] for it in listing["items"]] == [s1.id]


def test_list_filters_by_category(session):
    s1 = _make_story(session, url="https://example.com/ai", topic="ai")
    s2 = _make_story(session, url="https://example.com/aero", topic="aerospace")
    toggle_bookmark(session, s1.id, "user-a")
    toggle_bookmark(session, s2.id, "user-a")
    listing = list_bookmarks(session, "user-a", category="aerospace")
    assert listing["total"] == 1
    assert listing["items"][0]["story_id"] == s2.id


def test_list_empty_for_user_with_no_bookmarks(session):
    listing = list_bookmarks(session, "nobody")
    assert listing["total"] == 0
    assert listing["items"] == []


# --- service: failure modes -------------------------------------------------


def test_toggle_unknown_story_raises_not_found(session):
    with pytest.raises(BookmarkError) as exc:
        toggle_bookmark(session, 99999, "user-a")
    assert exc.value.not_found is True


def test_toggle_empty_user_raises(session):
    story = _make_story(session)
    with pytest.raises(BookmarkError) as exc:
        toggle_bookmark(session, story.id, "   ")
    assert exc.value.not_found is False


def test_list_invalid_page_raises(session):
    with pytest.raises(BookmarkError):
        list_bookmarks(session, "user-a", page=0)


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


def test_api_toggle_sets_cookie_and_bookmarks(client, api_engine):
    story_id = _seed_story(api_engine)
    resp = client.post(f"/api/articles/{story_id}/bookmark")
    assert resp.status_code == 200
    body = resp.json()
    assert body["bookmarked"] is True
    assert body["bookmark_count"] == 1
    assert "voter_id" in resp.cookies


def test_api_toggle_persists_across_requests(client, api_engine):
    story_id = _seed_story(api_engine, url="https://example.com/persist")
    client.post(f"/api/articles/{story_id}/bookmark")
    listing = client.get("/api/user/bookmarks")
    assert listing.status_code == 200
    data = listing.json()
    assert data["total"] == 1
    assert data["items"][0]["story_id"] == story_id


def test_api_bookmarks_private_to_user(client, api_engine):
    story_id = _seed_story(api_engine, url="https://example.com/priv")
    # user A bookmarks via the shared client (carries a voter_id cookie).
    client.post(f"/api/articles/{story_id}/bookmark")
    # A second, cookieless client sees an empty list — not user A's saves.
    other = TestClient(api.app)
    resp = other.get("/api/user/bookmarks")
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_api_toggle_unknown_story_is_404(client, api_engine):
    resp = client.post("/api/articles/99999/bookmark")
    assert resp.status_code == 404


def test_api_delete_without_cookie_is_400(client, api_engine):
    story_id = _seed_story(api_engine, url="https://example.com/del")
    other = TestClient(api.app)
    resp = other.delete(f"/api/articles/{story_id}/bookmark")
    assert resp.status_code == 400


def test_api_bulk_delete_removes_selected(client, api_engine):
    s1 = _seed_story(api_engine, url="https://example.com/b1")
    s2 = _seed_story(api_engine, url="https://example.com/b2")
    client.post(f"/api/articles/{s1}/bookmark")
    client.post(f"/api/articles/{s2}/bookmark")
    resp = client.post(
        "/api/user/bookmarks/bulk-delete", json={"story_ids": [s1, s2]}
    )
    assert resp.status_code == 200
    assert resp.json()["removed"] == 2
    assert client.get("/api/user/bookmarks").json()["total"] == 0


def test_api_bulk_delete_bad_payload_is_400(client, api_engine):
    resp = client.post("/api/user/bookmarks/bulk-delete", json={"story_ids": "nope"})
    assert resp.status_code == 400


# --- generated site ---------------------------------------------------------


def test_render_story_has_bookmark_button(session):
    from src.generate_site import render_story

    story = _make_story(session, bookmark_count=4)
    html = render_story(story, 1)
    assert 'class="bookmark-toggle"' in html
    assert 'class="bookmark-count">4' in html


def test_bookmarks_page_and_js_emitted(tmp_path, engine):
    from src.generate_site import generate_site

    out = generate_site(engine, tmp_path)
    assert (out / "bookmarks.html").exists()
    assert (out / "bookmark.js").exists()
    assert 'href="bookmarks.html"' in (out / "index.html").read_text()
