"""Tests for external discussion discovery, linking, verification, and rendering."""

import datetime as dt

import pytest
from sqlalchemy.exc import IntegrityError

from src.db import get_engine, get_session, init_db
from src.discussions import (
    discover_for_story,
    discovered_within,
    get_discussions,
    match_score,
    normalize_url,
    verify_discussions,
)
from src.generate_site import render_discussion_links, render_story
from src.models import ExternalDiscussion, Story

NOW = dt.datetime(2026, 5, 31, 12, 0)


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


def _story(session, title="GPT-5 transformer scaling on Starship telemetry"):
    story = Story(
        title=title,
        url="https://example.com/article",
        source_name="Hacker News",
        topic="both",
        published_at=NOW,
        fetched_at=NOW,
    )
    session.add(story)
    session.commit()
    return story


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_normalize_url_collapses_variations():
    base = normalize_url("https://reddit.com/r/ml/comments/abc/title")
    assert normalize_url("http://www.reddit.com/r/ml/comments/abc/title/") == base
    assert normalize_url("https://reddit.com/r/ml/comments/abc/title?utm=x") == base
    assert base == "https://reddit.com/r/ml/comments/abc/title"


def test_match_score_overlap_and_empty():
    assert match_score("Transformer scaling laws", "Scaling laws for transformers") > 0.3
    # No shared topical tokens -> no match.
    assert match_score("Starship launch", "Knitting patterns guide") == 0.0
    # Empty / all-stopword titles never divide by zero.
    assert match_score("", "anything here") == 0.0
    assert match_score("the and of", "the and of") == 0.0


# ---------------------------------------------------------------------------
# Discovery happy path
# ---------------------------------------------------------------------------


def test_discover_matches_and_stores_relevant_threads(session):
    story = _story(session)

    def search_fn(platform, query):
        return [
            {
                "url": f"https://{platform}.com/thread/{platform}",
                "title": "Transformer scaling on Starship telemetry",
                "comment_count": 42,
                "engagement_score": 100,
            },
            {
                "url": f"https://{platform}.com/thread/off-topic",
                "title": "Best sourdough bread recipes",
                "comment_count": 5,
            },
        ]

    created = discover_for_story(session, story, search_fn, now=NOW)

    # One relevant thread per platform (reddit/github/hn); off-topic dropped.
    assert len(created) == 3
    assert {c.platform for c in created} == {"reddit", "github", "hn"}
    for c in created:
        assert c.discovered_at == NOW
        assert c.last_verified_at == NOW
        assert c.comment_count == 42


def test_get_discussions_ranked_and_render(session):
    story = _story(session)

    def search_fn(platform, query):
        if platform != "reddit":
            return []
        return [
            {
                "url": "https://reddit.com/r/ml/low",
                "title": "Transformer scaling Starship",
                "comment_count": 2,
                "engagement_score": 10,
            },
            {
                "url": "https://reddit.com/r/ml/high",
                "title": "Transformer scaling Starship telemetry",
                "comment_count": 99,
                "engagement_score": 500,
            },
        ]

    discover_for_story(session, story, search_fn, now=NOW)
    discussions = get_discussions(session, story.id)

    assert [d["engagement_score"] for d in discussions] == [500, 10]
    assert discussions[0]["platform_label"] == "Reddit"

    html = render_discussion_links(discussions)
    assert "Discuss on Reddit" in html
    assert "99 comments" in html
    assert "https://reddit.com/r/ml/high" in html

    # And the link surfaces inside a rendered story row.
    assert "Discuss on Reddit" in render_story(story, 1, discussions)


# ---------------------------------------------------------------------------
# Dedup, caching, and edge cases
# ---------------------------------------------------------------------------


def test_discovery_deduplicates_url_variations(session):
    story = _story(session)

    def search_fn(platform, query):
        if platform != "github":
            return []
        return [
            {"url": "https://github.com/org/repo/issues/7", "title": "Transformer scaling Starship"},
            {"url": "http://www.github.com/org/repo/issues/7/", "title": "Transformer scaling Starship"},
        ]

    created = discover_for_story(session, story, search_fn, now=NOW)
    assert len(created) == 1
    assert session.query(ExternalDiscussion).count() == 1


def test_unique_constraint_blocks_exact_duplicate(session):
    story = _story(session)
    row = ExternalDiscussion(
        story_id=story.id,
        platform="hn",
        url="https://news.ycombinator.com/item?id=1",
        title="t",
        discovered_at=NOW,
    )
    session.add(row)
    session.commit()
    dupe = ExternalDiscussion(
        story_id=story.id,
        platform="hn",
        url="https://news.ycombinator.com/item?id=1",
        title="t2",
        discovered_at=NOW,
    )
    session.add(dupe)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_cache_skips_recent_rediscovery(session):
    story = _story(session)
    calls = []

    def search_fn(platform, query):
        calls.append(platform)
        return [{"url": f"https://{platform}.com/x", "title": story.title}]

    discover_for_story(session, story, search_fn, now=NOW)
    first = len(calls)
    assert first == 3

    # Within the TTL window: no new search calls, nothing created.
    later = NOW + dt.timedelta(hours=1)
    created = discover_for_story(session, story, search_fn, now=later)
    assert created == []
    assert len(calls) == first

    assert discovered_within(session, story.id, now=later) is True
    # Past the TTL the guard opens again.
    assert (
        discovered_within(session, story.id, now=NOW + dt.timedelta(hours=48))
        is False
    )


def test_force_bypasses_cache_but_still_dedupes(session):
    story = _story(session)

    def search_fn(platform, query):
        return [{"url": f"https://{platform}.com/x", "title": story.title}]

    discover_for_story(session, story, search_fn, now=NOW)
    again = discover_for_story(session, story, search_fn, now=NOW, force=True)
    # Forced re-run finds the same URLs already stored -> creates nothing.
    assert again == []
    assert session.query(ExternalDiscussion).count() == 3


def test_search_fn_exception_isolated_per_platform(session):
    story = _story(session)

    def search_fn(platform, query):
        if platform == "reddit":
            raise TimeoutError("rate limited")
        return [{"url": f"https://{platform}.com/x", "title": story.title}]

    created = discover_for_story(session, story, search_fn, now=NOW)
    # reddit blew up; github and hn still contribute.
    assert {c.platform for c in created} == {"github", "hn"}


def test_discovery_ignores_candidates_missing_fields(session):
    story = _story(session)

    def search_fn(platform, query):
        if platform != "hn":
            return []
        return [
            {"url": "https://hn.com/a"},  # no title
            {"title": "Transformer scaling Starship"},  # no url
            {"url": "https://hn.com/b", "title": "Transformer scaling Starship"},
        ]

    created = discover_for_story(session, story, search_fn, now=NOW)
    assert len(created) == 1
    assert created[0].url == "https://hn.com/b"


def test_get_discussions_empty_for_unlinked_story(session):
    story = _story(session)
    assert get_discussions(session, story.id) == []
    assert render_discussion_links([]) == ""


# ---------------------------------------------------------------------------
# Verification / link rot
# ---------------------------------------------------------------------------


def test_verify_refreshes_live_and_prunes_dead_links(session):
    story = _story(session)

    def search_fn(platform, query):
        return [{"url": f"https://{platform}.com/x", "title": story.title, "comment_count": 1}]

    discover_for_story(session, story, search_fn, now=NOW)

    def verify_fn(d):
        if d.platform == "github":
            return None  # dead link -> remove
        return {"comment_count": 7, "engagement_score": 50}

    later = NOW + dt.timedelta(days=1)
    summary = verify_discussions(session, verify_fn, story_id=story.id, now=later)

    assert summary == {"verified": 2, "removed": 1, "errors": 0}
    remaining = get_discussions(session, story.id)
    assert len(remaining) == 2
    assert all(d["platform"] != "github" for d in remaining)
    assert all(d["comment_count"] == 7 for d in remaining)
    for row in session.query(ExternalDiscussion).all():
        assert row.last_verified_at == later


def test_verify_treats_exceptions_as_unverified_not_dead(session):
    story = _story(session)
    row = ExternalDiscussion(
        story_id=story.id,
        platform="reddit",
        url="https://reddit.com/x",
        title="t",
        comment_count=3,
        discovered_at=NOW,
        last_verified_at=NOW,
    )
    session.add(row)
    session.commit()

    def verify_fn(d):
        raise ConnectionError("boom")

    summary = verify_discussions(session, verify_fn, now=NOW + dt.timedelta(days=1))
    assert summary["errors"] == 1
    assert summary["removed"] == 0
    # Row survives untouched (not deleted, last_verified_at unchanged).
    surviving = session.query(ExternalDiscussion).one()
    assert surviving.last_verified_at == NOW
