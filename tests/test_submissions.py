"""Tests for the user story submission service and its API."""

import datetime as dt
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import src.api as api
from src.db import get_engine, get_session, init_db
from src.models import Story, Submission, UserProfile
from src.submissions import (
    SUBMISSION_KARMA,
    SubmissionError,
    approve_submission,
    categorize,
    create_submission,
    find_duplicates,
    list_pending,
    reject_submission,
)

NOW = dt.datetime(2024, 6, 1, tzinfo=dt.timezone.utc)

ADMIN_HEADERS = {"X-Admin-Token": api.ADMIN_TOKEN}


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


def _seed_story(session, **kwargs) -> Story:
    base = dict(
        title="Existing Story",
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


# ---------------------------------------------------------------------------
# Auto-categorisation
# ---------------------------------------------------------------------------


def test_categorize_ai():
    assert categorize("New LLM beats GPT on reasoning") == "ai"


def test_categorize_aerospace():
    assert categorize("SpaceX Starship reaches orbit") == "aerospace"


def test_categorize_both():
    assert categorize("AI flight software for the new rocket") == "both"


def test_categorize_unknown():
    assert categorize("My thoughts on gardening") == "unknown"


def test_categorize_uses_url_host():
    assert categorize("Cool update", url="https://blog.nasa.gov/mission") == "aerospace"


def test_categorize_word_boundary_no_false_positive():
    # "ai" must not fire on words that merely contain it (e.g. "said", "rain").
    assert categorize("She said it would rain today") == "unknown"


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------


def test_find_duplicates_by_url(session):
    _seed_story(session, url="https://example.com/post/1")
    dupes = find_duplicates(
        session, "Totally different title", "https://www.example.com/post/1/"
    )
    assert len(dupes) == 1
    assert dupes[0]["reason"] == "url"
    assert dupes[0]["similarity"] == 1.0


def test_find_duplicates_by_title(session):
    _seed_story(session, title="OpenAI releases a new model today")
    dupes = find_duplicates(
        session, "OpenAI releases a new model today!", url="https://other.com/x"
    )
    assert len(dupes) == 1
    assert dupes[0]["reason"] == "title"
    assert dupes[0]["similarity"] >= 0.8


def test_find_duplicates_none_for_distinct(session):
    _seed_story(session, title="Apples and oranges", url="https://a.com/1")
    assert find_duplicates(session, "Quantum widgets", "https://b.com/2") == []


def test_find_duplicates_empty_db(session):
    assert find_duplicates(session, "Anything", "https://x.com/y") == []


def test_find_duplicates_flags_pending_submission_by_url(session):
    first = create_submission(
        session, "First submitter", url="https://example.com/race", user_id="alice"
    )
    dupes = find_duplicates(session, "Second submitter", "https://example.com/race/")
    assert len(dupes) == 1
    assert dupes[0]["submission_id"] == first.id
    assert dupes[0]["story_id"] is None
    assert dupes[0]["reason"] == "url"


def test_find_duplicates_ignores_decided_submission(session):
    sub = create_submission(
        session, "Decided submitter", url="https://example.com/done", user_id="bob"
    )
    reject_submission(session, sub.id)
    # A rejected submission no longer occupies the URL, so a new submit is clear.
    assert find_duplicates(session, "New attempt", "https://example.com/done") == []


def test_create_submission_rejects_pending_duplicate(session):
    create_submission(
        session, "Concurrent post", url="https://example.com/concurrent", user_id="a"
    )
    with pytest.raises(SubmissionError, match="duplicate"):
        create_submission(
            session,
            "Concurrent post copy",
            url="https://example.com/concurrent",
            user_id="b",
        )


# ---------------------------------------------------------------------------
# Creating submissions
# ---------------------------------------------------------------------------


def test_create_submission_happy_path(session):
    sub = create_submission(
        session,
        "DeepMind unveils new neural network",
        url="https://example.com/dm",
        description="A breakthrough",
        user_id="alice",
    )
    assert sub.id is not None
    assert sub.status == "pending"
    assert sub.category == "ai"
    assert sub.url == "https://example.com/dm"


def test_create_submission_self_post_has_no_url(session):
    sub = create_submission(session, "Ask: best rocket propulsion book?", user_id="bob")
    assert sub.url is None
    assert sub.category == "aerospace"


def test_create_submission_honours_explicit_category(session):
    sub = create_submission(
        session, "Some neutral headline", category="aerospace", user_id="carol"
    )
    assert sub.category == "aerospace"


def test_create_submission_empty_title_rejected(session):
    with pytest.raises(SubmissionError):
        create_submission(session, "   ", user_id="dave")


def test_create_submission_duplicate_rejected(session):
    _seed_story(session, url="https://example.com/dup")
    with pytest.raises(SubmissionError, match="duplicate"):
        create_submission(session, "Whatever", url="https://example.com/dup")


# ---------------------------------------------------------------------------
# Moderation queue + lifecycle
# ---------------------------------------------------------------------------


def test_list_pending_is_fifo(session):
    a = create_submission(session, "First AI story", user_id="u")
    b = create_submission(session, "Second rocket story", user_id="u")
    pending = list_pending(session)
    assert [p["id"] for p in pending] == [a.id, b.id]


def test_list_pending_excludes_decided(session):
    a = create_submission(session, "Approve me AI", user_id="u")
    create_submission(session, "Keep me pending rocket", user_id="u")
    approve_submission(session, a.id)
    pending = list_pending(session)
    assert a.id not in [p["id"] for p in pending]


def test_list_pending_bad_limit(session):
    with pytest.raises(SubmissionError):
        list_pending(session, 0)


def test_approve_mints_story_and_awards_karma(session):
    sub = create_submission(
        session, "New transformer model", url="https://example.com/t", user_id="erin"
    )
    story = approve_submission(session, sub.id)
    assert story.id is not None
    assert story.submitted_by == "erin"
    assert story.topic == "ai"

    refreshed = session.get(Submission, sub.id)
    assert refreshed.status == "approved"
    assert refreshed.story_id == story.id
    assert refreshed.points == SUBMISSION_KARMA

    prof = session.query(UserProfile).filter_by(username="erin").one()
    assert prof.karma == SUBMISSION_KARMA


def test_approve_self_post_gets_synthetic_url(session):
    sub = create_submission(session, "Ask: favourite LLM tooling?", user_id="frank")
    story = approve_submission(session, sub.id)
    assert story.url == f"submission:{sub.id}"


def test_approve_is_idempotent(session):
    sub = create_submission(session, "Lunar lander news", user_id="gina")
    first = approve_submission(session, sub.id)
    second = approve_submission(session, sub.id)
    assert first.id == second.id
    prof = session.query(UserProfile).filter_by(username="gina").one()
    # Karma is awarded exactly once, not on the repeat approval.
    assert prof.karma == SUBMISSION_KARMA


def test_approve_unknown_id(session):
    with pytest.raises(SubmissionError) as exc:
        approve_submission(session, 9999)
    assert exc.value.not_found is True


def test_reject_closes_without_story(session):
    sub = create_submission(session, "Spammy rocket post", user_id="h")
    rejected = reject_submission(session, sub.id)
    assert rejected.status == "rejected"
    assert rejected.story_id is None
    assert session.query(UserProfile).filter_by(username="h").one_or_none() is None


def test_reject_then_approve_fails(session):
    sub = create_submission(session, "Borderline AI post", user_id="i")
    reject_submission(session, sub.id)
    with pytest.raises(SubmissionError, match="already rejected"):
        approve_submission(session, sub.id)


def test_approve_then_reject_fails(session):
    sub = create_submission(session, "Good AI post", user_id="j")
    approve_submission(session, sub.id)
    with pytest.raises(SubmissionError, match="already approved"):
        reject_submission(session, sub.id)


# ---------------------------------------------------------------------------
# API layer
# ---------------------------------------------------------------------------


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


def test_api_create_submission(client):
    resp = client.post(
        "/api/submissions",
        json={"title": "GPT-5 rumours swirl", "url": "https://example.com/gpt5"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "pending"
    assert body["category"] == "ai"
    assert "voter_id" in resp.cookies


def test_api_create_submission_missing_title(client):
    resp = client.post("/api/submissions", json={"url": "https://example.com/x"})
    assert resp.status_code == 400


def test_api_create_submission_duplicate(client, api_engine):
    sess = get_session(api_engine)
    try:
        _seed_story(sess, url="https://example.com/known")
    finally:
        sess.close()
    resp = client.post(
        "/api/submissions",
        json={"title": "Anything", "url": "https://example.com/known"},
    )
    assert resp.status_code == 400


def test_api_list_and_approve_flow(client):
    create = client.post("/api/submissions", json={"title": "Mars rover update"})
    sub_id = create.json()["id"]

    listing = client.get("/api/submissions")
    assert sub_id in [s["id"] for s in listing.json()]

    approve = client.post(
        f"/api/submissions/{sub_id}/approve", headers=ADMIN_HEADERS
    )
    assert approve.status_code == 200
    assert approve.json()["status"] == "approved"

    after = client.get("/api/submissions")
    assert sub_id not in [s["id"] for s in after.json()]


def test_api_reject(client):
    create = client.post("/api/submissions", json={"title": "Reject this AI post"})
    sub_id = create.json()["id"]
    resp = client.post(f"/api/submissions/{sub_id}/reject", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"


def test_api_approve_unknown_returns_404(client):
    assert (
        client.post(
            "/api/submissions/12345/approve", headers=ADMIN_HEADERS
        ).status_code
        == 404
    )


def test_api_approve_requires_admin_token(client):
    create = client.post("/api/submissions", json={"title": "Needs moderation AI"})
    sub_id = create.json()["id"]
    assert client.post(f"/api/submissions/{sub_id}/approve").status_code == 403
    assert (
        client.post(
            f"/api/submissions/{sub_id}/approve",
            headers={"X-Admin-Token": "wrong-token"},
        ).status_code
        == 403
    )
    listing = client.get("/api/submissions")
    assert sub_id in [s["id"] for s in listing.json()]


def test_api_reject_requires_admin_token(client):
    create = client.post("/api/submissions", json={"title": "Spam AI post"})
    sub_id = create.json()["id"]
    assert client.post(f"/api/submissions/{sub_id}/reject").status_code == 403


def test_api_duplicates_preview(client, api_engine):
    sess = get_session(api_engine)
    try:
        _seed_story(sess, title="Falcon 9 lands again", url="https://example.com/f9")
    finally:
        sess.close()
    resp = client.get(
        "/api/submissions/duplicates",
        params={"title": "Falcon 9 lands again", "url": "https://example.com/f9"},
    )
    assert resp.status_code == 200
    assert resp.json()[0]["reason"] == "url"


def test_submit_and_moderation_pages_render(client):
    client.post("/api/submissions", json={"title": "Pending neural net story"})
    assert client.get("/submit").status_code == 200
    mod = client.get("/moderation")
    assert mod.status_code == 200
    assert "Pending neural net story" in mod.text


def test_unused_now_constant_present():
    # Guards against accidental removal of the deterministic timestamp anchor.
    assert isinstance(NOW, datetime)
    assert NOW.tzinfo == timezone.utc
