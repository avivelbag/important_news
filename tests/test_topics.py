"""Tests for the topic/tag service, seeding, and topic API endpoints."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import src.api as api
from src.db import get_engine, get_session, init_db
from src.models import Story
from src.topics import (
    TOPIC_SEEDS,
    TopicError,
    auto_tag_all,
    auto_tag_story,
    follow_topic,
    followed_feed,
    get_topic,
    list_followed,
    list_topics,
    seed_topics,
    suggest_topics,
    tag_story,
    topic_analytics,
    topic_stories,
    unfollow_topic,
)

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


def _story(session, title, *, score=0.0, days_old=0, topic="ai") -> Story:
    published = _BASE + timedelta(days=days_old)
    story = Story(
        title=title,
        url=f"https://example.com/{title.replace(' ', '-')}-{days_old}-{score}",
        source_name="hn",
        topic=topic,
        computed_score=score,
        published_at=published,
        fetched_at=published,
    )
    session.add(story)
    session.commit()
    return story


# --- seeding & suggestion ---------------------------------------------------


def test_seed_creates_hierarchy_with_at_least_20_tags(session):
    topics = list_topics(session)
    children = [t for t in topics if t["parent_id"] is not None]
    assert len(children) >= 20
    slugs = {t["slug"] for t in topics}
    assert "artificial-intelligence" in slugs
    assert "aerospace" in slugs
    assert "llms" in slugs and "launch-systems" in slugs


def test_seed_is_idempotent(session):
    again = seed_topics(session)
    assert again["created"] == 0
    assert again["total"] == len(list_topics(session))


def test_seed_data_slugs_unique():
    slugs = [s["slug"] for s in TOPIC_SEEDS]
    assert len(slugs) == len(set(slugs))


def test_suggest_topics_matches_keywords():
    slugs = suggest_topics("New GPT model breaks records", "a large language model")
    assert "llms" in slugs


def test_suggest_topics_word_boundary_no_false_positive():
    assert "generative-ai" not in suggest_topics("The organ recital", "")


def test_suggest_topics_empty_returns_empty():
    assert suggest_topics("", "") == []


# --- tagging ----------------------------------------------------------------


def test_auto_tag_story_tags_from_title(session):
    story = _story(session, "SpaceX launches a new rocket on Starship")
    result = auto_tag_story(session, story.id)
    assert "launch-systems" in result["topics"]
    assert "commercial-space" in result["topics"]


def test_auto_tag_story_no_match_is_noop(session):
    story = _story(session, "Quarterly earnings report")
    assert auto_tag_story(session, story.id)["topics"] == []


def test_tag_story_is_idempotent(session):
    story = _story(session, "Some article")
    tag_story(session, story.id, ["llms"])
    second = tag_story(session, story.id, ["llms"])
    assert second["topics"] == ["llms"]
    topic = next(t for t in list_topics(session) if t["slug"] == "llms")
    assert topic["article_count"] == 1


def test_tag_story_unknown_slug_raises(session):
    story = _story(session, "Some article")
    with pytest.raises(TopicError) as exc:
        tag_story(session, story.id, ["not-a-real-topic"])
    assert exc.value.not_found is True


def test_tag_story_unknown_story_raises(session):
    with pytest.raises(TopicError) as exc:
        tag_story(session, 999999, ["llms"])
    assert exc.value.not_found is True


def test_auto_tag_all_backfills(session):
    _story(session, "GPT language model breakthrough")
    _story(session, "irrelevant business update")
    assert auto_tag_all(session)["tagged"] == 1


# --- following --------------------------------------------------------------


def test_follow_and_unfollow_updates_count(session):
    follow_topic(session, "alice", "llms")
    again = follow_topic(session, "alice", "llms")
    assert again["follower_count"] == 1
    assert again["following"] is True
    follow_topic(session, "bob", "llms")
    assert get_topic(session, "llms")["follower_count"] == 2
    removed = unfollow_topic(session, "alice", "llms")
    assert removed["following"] is False
    assert removed["follower_count"] == 1


def test_unfollow_not_following_is_noop(session):
    result = unfollow_topic(session, "nobody", "llms")
    assert result["following"] is False
    assert result["follower_count"] == 0


def test_follow_empty_user_raises(session):
    with pytest.raises(TopicError):
        follow_topic(session, "   ", "llms")


def test_follow_unknown_topic_raises(session):
    with pytest.raises(TopicError) as exc:
        follow_topic(session, "alice", "ghost-topic")
    assert exc.value.not_found is True


def test_list_followed_returns_only_user_topics(session):
    follow_topic(session, "alice", "llms")
    follow_topic(session, "alice", "robotics")
    follow_topic(session, "bob", "drones")
    slugs = {t["slug"] for t in list_followed(session, "alice")}
    assert slugs == {"llms", "robotics"}


# --- topic pages & feed -----------------------------------------------------


def test_topic_stories_sorted_by_recency_and_score(session):
    old = _story(session, "old llm news", score=1.0, days_old=0)
    new = _story(session, "new llm news", score=5.0, days_old=10)
    tag_story(session, old.id, ["llms"])
    tag_story(session, new.id, ["llms"])

    by_recency = topic_stories(session, "llms", sort="recency")
    assert [s["id"] for s in by_recency["stories"]] == [new.id, old.id]

    by_score = topic_stories(session, "llms", sort="score")
    assert by_score["stories"][0]["id"] == new.id
    assert by_score["total"] == 2


def test_topic_stories_invalid_sort_raises(session):
    with pytest.raises(TopicError):
        topic_stories(session, "llms", sort="popularity")


def test_topic_stories_unknown_topic_raises(session):
    with pytest.raises(TopicError) as exc:
        topic_stories(session, "ghost", sort="recency")
    assert exc.value.not_found is True


def test_followed_feed_only_includes_followed(session):
    a = _story(session, "llm story", days_old=1)
    b = _story(session, "drone story", days_old=2)
    tag_story(session, a.id, ["llms"])
    tag_story(session, b.id, ["drones"])
    follow_topic(session, "alice", "llms")
    feed = followed_feed(session, "alice", sort="recency")
    assert {s["id"] for s in feed["stories"]} == {a.id}


def test_followed_feed_dedupes_multi_tagged(session):
    s = _story(session, "rocket robot", days_old=1)
    tag_story(session, s.id, ["launch-systems", "robotics"])
    follow_topic(session, "alice", "launch-systems")
    follow_topic(session, "alice", "robotics")
    assert followed_feed(session, "alice")["total"] == 1


def test_followed_feed_no_follows_is_empty(session):
    s = _story(session, "llm story")
    tag_story(session, s.id, ["llms"])
    feed = followed_feed(session, "alice")
    assert feed["stories"] == []
    assert feed["total"] == 0


# --- analytics & related ----------------------------------------------------


def test_analytics_ranks_followed_and_trending(session):
    s1 = _story(session, "a")
    s2 = _story(session, "b")
    tag_story(session, s1.id, ["llms", "robotics"])
    tag_story(session, s2.id, ["llms"])
    follow_topic(session, "alice", "robotics")
    follow_topic(session, "bob", "robotics")
    follow_topic(session, "alice", "llms")

    stats = topic_analytics(session)
    assert stats["most_followed"][0]["slug"] == "robotics"
    assert stats["trending"][0]["slug"] == "llms"
    assert all(t["follower_count"] > 0 for t in stats["most_followed"])
    assert all(t["article_count"] > 0 for t in stats["trending"])


def test_get_topic_lists_related(session):
    domain = get_topic(session, "artificial-intelligence")
    assert "llms" in domain["related"]
    leaf = get_topic(session, "llms")
    assert "computer-vision" in leaf["related"]
    assert "llms" not in leaf["related"]


def test_list_topics_unknown_parent_raises(session):
    with pytest.raises(TopicError) as exc:
        list_topics(session, parent="ghost")
    assert exc.value.not_found is True


# --- API --------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # A file-backed DB (not in-memory) so the TestClient's request thread sees
    # the same schema/data, mirroring tests/test_submissions.py.
    db_path = tmp_path / "topics.db"
    eng = get_engine(f"sqlite:///{db_path}")
    init_db(eng)
    monkeypatch.setattr(api, "_engine", eng)
    monkeypatch.setattr(api, "get_session", lambda e=None: get_session(eng))
    sess = get_session(eng)
    seed_topics(sess)
    sess.close()
    with TestClient(api.app) as test_client:
        yield test_client
    eng.dispose()


def test_api_list_and_get_topic(client):
    resp = client.get("/api/topics")
    assert resp.status_code == 200
    assert len(resp.json()["topics"]) >= 22

    detail = client.get("/api/topics/llms")
    assert detail.status_code == 200
    assert detail.json()["slug"] == "llms"
    assert "computer-vision" in detail.json()["related"]


def test_api_get_unknown_topic_404(client):
    assert client.get("/api/topics/ghost").status_code == 404


def test_api_follow_unfollow_roundtrip(client):
    # The TestClient persists the voter_id cookie set by follow across requests.
    follow = client.post("/api/user/topics/llms/follow")
    assert follow.status_code == 200
    assert follow.json()["follower_count"] == 1

    followed = client.get("/api/user/topics")
    assert {t["slug"] for t in followed.json()["topics"]} == {"llms"}

    unfollow = client.request("DELETE", "/api/user/topics/llms/follow")
    assert unfollow.status_code == 200
    assert unfollow.json()["following"] is False


def test_api_unfollow_without_cookie_400(client):
    client.cookies.clear()
    assert client.request("DELETE", "/api/user/topics/llms/follow").status_code == 400


def test_api_suggest_endpoint(client):
    resp = client.get("/api/topics/suggest", params={"title": "A new GPT model"})
    assert resp.status_code == 200
    assert "llms" in resp.json()["slugs"]
