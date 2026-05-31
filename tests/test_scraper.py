import datetime as dt
import json
import xml.etree.ElementTree as ET

import pytest

import src.db as db
import src.models as models
import src.scraper as scraper

NOW = dt.datetime(2024, 6, 1, 0, 0, 0, tzinfo=dt.timezone.utc)

RSS_AI = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>AI Feed</title>
  <item>
    <title>New neural network beats GPT on a machine learning benchmark</title>
    <link>https://example.com/ai-1</link>
    <description>A deep learning breakthrough.</description>
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
    <updated>Wed, 03 Jan 2024 10:00:00 +0000</updated>
  </entry>
</feed>
"""


def _engine():
    engine = db.get_engine("sqlite://")
    db.init_db(engine)
    return engine


def _make_fetcher(mapping):
    def fetch(url):
        return mapping[url]

    return fetch


def test_categorize_picks_dominant_topic():
    assert scraper.categorize("a machine learning neural gpt model") == "ai"
    assert scraper.categorize("rocket launch to orbit satellite nasa") == "aerospace"


def test_categorize_both_and_default():
    assert scraper.categorize("gpt powers the new mars rocket") == "both"
    assert scraper.categorize("nothing relevant here", default="ai") == "ai"
    assert scraper.categorize("nothing relevant here") is None


def test_parse_rss_extracts_and_categorizes_items():
    items = scraper.parse_rss(RSS_AI, default_category="ai")
    assert len(items) == 2
    first = items[0]
    assert first.title.startswith("New neural network")
    assert first.url == "https://example.com/ai-1"
    assert first.category == "ai"
    assert first.published_at == dt.datetime(2024, 1, 1, 12, 0, 0)
    assert items[1].category == "aerospace"


def test_parse_rss_handles_atom_and_skips_incomplete_entries():
    incomplete = (
        '<?xml version="1.0"?><rss version="2.0"><channel>'
        "<item><title>No link here</title></item>"
        "<item><link>https://example.com/x</link></item>"
        "</channel></rss>"
    )
    assert scraper.parse_rss(incomplete) == []
    atom_items = scraper.parse_rss(ATOM_FEED, default_category="aerospace")
    assert len(atom_items) == 1
    assert atom_items[0].url == "https://example.com/atom-1"
    assert atom_items[0].category == "aerospace"
    assert atom_items[0].published_at == dt.datetime(2024, 1, 3, 10, 0, 0)


def test_parse_rss_raises_on_malformed_xml():
    with pytest.raises(ET.ParseError):
        scraper.parse_rss("<not valid xml")


def test_fetch_rss_respects_limit():
    spec = scraper.SourceSpec(
        name="Feed", url="http://feed", kind="rss", category="ai", limit=1
    )
    items = scraper.fetch_rss(spec, _make_fetcher({"http://feed": RSS_AI}))
    assert len(items) == 1


def test_fetch_hackernews_skips_textposts_and_parses_items():
    spec = scraper.SourceSpec(
        name="HN", url="http://hn/top", kind="hn", category="ai", limit=3
    )
    item_with_url = json.dumps(
        {
            "title": "OpenAI releases a new LLM",
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
    items = scraper.fetch_hackernews(spec, _make_fetcher(mapping))
    assert len(items) == 1
    assert items[0].url == "https://example.com/hn-1"
    assert items[0].category == "ai"
    assert items[0].published_at == dt.datetime(2024, 1, 1, 0, 0, 0)


def test_insert_items_deduplicates_by_url():
    engine = _engine()
    spec = scraper.DEFAULT_SOURCES[0]
    items = [
        scraper.NormalizedItem(title="A", url="https://dup.com/1", category="ai"),
        scraper.NormalizedItem(title="A again", url="https://dup.com/1", category="ai"),
        scraper.NormalizedItem(title="B", url="https://dup.com/2", category="ai"),
    ]
    session = db.get_session(engine)
    source = scraper.ensure_source(session, spec)
    inserted, skipped = scraper.insert_items(session, source, items, NOW)
    session.commit()
    session.close()
    assert inserted == 2
    assert skipped == 1
    check = db.get_session(engine)
    assert check.query(models.Story).count() == 2
    check.close()


def test_insert_items_falls_back_to_now_when_no_date():
    engine = _engine()
    spec = scraper.DEFAULT_SOURCES[0]
    items = [scraper.NormalizedItem(title="X", url="https://x.com/1", category="ai")]
    session = db.get_session(engine)
    source = scraper.ensure_source(session, spec)
    scraper.insert_items(session, source, items, NOW)
    session.commit()
    session.close()
    check = db.get_session(engine)
    story = check.query(models.Story).one()
    assert story.published_at == NOW.replace(tzinfo=None)
    assert story.fetched_at == NOW.replace(tzinfo=None)
    check.close()


def test_scrape_source_is_idempotent_across_runs():
    engine = _engine()
    spec = scraper.SourceSpec(name="Feed", url="http://feed", kind="rss", category="ai")
    fetch = _make_fetcher({"http://feed": RSS_AI})
    first = scraper.scrape_source(engine, spec, fetch, NOW)
    second = scraper.scrape_source(engine, spec, fetch, NOW)
    assert first == 2
    assert second == 0
    check = db.get_session(engine)
    assert check.query(models.Story).count() == 2
    assert check.query(models.Source).count() == 1
    check.close()


def test_ensure_source_reuses_existing_row():
    engine = _engine()
    spec = scraper.DEFAULT_SOURCES[1]
    s1 = db.get_session(engine)
    a_id = scraper.ensure_source(s1, spec).id
    s1.commit()
    s1.close()
    s2 = db.get_session(engine)
    b = scraper.ensure_source(s2, spec)
    assert b.id == a_id
    assert s2.query(models.Source).count() == 1
    s2.close()


def test_run_scraper_isolates_per_source_failures():
    engine = _engine()
    sources = [
        scraper.SourceSpec(name="Good", url="http://good", kind="rss", category="ai"),
        scraper.SourceSpec(name="Bad", url="http://bad", kind="rss", category="ai"),
    ]
    fetch = _make_fetcher({"http://good": RSS_AI})
    result = scraper.run_scraper(engine, sources=sources, fetch=fetch, now=NOW)
    assert result.inserted == 2
    assert result.errors == 1
    assert result.per_source == {"Good": 2, "Bad": 0}
    check = db.get_session(engine)
    assert check.query(models.Story).count() == 2
    check.close()


def test_scrape_source_rejects_unknown_kind():
    engine = _engine()
    spec = scraper.SourceSpec(name="X", url="http://x", kind="mystery", category="ai")
    with pytest.raises(ValueError, match="unknown source kind"):
        scraper.scrape_source(engine, spec, _make_fetcher({}), NOW)


def test_default_sources_cover_at_least_three_public_sources():
    assert len(scraper.DEFAULT_SOURCES) >= 3
    kinds = {s.kind for s in scraper.DEFAULT_SOURCES}
    assert "hn" in kinds and "rss" in kinds
    categories = {s.category for s in scraper.DEFAULT_SOURCES}
    assert {"ai", "aerospace"} <= categories
