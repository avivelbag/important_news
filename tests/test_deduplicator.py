import datetime as dt
import json

import pytest

import src.db as db
import src.deduplicator as dedup
import src.generate_site as generate_site
import src.models as models

NOW = dt.datetime(2024, 6, 1, 0, 0, 0)


def _engine():
    engine = db.get_engine("sqlite://")
    db.init_db(engine)
    return engine


def _add(session, *, title, url, source="A", published, raw=0, votes=0):
    story = models.Story(
        title=title,
        url=url,
        source_name=source,
        topic="ai",
        raw_score=raw,
        vote_count=votes,
        published_at=published,
        fetched_at=NOW,
    )
    session.add(story)
    return story


# --- URL normalization ---------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://example.com/a/", "https://example.com/a"),
        ("https://www.example.com/a", "https://example.com/a"),
        ("https://EXAMPLE.com/A", "https://example.com/A"),
        ("https://example.com/a?utm=1&x=2", "https://example.com/a"),
        ("https://example.com/a#section", "https://example.com/a"),
        ("https://example.com/a/?ref=feed#top", "https://example.com/a"),
    ],
)
def test_normalize_url_collapses_variations(raw, expected):
    assert dedup.normalize_url(raw) == expected


def test_normalize_url_variations_match_each_other():
    a = dedup.normalize_url("http://www.Example.com/story/?utm_source=hn")
    b = dedup.normalize_url("http://example.com/story")
    assert a == b


def test_normalize_url_shortener_ignores_query():
    a = dedup.normalize_url("https://bit.ly/abc?from=twitter")
    b = dedup.normalize_url("https://bit.ly/abc")
    assert a == b


def test_normalize_url_empty_returns_empty():
    assert dedup.normalize_url("") == ""
    assert dedup.normalize_url("   ") == ""


# --- title similarity ----------------------------------------------------


def test_title_similarity_identical_is_one():
    assert dedup.title_similarity("Hello World", "hello world") == 1.0


def test_title_similarity_near_match_over_threshold():
    score = dedup.title_similarity(
        "SpaceX launches new Starship rocket to orbit",
        "SpaceX launches a new Starship rocket to orbit",
    )
    assert score > dedup.DEFAULT_TITLE_THRESHOLD


def test_title_similarity_unrelated_is_low():
    score = dedup.title_similarity(
        "OpenAI releases GPT model", "NASA satellite reaches Mars"
    )
    assert score < dedup.DEFAULT_TITLE_THRESHOLD


def test_title_similarity_empty_is_zero():
    assert dedup.title_similarity("", "anything") == 0.0
    assert dedup.title_similarity("anything", "") == 0.0


# --- grouping ------------------------------------------------------------


def test_find_duplicate_groups_by_url_variation():
    engine = _engine()
    session = db.get_session(engine)
    a = _add(session, title="Story one", url="https://x.com/a", published=NOW)
    b = _add(
        session, title="Totally different headline", url="https://www.x.com/a/?utm=1",
        published=NOW,
    )
    session.flush()
    groups = dedup.find_duplicate_groups([a, b])
    assert len(groups) == 1
    assert {s.id for s in groups[0]} == {a.id, b.id}
    session.close()


def test_find_duplicate_groups_singletons_excluded():
    engine = _engine()
    session = db.get_session(engine)
    a = _add(session, title="Apple news here", url="https://x.com/1", published=NOW)
    b = _add(session, title="Banana update today", url="https://y.com/2", published=NOW)
    session.flush()
    assert dedup.find_duplicate_groups([a, b]) == []
    session.close()


# --- merge / deduplicate -------------------------------------------------


def test_deduplicate_merges_and_picks_earliest_canonical():
    engine = _engine()
    session = db.get_session(engine)
    earlier = NOW - dt.timedelta(hours=5)
    canonical = _add(
        session, title="Big AI breakthrough announced", url="https://hn.com/x",
        source="Hacker News", published=earlier, raw=10, votes=3,
    )
    later = _add(
        session, title="Big AI breakthrough announced!", url="https://reddit.com/y",
        source="Reddit", published=NOW, raw=5, votes=2,
    )
    session.commit()

    merged = dedup.deduplicate(engine, now=NOW)
    assert merged == 1

    session2 = db.get_session(engine)
    canon = session2.get(models.Story, canonical.id)
    dupe = session2.get(models.Story, later.id)
    assert canon.canonical_id is None
    assert dupe.canonical_id == canon.id
    # aggregated engagement
    assert canon.raw_score == 15
    assert canon.vote_count == 5
    # combined sources, earliest publish kept
    assert set(json.loads(canon.merged_sources)) == {"Hacker News", "Reddit"}
    assert canon.published_at == earlier
    session2.close()


def test_deduplicate_idempotent():
    engine = _engine()
    session = db.get_session(engine)
    _add(session, title="Same exact story title", url="https://a.com/1",
         source="A", published=NOW - dt.timedelta(hours=1), raw=4)
    _add(session, title="Same exact story title", url="https://b.com/2",
         source="B", published=NOW, raw=6)
    session.commit()

    first = dedup.deduplicate(engine, now=NOW)
    second = dedup.deduplicate(engine, now=NOW)
    assert first == 1
    assert second == 1  # stable, not re-summing into ever-growing totals

    session2 = db.get_session(engine)
    canon = (
        session2.query(models.Story)
        .filter(models.Story.canonical_id.is_(None))
        .one()
    )
    assert canon.raw_score == 10
    session2.close()


def test_deduplicate_no_duplicates_is_noop():
    engine = _engine()
    session = db.get_session(engine)
    _add(session, title="OpenAI releases a new language model", url="https://a.com/1",
         published=NOW)
    _add(session, title="SpaceX rocket reaches Mars orbit safely", url="https://b.com/2",
         published=NOW)
    session.commit()

    assert dedup.deduplicate(engine, now=NOW) == 0

    session2 = db.get_session(engine)
    rows = session2.query(models.Story).all()
    assert all(r.canonical_id is None for r in rows)
    assert all(r.merged_sources is None for r in rows)
    session2.close()


def test_deduplicate_empty_db():
    engine = _engine()
    assert dedup.deduplicate(engine, now=NOW) == 0


# --- site generator integration ------------------------------------------


def test_site_renders_canonical_once_with_all_sources(tmp_path):
    engine = _engine()
    session = db.get_session(engine)
    _add(session, title="Shared headline about rockets", url="https://hn.com/x",
         source="Hacker News", published=NOW - dt.timedelta(hours=2))
    _add(session, title="Shared headline about rockets", url="https://reddit.com/y",
         source="Reddit", published=NOW)
    session.commit()
    dedup.deduplicate(engine, now=NOW)

    out = generate_site.generate_site(engine, tmp_path)
    html = (out / "index.html").read_text(encoding="utf-8")

    # The story appears once and lists both sources.
    assert html.count("Shared headline about rockets") == 1
    assert "via Hacker News, Reddit" in html
