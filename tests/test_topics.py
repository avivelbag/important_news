import datetime as dt

import pytest

from src.db import init_engine_for_test
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


@pytest.fixture
def session():
    s = init_engine_for_test("sqlite:///:memory:")
    seed_topics(s)
    return s


def _story(session, title, summary="", topic="ai", score=0.0, days_old=0):
    created = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(
        days=days_old
    )
    s = Story(
        url=f"https://example.com/{title}-{days_old}-{score}",
        title=title,
        summary=summary,
        topic=topic,
        score=score,
        created_at=created,
    )
    session.add(s)
    session.commit()
    return s


def test_seed_creates_hierarchy_with_at_least_20_tags(session):
    topics = list_topics(session)
    children = [t for t in topics if t["parent_id"] is not None]
    assert len(children) >= 20
    slugs = {t["slug"] for t in topics}
    assert "artificial-intelligence" in slugs
    assert "aerospace" in slugs
    assert "llms" in slugs and "launch-systems" in slugs


def test_seed_is_idempotent(session):
    before = topic_analytics(session)
    again = seed_topics(session)
    assert again["created"] == 0
    after = list_topics(session)
    assert len(after) == len(list_topics(session))
    assert before["most_followed"] == []


def test_suggest_topics_matches_keywords():
    slugs = suggest_topics("New GPT model breaks records", "a large language model")
    assert "llms" in slugs


def test_suggest_topics_word_boundary_no_false_positive():
    slugs = suggest_topics("The organ recital", "")
    assert "generative-ai" not in slugs


def test_suggest_topics_empty_returns_empty():
    assert suggest_topics("", "") == []


def test_auto_tag_story_tags_from_text(session):
    story = _story(session, "SpaceX launches a new rocket", "starship flight test")
    result = auto_tag_story(session, story.id)
    assert "launch-systems" in result["topics"]
    assert "commercial-space" in result["topics"]


def test_auto_tag_story_no_match_is_noop(session):
    story = _story(session, "Quarterly earnings report", "nothing relevant here")
    result = auto_tag_story(session, story.id)
    assert result["topics"] == []


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


def test_follow_and_unfollow_updates_count(session):
    follow_topic(session, "alice", "llms")
    again = follow_topic(session, "alice", "llms")
    assert again["follower_count"] == 1
    assert again["following"] is True
    follow_topic(session, "bob", "llms")
    topic = get_topic(session, "llms")
    assert topic["follower_count"] == 2
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
    followed = list_followed(session, "alice")
    slugs = {t["slug"] for t in followed}
    assert slugs == {"llms", "robotics"}


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
    ids = {s["id"] for s in feed["stories"]}
    assert ids == {a.id}


def test_followed_feed_dedupes_multi_tagged(session):
    s = _story(session, "rocket robot", days_old=1)
    tag_story(session, s.id, ["launch-systems", "robotics"])
    follow_topic(session, "alice", "launch-systems")
    follow_topic(session, "alice", "robotics")
    feed = followed_feed(session, "alice")
    assert feed["total"] == 1


def test_followed_feed_no_follows_is_empty(session):
    s = _story(session, "llm story")
    tag_story(session, s.id, ["llms"])
    feed = followed_feed(session, "alice")
    assert feed["stories"] == []
    assert feed["total"] == 0


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


def test_auto_tag_all_backfills(session):
    _story(session, "GPT language model breakthrough")
    _story(session, "irrelevant business update")
    result = auto_tag_all(session)
    assert result["tagged"] == 1


def test_list_topics_unknown_parent_raises(session):
    with pytest.raises(TopicError) as exc:
        list_topics(session, parent="ghost")
    assert exc.value.not_found is True


def test_seed_data_slugs_unique():
    slugs = [s["slug"] for s in TOPIC_SEEDS]
    assert len(slugs) == len(set(slugs))
