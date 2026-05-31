"""Tests for the data-source connectors and scraper pipeline.

All network access is replaced with in-memory fetch stubs so the suite is
deterministic and never touches the network or the real filesystem (the DB is
an in-memory SQLite engine).
"""
from __future__ import annotations

import datetime as _dt
import json

import pytest
import xml.etree.ElementTree as ET

from src.db import init_db, make_engine, session_scope
from src.models import Article, Source
from src.scraper import (
    DEFAULT_SOURCES,
    NormalizedItem,
    SourceSpec,
    categorize,
    ensure_source,
    fetch_hackernews,
    fetch_rss,
    insert_items,
    parse_rss,
    run_scraper,
    scrape_source,
)

RSS_AI = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>AI Feed</title>
  <item>
    <title>New neural network beats GPT on machine learning benchmark</title>
    <link>https://example.com/ai-1</link>
    <description>An AI breakthrough in deep learning.</description>
    <pubDate>Mon, 01 Jan 2024 12:00:00 +0000</pubDate>
  </item>
  <item>
    <title>Rocket launch to orbit by SpaceX</title>
    <link>https://example.com/aero-1</link>
    <description>A satellite reaches space.</description>
    <pubDate>Tue, 02 Jan 2024 08:30:00 GMT</pubDate>
  </item>
</channel></rss>
"""

ATOM_FEED = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Feed</title>
  <entry>
    <title>NASA satellite reaches Mars orbit</title>
    <link href="https://example.com/atom-1"/>
    <summary>An aerospace milestone for the spacecraft.</summary>
    <updated>2024-03-04T10:00:00Z</updated>
  </entry>
</feed>
"""


def _engine():
    engine = make_engine(":memory:")
    init_db(engine)
    return engine


def _make_fetcher(mapping):
    """Return a fetch stub backed by a ``{url: body}`` mapping."""

    def fetch(url):
        return mapping[url]

    return fetch


def test_categorize_picks_dominant_topic():
    ai_text = "machine learning model with a neural transformer and gpt"
    aero_text = "rocket launch to orbit satellite nasa spacecraft"
    assert categorize(ai_text) == "ai"
    assert categorize(aero_text) == "aerospace"


def test_categorize_falls_back_to_default_when_no_or_tied_keywords():
    assert categorize("nothing relevant here", default="ai") == "ai"
    assert categorize("nothing relevant here") is None
    # one ai keyword + one aerospace keyword -> tie -> default
    assert categorize("ai and space", default="aerospace") == "aerospace"


def test_parse_rss_extracts_and_categorizes_items():
    items = parse_rss(RSS_AI, default_category="ai")
    assert len(items) == 2
    first = items[0]
    assert first.title.startswith("New neural network")
    assert first.url == "https://example.com/ai-1"
    assert first.category == "ai"
    assert first.published_at == _dt.datetime(2024, 1, 1, 12, 0, 0)
    assert items[1].category == "aerospace"


def test_parse_rss_handles_atom_and_skips_incomplete_entries():
    incomplete = (
        '<?xml version="1.0"?><rss version="2.0"><channel>'
        "<item><title>No link here</title></item>"
        '<item><link>https://example.com/x</link></item>'
        "</channel></rss>"
    )
    assert parse_rss(incomplete) == []
    atom_items = parse_rss(ATOM_FEED, default_category="aerospace")
    assert len(atom_items) == 1
    assert atom_items[0].url == "https://example.com/atom-1"
    assert atom_items[0].category == "aerospace"


def test_parse_rss_raises_on_malformed_xml():
    with pytest.raises(ET.ParseError):
        parse_rss("<not valid xml")


def test_fetch_rss_respects_limit():
    spec = SourceSpec(name="Feed", url="http://feed", kind="rss", category="ai", limit=1)
    items = fetch_rss(spec, _make_fetcher({"http://feed": RSS_AI}))
    assert len(items) == 1


def test_fetch_hackernews_skips_textposts_and_parses_items():
    spec = SourceSpec(name="HN", url="http://hn/top", kind="hn", category="ai", limit=3)
    item_with_url = json.dumps(
        {
            "title": "OpenAI releases new LLM",
            "url": "https://example.com/hn-1",
            "time": 1704067200,
        }
    )
    ask_post = json.dumps({"title": "Ask HN: thoughts?", "time": 1704067200})
    mapping = {
        "http://hn/top": json.dumps([1, 2]),
        "https://hacker-news.firebaseio.com/v0/item/1.json": item_with_url,
        "https://hacker-news.firebaseio.com/v0/item/2.json": ask_post,
    }
    items = fetch_hackernews(spec, _make_fetcher(mapping))
    assert len(items) == 1
    assert items[0].url == "https://example.com/hn-1"
    assert items[0].category == "ai"
    assert items[0].published_at == _dt.datetime(2024, 1, 1, 0, 0, 0)


def test_insert_items_deduplicates_by_url():
    engine = _engine()
    spec = DEFAULT_SOURCES[0]
    items = [
        NormalizedItem(title="A", url="https://dup.com/1", category="ai"),
        NormalizedItem(title="A again", url="https://dup.com/1", category="ai"),
        NormalizedItem(title="B", url="https://dup.com/2", category="ai"),
    ]
    with session_scope(engine) as session:
        source = ensure_source(session, spec)
        inserted, skipped = insert_items(session, source, items)
    assert inserted == 2
    assert skipped == 1
    with session_scope(engine) as session:
        assert session.query(Article).count() == 2


def test_scrape_source_is_idempotent_across_runs():
    engine = _engine()
    spec = SourceSpec(name="Feed", url="http://feed", kind="rss", category="ai")
    fetch = _make_fetcher({"http://feed": RSS_AI})
    first = scrape_source(engine, spec, fetch)
    second = scrape_source(engine, spec, fetch)
    assert first == 2
    assert second == 0
    with session_scope(engine) as session:
        assert session.query(Article).count() == 2
        assert session.query(Source).count() == 1


def test_ensure_source_reuses_existing_row():
    engine = _engine()
    spec = DEFAULT_SOURCES[1]
    with session_scope(engine) as session:
        a = ensure_source(session, spec)
        a_id = a.id
    with session_scope(engine) as session:
        b = ensure_source(session, spec)
        assert b.id == a_id
        assert session.query(Source).count() == 1


def test_run_scraper_isolates_per_source_failures():
    engine = _engine()
    sources = [
        SourceSpec(name="Good", url="http://good", kind="rss", category="ai"),
        SourceSpec(name="Bad", url="http://bad", kind="rss", category="ai"),
    ]
    # "Bad" url is absent from the mapping -> KeyError inside the connector.
    fetch = _make_fetcher({"http://good": RSS_AI})
    result = run_scraper(engine, sources=sources, fetch=fetch)
    assert result.inserted == 2
    assert result.errors == 1
    assert result.per_source == {"Good": 2, "Bad": 0}
    with session_scope(engine) as session:
        assert session.query(Article).count() == 2


def test_scrape_source_rejects_unknown_kind():
    engine = _engine()
    spec = SourceSpec(name="X", url="http://x", kind="mystery", category="ai")
    with pytest.raises(ValueError, match="unknown source kind"):
        scrape_source(engine, spec, _make_fetcher({}))


def test_default_sources_cover_at_least_three_public_sources():
    assert len(DEFAULT_SOURCES) >= 3
    kinds = {s.kind for s in DEFAULT_SOURCES}
    assert "hn" in kinds and "rss" in kinds
    categories = {s.category for s in DEFAULT_SOURCES}
    assert {"ai", "aerospace"} <= categories
