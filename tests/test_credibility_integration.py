"""Integration tests for credibility ranking, search, stats, and API endpoints."""

import datetime as dt

import pytest
from fastapi.testclient import TestClient

import src.api as api
import src.credibility_scorer as cs
import src.db as db
import src.models as models
import src.scorer as scorer
import src.search as search

NOW = dt.datetime(2024, 6, 1, tzinfo=dt.timezone.utc)
NOW_NAIVE = NOW.replace(tzinfo=None)


def _engine():
    engine = db.get_engine("sqlite://")
    db.init_db(engine)
    return engine


def _add_source(session, name, url):
    source = models.Source(name=name, url=url)
    session.add(source)
    session.flush()
    return source


def _add_story(session, source, *, title, vote_count=0, downvotes=0, comment_count=0,
               published=NOW_NAIVE):
    story = models.Story(
        title=title,
        url=f"https://x/{source.id}/{title}",
        source_name=source.name,
        topic="ai",
        vote_count=vote_count,
        downvotes=downvotes,
        comment_count=comment_count,
        published_at=published,
        fetched_at=published,
        source=source,
    )
    session.add(story)
    session.flush()
    return story


# --- recompute_source: denormalisation + stats ------------------------------


def test_recompute_source_denormalizes_score_and_builds_stats():
    engine = _engine()
    session = db.get_session(engine)
    source = _add_source(session, "NASA", "https://www.nasa.gov")
    earliest = NOW_NAIVE - dt.timedelta(days=5)
    _add_story(session, source, title="a", vote_count=9, downvotes=1, comment_count=4,
               published=earliest)
    _add_story(session, source, title="b", vote_count=5, comment_count=2)
    session.commit()

    cred = cs.recompute_source(session, source, NOW)
    session.commit()

    stories = session.query(models.Story).filter_by(source_id=source.id).all()
    # Every story carries the denormalised effective score.
    assert all(s.credibility_score == pytest.approx(cs.effective_score(cred))
               for s in stories)

    stats = session.query(models.SourceStats).filter_by(source_id=source.id).one()
    assert stats.article_count == 2
    assert stats.avg_votes == pytest.approx(7.0)
    assert stats.avg_comments == pytest.approx(3.0)
    assert stats.established_date == earliest
    session.close()


def test_recompute_source_no_stories_has_empty_stats():
    engine = _engine()
    session = db.get_session(engine)
    source = _add_source(session, "Empty", "https://random.example")
    session.commit()
    cs.recompute_source(session, source, NOW)
    session.commit()
    stats = session.query(models.SourceStats).filter_by(source_id=source.id).one()
    assert stats.article_count == 0
    assert stats.avg_votes == 0.0
    assert stats.established_date is None
    session.close()


# --- ranking wiring (scorer) ------------------------------------------------


def test_recompute_scores_weights_by_credibility():
    engine = _engine()
    session = db.get_session(engine)
    high = _add_source(session, "NASA", "https://www.nasa.gov")
    low = _add_source(session, "Blog", "https://random.example")
    _add_story(session, high, title="hi", vote_count=10)
    _add_story(session, low, title="lo", vote_count=10)
    session.commit()
    cs.recompute_source(session, high, NOW)
    cs.recompute_source(session, low, NOW)
    session.commit()
    session.close()

    scorer.recompute_scores(engine, now=NOW)

    session = db.get_session(engine)
    hi = session.query(models.Story).filter_by(title="hi").one()
    lo = session.query(models.Story).filter_by(title="lo").one()
    # Same base inputs, but the high-credibility source's story scores higher.
    assert hi.computed_score > lo.computed_score
    session.close()


def test_recompute_scores_no_credibility_row_is_noop():
    engine = _engine()
    session = db.get_session(engine)
    source = _add_source(session, "Plain", "https://random.example")
    _add_story(session, source, title="s", vote_count=3)
    session.commit()
    session.close()

    # No SourceCredibility row exists: scoring must still succeed unchanged.
    count = scorer.recompute_scores(engine, now=NOW)
    assert count == 1
    session = db.get_session(engine)
    refreshed = session.query(models.Story).filter_by(title="s").one()
    assert refreshed.computed_score > 0
    session.close()


# --- search ordering + serialization ---------------------------------------


def test_search_ranks_high_credibility_first_and_emits_badge():
    engine = _engine()
    session = db.get_session(engine)
    high = _add_source(session, "NASA", "https://www.nasa.gov")
    low = _add_source(session, "Blog", "https://random.example")
    # Same title -> identical relevance; credibility breaks the tie.
    _add_story(session, high, title="rocket launch", published=NOW_NAIVE)
    _add_story(session, low, title="rocket launch", published=NOW_NAIVE)
    session.commit()
    cs.recompute_source(session, high, NOW)
    cs.recompute_source(session, low, NOW)
    session.commit()

    results = search.search_stories(session, "rocket")
    assert results[0]["source"] == "NASA"
    assert results[0]["credibility_score"] > results[1]["credibility_score"]
    assert "credibility_badge" in results[0]
    session.close()


def test_search_default_credibility_when_unscored():
    engine = _engine()
    session = db.get_session(engine)
    source = _add_source(session, "Blog", "https://random.example")
    _add_story(session, source, title="rocket launch")
    session.commit()
    results = search.search_stories(session, "rocket")
    assert results[0]["credibility_score"] == pytest.approx(50.0)
    assert results[0]["credibility_badge"] == "Community Submitted"
    session.close()


# --- service helpers --------------------------------------------------------


def test_credibility_report_missing_source_is_none():
    engine = _engine()
    session = db.get_session(engine)
    assert cs.credibility_report(session, 999) is None
    session.close()


def test_list_source_credibility_sorted_desc():
    engine = _engine()
    session = db.get_session(engine)
    high = _add_source(session, "NASA", "https://www.nasa.gov")
    low = _add_source(session, "Blog", "https://random.example")
    _add_story(session, high, title="a", vote_count=10)
    session.commit()
    cs.recompute_source(session, high, NOW)
    cs.recompute_source(session, low, NOW)
    session.commit()

    rows = cs.list_source_credibility(session)
    assert [r["name"] for r in rows] == ["NASA", "Blog"]
    assert rows[0]["effective_score"] >= rows[1]["effective_score"]
    session.close()


# --- API endpoints ----------------------------------------------------------


@pytest.fixture()
def api_engine(tmp_path, monkeypatch):
    eng = db.get_engine(f"sqlite:///{tmp_path / 'api.db'}")
    db.init_db(eng)
    monkeypatch.setattr(api, "_engine", eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def client(api_engine):
    return TestClient(api.app)


def _seed_source(engine, name="NASA", url="https://www.nasa.gov"):
    session = db.get_session(engine)
    source = _add_source(session, name, url)
    session.commit()
    sid = source.id
    session.close()
    return sid


def test_api_get_credibility(client, api_engine):
    sid = _seed_source(api_engine)
    resp = client.get(f"/api/sources/{sid}/credibility")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source_id"] == sid
    assert body["badge"] in ("Verified Source", "Community Submitted", "Unverified")
    assert "stats" in body


def test_api_get_credibility_unknown_source_404(client, api_engine):
    resp = client.get("/api/sources/4242/credibility")
    assert resp.status_code == 404


def test_api_admin_override_requires_token(client, api_engine):
    sid = _seed_source(api_engine)
    resp = client.post(f"/api/admin/sources/{sid}/credibility", json={"score": 90})
    assert resp.status_code == 403


def test_api_admin_override_sets_and_clears(client, api_engine):
    sid = _seed_source(api_engine)
    headers = {"X-Admin-Token": "swarm-admin"}

    resp = client.post(
        f"/api/admin/sources/{sid}/credibility",
        json={"score": 12, "reason": "penalty", "moderator": "mod1"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["manual_override"] == pytest.approx(12.0)
    assert resp.json()["badge"] == "Unverified"

    cleared = client.post(
        f"/api/admin/sources/{sid}/credibility",
        json={"reason": "lifted"},
        headers=headers,
    )
    assert cleared.status_code == 200
    assert cleared.json()["manual_override"] is None

    # The override + clear were both audited.
    session = db.get_session(api_engine)
    actions = session.query(models.ModerationAction).filter_by(
        content_type="source", content_id=sid
    ).count()
    assert actions == 2
    session.close()


def test_api_admin_override_rejects_non_numeric_score(client, api_engine):
    sid = _seed_source(api_engine)
    resp = client.post(
        f"/api/admin/sources/{sid}/credibility",
        json={"score": "high"},
        headers={"X-Admin-Token": "swarm-admin"},
    )
    assert resp.status_code == 400


def test_api_admin_list_sources(client, api_engine):
    _seed_source(api_engine, name="NASA", url="https://www.nasa.gov")
    _seed_source(api_engine, name="Blog", url="https://random.example")
    resp = client.get("/api/admin/sources", headers={"X-Admin-Token": "swarm-admin"})
    assert resp.status_code == 200
    names = {row["name"] for row in resp.json()}
    assert {"NASA", "Blog"} <= names
