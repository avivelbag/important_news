import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.discussions import (
    discover_discussions_for_stories,
    hn_search_fn,
)
from src.models import ExternalDiscussion, Story, session_scope
from src.scraper import run


def _db(tmp_path):
    return str(tmp_path / "news.db")


def _add_story(session, title, summary, url):
    story = Story(
        title=title,
        url=url,
        summary=summary,
        source="hackernews",
        published_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        category="ai",
        importance=1.0,
    )
    session.add(story)
    session.commit()
    return story


def test_hn_search_fn_parses_hits():
    payload = {
        "hits": [
            {"objectID": "42", "title": "Neural model deep learning",
             "num_comments": 12, "points": 340},
        ]
    }
    results = hn_search_fn({"neural", "model"}, None, fetch=lambda url: payload)
    assert results == [
        {
            "platform": "hackernews",
            "url": "https://news.ycombinator.com/item?id=42",
            "title": "Neural model deep learning",
            "comment_count": 12,
            "engagement_score": 340.0,
        }
    ]


def test_hn_search_fn_empty_keywords_no_fetch():
    called = []

    def fetch(url):
        called.append(url)
        return {"hits": []}

    assert hn_search_fn(set(), None, fetch=fetch) == []
    assert called == []


def test_hn_search_fn_handles_fetch_error():
    def fetch(url):
        raise TimeoutError("network down")

    assert hn_search_fn({"neural"}, None, fetch=fetch) == []


def test_hn_search_fn_skips_hits_without_id():
    payload = {"hits": [{"title": "no id here", "num_comments": 1, "points": 2}]}
    assert hn_search_fn({"neural"}, None, fetch=lambda url: payload) == []


def test_hn_search_fn_coerces_missing_metrics():
    payload = {"hits": [{"objectID": "7", "title": "Neural model"}]}
    results = hn_search_fn({"neural", "model"}, None, fetch=lambda url: payload)
    assert results[0]["comment_count"] == 0
    assert results[0]["engagement_score"] == 0.0


def test_discover_for_all_stories(tmp_path):
    with session_scope(_db(tmp_path)) as session:
        a = _add_story(session, "Neural nets break records", "deep learning model",
                       "https://example.com/a")
        b = _add_story(session, "Rocket reaches orbit", "satellite launch aerospace",
                       "https://example.com/b")

        def search(keywords, story):
            if "neural" in keywords:
                return [{"platform": "hackernews", "url": "https://news.ycombinator.com/item?id=1",
                         "title": "Neural model deep learning", "comment_count": 5,
                         "engagement_score": 9.0}]
            return [{"platform": "hackernews", "url": "https://news.ycombinator.com/item?id=2",
                     "title": "Rocket orbit satellite launch", "comment_count": 3,
                     "engagement_score": 4.0}]

        discovered = discover_discussions_for_stories(session, search, min_score=0.1)
        assert len(discovered) == 2
        a_links = session.execute(
            select(ExternalDiscussion).where(ExternalDiscussion.article_id == a.id)
        ).scalars().all()
        b_links = session.execute(
            select(ExternalDiscussion).where(ExternalDiscussion.article_id == b.id)
        ).scalars().all()
        assert len(a_links) == 1
        assert len(b_links) == 1


def test_discover_for_all_stories_empty_db(tmp_path):
    with session_scope(_db(tmp_path)) as session:
        assert discover_discussions_for_stories(session, lambda kw, s: []) == []


def test_run_triggers_discovery(tmp_path):
    db = _db(tmp_path)
    with patch("src.scraper.fetch_feed") as mock_fetch:
        mock_fetch.return_value = type("P", (), {"entries": [
            {"title": "AI breakthrough neural model", "link": "https://x.com/1",
             "summary": "deep learning", "published_parsed": (2024, 1, 1, 0, 0, 0, 0, 0, 0)},
        ]})()

        def search(keywords, story):
            return [{"platform": "hackernews",
                     "url": "https://news.ycombinator.com/item?id=99",
                     "title": "AI breakthrough neural model deep learning",
                     "comment_count": 8, "engagement_score": 20.0}]

        run(db, search_fn=search)

    with session_scope(db) as session:
        links = session.execute(select(ExternalDiscussion)).scalars().all()
        assert len(links) == 1
        assert links[0].platform == "hackernews"


def test_run_skips_discovery_when_search_fn_none(tmp_path):
    db = _db(tmp_path)
    with patch("src.scraper.fetch_feed") as mock_fetch:
        mock_fetch.return_value = type("P", (), {"entries": [
            {"title": "AI breakthrough", "link": "https://x.com/1",
             "summary": "neural", "published_parsed": (2024, 1, 1, 0, 0, 0, 0, 0, 0)},
        ]})()
        run(db, search_fn=None)

    with session_scope(db) as session:
        links = session.execute(select(ExternalDiscussion)).scalars().all()
        assert links == []
