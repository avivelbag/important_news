"""Tests for the content moderation/flagging service and its API endpoints."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import src.api as api
from src.db import get_engine, get_session, init_db
from src.generate_site import fetch_stories
from src.models import Comment, ModerationAction, ModerationNotification, Story
from src.moderation import (
    AUTO_HIDE_THRESHOLD,
    ModerationError,
    delete_content,
    dismiss_flags,
    flag_content,
    flagger_stats,
    hide_content,
    list_actions,
    list_flagged,
    list_notifications,
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


def _make_comment(session, story, **kwargs) -> Comment:
    base = dict(
        story_id=story.id,
        body="a comment",
        created_at=datetime(2026, 1, 2, 3, 6, tzinfo=timezone.utc),
    )
    base.update(kwargs)
    comment = Comment(**base)
    session.add(comment)
    session.commit()
    return comment


# --- service: happy path ----------------------------------------------------


def test_flag_story_records_flag_and_count(session):
    story = _make_story(session)
    result = flag_content(session, "story", story.id, "user-a", "spam")
    assert result["flag_count"] == 1
    assert result["reason_counts"] == {"spam": 1}
    assert result["is_hidden"] is False
    assert story.flag_count == 1


def test_flag_comment_records_flag(session):
    story = _make_story(session)
    comment = _make_comment(session, story)
    result = flag_content(session, "comment", comment.id, "user-a", "abuse")
    assert result["flag_count"] == 1
    assert comment.flag_count == 1


def test_reason_breakdown_aggregates_distinct_reasons(session):
    story = _make_story(session)
    flag_content(session, "story", story.id, "u1", "spam")
    flag_content(session, "story", story.id, "u2", "abuse")
    result = flag_content(session, "story", story.id, "u3", "spam")
    assert result["reason_counts"] == {"spam": 2, "abuse": 1}
    assert result["flag_count"] == 3


# --- service: duplicate / own-content guards --------------------------------


def test_duplicate_flag_from_same_user_is_noop(session):
    story = _make_story(session)
    flag_content(session, "story", story.id, "user-a", "spam")
    result = flag_content(session, "story", story.id, "user-a", "abuse")
    assert result["flag_count"] == 1
    assert result["reason_counts"] == {"abuse": 1}


def test_cannot_flag_own_story(session):
    story = _make_story(session, submitted_by="owner")
    with pytest.raises(ModerationError, match="own content"):
        flag_content(session, "story", story.id, "owner", "spam")


def test_cannot_flag_own_comment(session):
    story = _make_story(session)
    comment = _make_comment(session, story, user_id="owner")
    with pytest.raises(ModerationError, match="own content"):
        flag_content(session, "comment", comment.id, "owner", "abuse")


# --- service: validation / failure modes ------------------------------------


def test_unknown_reason_rejected(session):
    story = _make_story(session)
    with pytest.raises(ModerationError, match="unknown reason"):
        flag_content(session, "story", story.id, "user-a", "bogus")


def test_unknown_content_type_rejected(session):
    with pytest.raises(ModerationError, match="unknown content_type"):
        flag_content(session, "widget", 1, "user-a", "spam")


def test_flag_missing_content_is_not_found(session):
    with pytest.raises(ModerationError) as exc:
        flag_content(session, "story", 999, "user-a", "spam")
    assert exc.value.not_found is True


def test_empty_user_rejected(session):
    story = _make_story(session)
    with pytest.raises(ModerationError, match="user_id"):
        flag_content(session, "story", story.id, "   ", "spam")


# --- service: auto-hide -----------------------------------------------------


def test_auto_hide_when_threshold_reached(session):
    story = _make_story(session)
    for i in range(AUTO_HIDE_THRESHOLD):
        result = flag_content(session, "story", story.id, f"user-{i}", "spam")
    assert result["is_hidden"] is True
    assert story.is_hidden is True
    actions = list_actions(session, "story", story.id)
    assert any(a["action"] == "auto_hide" for a in actions)


def test_no_auto_hide_below_threshold(session):
    story = _make_story(session)
    for i in range(AUTO_HIDE_THRESHOLD - 1):
        flag_content(session, "story", story.id, f"user-{i}", "spam")
    assert story.is_hidden is False


def test_custom_threshold_respected(session):
    story = _make_story(session)
    flag_content(session, "story", story.id, "u1", "spam", auto_hide_threshold=1)
    assert story.is_hidden is True


# --- service: moderator actions + audit + notifications ---------------------


def test_hide_content_upholds_flags_and_notifies(session):
    story = _make_story(session, submitted_by="owner")
    flag_content(session, "story", story.id, "u1", "spam")
    result = hide_content(session, "story", story.id, "mod-1")
    assert result["is_hidden"] is True
    assert result["upheld"] == 1
    assert story.is_hidden is True
    notes = list_notifications(session, "owner")
    assert len(notes) == 1
    assert notes[0]["action"] == "hide"


def test_delete_comment_soft_deletes_and_hides(session):
    story = _make_story(session)
    comment = _make_comment(session, story, user_id="owner")
    flag_content(session, "comment", comment.id, "u1", "abuse")
    result = delete_content(session, "comment", comment.id, "mod-1")
    assert result["deleted"] is True
    assert comment.deleted is True
    assert comment.is_hidden is True
    notes = list_notifications(session, "owner")
    assert notes[0]["action"] == "delete"


def test_dismiss_clears_flags_and_unhides(session):
    story = _make_story(session)
    for i in range(AUTO_HIDE_THRESHOLD):
        flag_content(session, "story", story.id, f"user-{i}", "spam")
    assert story.is_hidden is True
    result = dismiss_flags(session, "story", story.id, "mod-1")
    assert result["dismissed"] == AUTO_HIDE_THRESHOLD
    assert story.is_hidden is False
    assert story.flag_count == 0


def test_actions_are_logged_for_audit(session):
    story = _make_story(session)
    flag_content(session, "story", story.id, "u1", "spam")
    hide_content(session, "story", story.id, "mod-1")
    actions = list_actions(session, "story", story.id)
    assert actions[0]["action"] == "hide"
    assert actions[0]["moderator"] == "mod-1"
    assert session.query(ModerationAction).count() >= 1


def test_no_notification_when_owner_anonymous(session):
    story = _make_story(session)  # no submitted_by
    flag_content(session, "story", story.id, "u1", "spam")
    hide_content(session, "story", story.id, "mod-1")
    assert session.query(ModerationNotification).count() == 0


def test_hide_requires_moderator(session):
    story = _make_story(session)
    with pytest.raises(ModerationError, match="moderator"):
        hide_content(session, "story", story.id, "")


# --- service: dashboard / flagger stats -------------------------------------


def test_list_flagged_sorted_by_count(session):
    s1 = _make_story(session, url="https://a.test")
    s2 = _make_story(session, url="https://b.test")
    flag_content(session, "story", s1.id, "u1", "spam")
    flag_content(session, "story", s2.id, "u1", "spam")
    flag_content(session, "story", s2.id, "u2", "abuse")
    flagged = list_flagged(session)
    assert flagged[0]["content_id"] == s2.id
    assert flagged[0]["flag_count"] == 2
    assert flagged[0]["reason_counts"] == {"spam": 1, "abuse": 1}


def test_list_flagged_filters_by_type_and_reason(session):
    story = _make_story(session)
    comment = _make_comment(session, story)
    flag_content(session, "story", story.id, "u1", "spam")
    flag_content(session, "comment", comment.id, "u2", "abuse")
    only_comments = list_flagged(session, content_type="comment")
    assert len(only_comments) == 1
    assert only_comments[0]["content_type"] == "comment"
    only_spam = list_flagged(session, reason="spam")
    assert len(only_spam) == 1
    assert only_spam[0]["content_type"] == "story"


def test_list_flagged_excludes_resolved(session):
    story = _make_story(session)
    flag_content(session, "story", story.id, "u1", "spam")
    dismiss_flags(session, "story", story.id, "mod-1")
    assert list_flagged(session) == []


def test_flagger_stats_tracks_false_reporters(session):
    s1 = _make_story(session, url="https://a.test")
    s2 = _make_story(session, url="https://b.test")
    flag_content(session, "story", s1.id, "liar", "spam")
    flag_content(session, "story", s2.id, "liar", "spam")
    dismiss_flags(session, "story", s1.id, "mod-1")
    hide_content(session, "story", s2.id, "mod-1")
    stats = {s["user_id"]: s for s in flagger_stats(session)}
    assert stats["liar"]["total"] == 2
    assert stats["liar"]["dismissed"] == 1
    assert stats["liar"]["upheld"] == 1
    assert stats["liar"]["false_rate"] == 0.5


def test_list_notifications_unread_filter(session):
    story = _make_story(session, submitted_by="owner")
    flag_content(session, "story", story.id, "u1", "spam")
    hide_content(session, "story", story.id, "mod-1")
    unread = list_notifications(session, "owner", unread_only=True)
    assert len(unread) == 1


# --- generator integration --------------------------------------------------


def test_hidden_story_excluded_from_site(session):
    visible = _make_story(session, url="https://visible.test")
    hidden = _make_story(session, url="https://hidden.test")
    for i in range(AUTO_HIDE_THRESHOLD):
        flag_content(session, "story", hidden.id, f"user-{i}", "spam")
    rendered = {s.id for s in fetch_stories(session)}
    assert visible.id in rendered
    assert hidden.id not in rendered


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


def test_api_flag_story_then_list_as_admin(client, api_engine):
    session = get_session(api_engine)
    story = _make_story(session)
    resp = client.post(f"/api/stories/{story.id}/flag", json={"reason": "spam"})
    assert resp.status_code == 201
    assert resp.json()["flag_count"] == 1

    unauth = client.get("/api/flags")
    assert unauth.status_code == 403

    listed = client.get("/api/flags", headers={"X-Admin-Token": "swarm-admin"})
    assert listed.status_code == 200
    assert listed.json()[0]["content_id"] == story.id


def test_api_flag_requires_reason(client, api_engine):
    session = get_session(api_engine)
    story = _make_story(session)
    resp = client.post(f"/api/stories/{story.id}/flag", json={})
    assert resp.status_code == 400


def test_api_dismiss_and_delete_require_admin(client, api_engine):
    session = get_session(api_engine)
    story = _make_story(session)
    client.post(f"/api/stories/{story.id}/flag", json={"reason": "spam"})
    forbidden = client.post(f"/api/flags/story/{story.id}/dismiss")
    assert forbidden.status_code == 403
    ok = client.post(
        f"/api/flags/story/{story.id}/hide",
        headers={"X-Admin-Token": "swarm-admin"},
    )
    assert ok.status_code == 200
    assert ok.json()["is_hidden"] is True


def test_api_flag_missing_story_404(client):
    resp = client.post("/api/stories/424242/flag", json={"reason": "spam"})
    assert resp.status_code == 404
