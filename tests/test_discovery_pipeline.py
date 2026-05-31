"""Tests for the HN search adapter and pipeline-level discussion discovery."""

import datetime as dt

import pytest
from sqlalchemy import func, select

import src.scraper as scraper
from src.db import get_engine, get_session, init_db
from src.discussions import (
    discover_discussions_for_stories,
    hn_search_fn,
)
from src.models import ExternalDiscussion, Story

NOW = dt.datetime(2026, 5, 31, 12, 0)

RSS_AI = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>AI Feed</title>
  <item>
    <title>Neural network beats GPT on a machine learning benchmark</title>
    <link>https://example.com/ai-1</link>
    <description>A deep learning breakthrough.</description>
    <pubDate>Mon, 01 Jan 2024 12:00:00 +0000</pubDate>
  </item>
</channel></rss>
"""


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


def _story(session, title, url):
    story = Story(
        title=title,
        url=url,
        source_name="Hacker News",
        topic="ai",
        published_at=NOW,
        fetched_at=NOW,
    )
    session.add(story)
    session.commit()
    return story


# ---------------------------------------------------------------------------
# hn_search_fn adapter
# ---------------------------------------------------------------------------


def test_hn_search_fn_parses_hits():
    payload = {
        "hits": [
            {
                "objectID": "42",
                "title": "Neural network machine learning",
                "num_comments": 12,
                "points": 340,
            }
        ]
    }
    results = hn_search_fn("hn", "neural network", fetch=lambda url: payload)
    assert results == [
        {
            "platform": "hn",
            "url": "https://news.ycombinator.com/item?id=42",
            "title": "Neural network machine learning",
            "comment_count": 12,
            "engagement_score": 340,
        }
    ]


def test_hn_search_fn_non_hn_platform_returns_empty_without_fetch():
    called = []

    def fetch(url):
        called.append(url)
        return {"hits": []}

    assert hn_search_fn("reddit", "neural", fetch=fetch) == []
    assert hn_search_fn("github", "neural", fetch=fetch) == []
    assert called == []


def test_hn_search_fn_empty_query_returns_empty():
    assert hn_search_fn("hn", "   ", fetch=lambda url: {"hits": []}) == []


def test_hn_search_fn_handles_fetch_error():
    def fetch(url):
        raise TimeoutError("network down")

    assert hn_search_fn("hn", "neural", fetch=fetch) == []


def test_hn_search_fn_skips_hits_without_object_id():
    payload = {"hits": [{"title": "no id", "num_comments": 1, "points": 2}]}
    assert hn_search_fn("hn", "neural", fetch=lambda url: payload) == []


def test_hn_search_fn_coerces_missing_metrics():
    payload = {"hits": [{"objectID": "7", "title": "Neural network"}]}
    result = hn_search_fn("hn", "neural", fetch=lambda url: payload)
    assert result[0]["comment_count"] == 0
    assert result[0]["engagement_score"] == 0


# ---------------------------------------------------------------------------
# discover_discussions_for_stories (pipeline entry point)
# ---------------------------------------------------------------------------


def test_discover_for_all_stories(session):
    a = _story(session, "Neural nets break records on deep learning",
               "https://example.com/a")
    b = _story(session, "Rocket reaches orbit after satellite launch",
               "https://example.com/b")

    def search_fn(platform, query):
        if platform != "hn":
            return []
        if "neural" in query.lower():
            return [{"platform": "hn", "url": "https://news.ycombinator.com/item?id=1",
                     "title": "Neural nets deep learning records", "comment_count": 5,
                     "engagement_score": 9}]
        return [{"platform": "hn", "url": "https://news.ycombinator.com/item?id=2",
                 "title": "Rocket orbit satellite launch", "comment_count": 3,
                 "engagement_score": 4}]

    created = discover_discussions_for_stories(session, search_fn, now=NOW)

    assert len(created) == 2
    a_links = session.scalars(
        select(ExternalDiscussion).where(ExternalDiscussion.story_id == a.id)
    ).all()
    b_links = session.scalars(
        select(ExternalDiscussion).where(ExternalDiscussion.story_id == b.id)
    ).all()
    assert len(a_links) == 1
    assert len(b_links) == 1


def test_discover_skips_duplicate_stories(session):
    canonical = _story(session, "Neural network deep learning record",
                       "https://example.com/canonical")
    dupe = _story(session, "Neural network deep learning record",
                  "https://example.com/dupe")
    dupe.canonical_id = canonical.id
    session.commit()

    def search_fn(platform, query):
        if platform != "hn":
            return []
        return [{"platform": "hn", "url": "https://news.ycombinator.com/item?id=9",
                 "title": "Neural network deep learning record", "comment_count": 1,
                 "engagement_score": 1}]

    created = discover_discussions_for_stories(session, search_fn, now=NOW)

    # Only the canonical story is matched; the duplicate is skipped.
    assert len(created) == 1
    assert created[0].story_id == canonical.id


def test_discover_for_all_stories_empty_db(session):
    assert discover_discussions_for_stories(session, lambda p, q: [], now=NOW) == []


# ---------------------------------------------------------------------------
# Pipeline integration via run_scraper
# ---------------------------------------------------------------------------


def test_run_scraper_triggers_discovery(engine):
    spec = scraper.SourceSpec(name="Feed", url="http://feed", kind="rss", category="ai")

    def fetch(url):
        return RSS_AI

    def search_fn(platform, query):
        if platform != "hn":
            return []
        return [{"platform": "hn", "url": "https://news.ycombinator.com/item?id=99",
                 "title": "Neural network machine learning benchmark", "comment_count": 8,
                 "engagement_score": 20}]

    scraper.run_scraper(engine, sources=[spec], fetch=fetch, now=NOW, search_fn=search_fn)

    session = get_session(engine)
    try:
        links = session.scalars(select(ExternalDiscussion)).all()
        assert len(links) == 1
        assert links[0].platform == "hn"
        assert links[0].comment_count == 8
    finally:
        session.close()


def test_run_scraper_search_fn_none_skips_discovery(engine):
    spec = scraper.SourceSpec(name="Feed", url="http://feed", kind="rss", category="ai")

    def fetch(url):
        return RSS_AI

    scraper.run_scraper(engine, sources=[spec], fetch=fetch, now=NOW, search_fn=None)

    session = get_session(engine)
    try:
        assert session.scalar(select(func.count(ExternalDiscussion.id))) == 0
    finally:
        session.close()
