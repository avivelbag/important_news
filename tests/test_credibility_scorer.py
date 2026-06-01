"""Tests for source credibility scoring, persistence, and moderator overrides."""

import datetime as dt

import pytest

import src.credibility_scorer as cs
import src.db as db
import src.models as models

NOW = dt.datetime(2024, 6, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
NOW_NAIVE = NOW.replace(tzinfo=None)


def _engine():
    engine = db.get_engine("sqlite://")
    db.init_db(engine)
    return engine


def _add_source(session, name="Feed", url="https://blog.example.com"):
    source = models.Source(name=name, url=url)
    session.add(source)
    session.flush()
    return source


def _add_story(session, source, *, vote_count=0, downvotes=0, published=NOW_NAIVE):
    story = models.Story(
        title=f"story-{source.id}-{vote_count}-{downvotes}",
        url=f"https://x/{source.id}/{vote_count}/{downvotes}/{published.isoformat()}",
        source_name=source.name,
        topic="ai",
        vote_count=vote_count,
        downvotes=downvotes,
        published_at=published,
        fetched_at=published,
        source=source,
    )
    session.add(story)
    session.flush()
    return story


# ---------------------------------------------------------------------------
# extract_domain / domain_authority
# ---------------------------------------------------------------------------


def test_extract_domain_strips_scheme_and_www():
    assert cs.extract_domain("https://www.nasa.gov/news") == "nasa.gov"
    assert cs.extract_domain("nasa.gov") == "nasa.gov"
    assert cs.extract_domain("http://blog.openai.com/x") == "blog.openai.com"
    assert cs.extract_domain("") == ""
    assert cs.extract_domain(None) == ""


def test_domain_authority_known_publications_score_high():
    assert cs.domain_authority("https://www.nasa.gov/x") == 100.0
    assert cs.domain_authority("https://arxiv.org/abs/1") == 95.0
    # Subdomain inherits the parent domain's authority.
    assert cs.domain_authority("https://blog.openai.com/post") == 90.0


def test_domain_authority_unknown_domain_gets_default():
    assert cs.domain_authority("https://some-random-blog.example") == (
        cs._UNKNOWN_DOMAIN_AUTHORITY
    )
    assert cs.domain_authority(None) == cs._UNKNOWN_DOMAIN_AUTHORITY


# ---------------------------------------------------------------------------
# vote_ratio
# ---------------------------------------------------------------------------


def test_vote_ratio_happy_path():
    assert cs.vote_ratio(8, 10) == pytest.approx(0.8)
    assert cs.vote_ratio(10, 10) == pytest.approx(1.0)
    assert cs.vote_ratio(0, 10) == pytest.approx(0.0)


def test_vote_ratio_no_votes_is_neutral():
    assert cs.vote_ratio(0, 0) == pytest.approx(0.5)
    assert cs.vote_ratio(5, 0) == pytest.approx(0.5)


def test_vote_ratio_clamped_to_unit_interval():
    assert cs.vote_ratio(15, 10) == pytest.approx(1.0)
    assert cs.vote_ratio(-5, 10) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# freshness_score
# ---------------------------------------------------------------------------


def test_freshness_score_decays_linearly():
    assert cs.freshness_score(NOW, NOW) == pytest.approx(100.0)
    half = NOW - dt.timedelta(days=cs._FRESHNESS_HORIZON_DAYS / 2)
    assert cs.freshness_score(half, NOW) == pytest.approx(50.0)


def test_freshness_score_floor_for_stale_or_missing():
    old = NOW - dt.timedelta(days=cs._FRESHNESS_HORIZON_DAYS + 10)
    assert cs.freshness_score(old, NOW) == 0.0
    assert cs.freshness_score(None, NOW) == 0.0


# ---------------------------------------------------------------------------
# compute_credibility
# ---------------------------------------------------------------------------


def test_compute_credibility_known_source_high():
    score = cs.compute_credibility(
        upvotes=90,
        total_votes=100,
        url="https://www.nasa.gov/x",
        last_published=NOW,
        now=NOW,
    )
    # 0.4*90 + 0.4*100 + 0.2*100 = 36 + 40 + 20 = 96
    assert score == pytest.approx(96.0)
    assert cs.credibility_tier(score) == cs.TIER_VERIFIED


def test_compute_credibility_unknown_downvoted_source_low():
    score = cs.compute_credibility(
        upvotes=1,
        total_votes=10,
        url="https://random-blog.example",
        last_published=NOW - dt.timedelta(days=60),
        now=NOW,
    )
    # 0.4*10 + 0.4*35 + 0.2*0 = 4 + 14 = 18
    assert score == pytest.approx(18.0)
    assert cs.credibility_tier(score) == cs.TIER_UNVERIFIED


def test_compute_credibility_is_clamped():
    score = cs.compute_credibility(
        upvotes=100, total_votes=100, url="https://nasa.gov", last_published=NOW,
        now=NOW,
    )
    assert 0.0 <= score <= 100.0


# ---------------------------------------------------------------------------
# tier / badge / multiplier
# ---------------------------------------------------------------------------


def test_tier_and_badge_buckets():
    assert cs.credibility_tier(85) == cs.TIER_VERIFIED
    assert cs.credibility_tier(55) == cs.TIER_COMMUNITY
    assert cs.credibility_tier(10) == cs.TIER_UNVERIFIED
    assert cs.credibility_badge(85) == "Verified Source"
    assert cs.credibility_badge(55) == "Community Submitted"
    assert cs.credibility_badge(10) == "Unverified"


def test_credibility_multiplier_and_weight():
    assert cs.credibility_multiplier(50) == pytest.approx(1.0)
    assert cs.credibility_multiplier(100) == pytest.approx(1.5)
    assert cs.credibility_multiplier(0) == pytest.approx(0.5)
    # Out-of-range scores are clamped before mapping.
    assert cs.credibility_multiplier(200) == pytest.approx(1.5)
    assert cs.weight_by_credibility(10.0, 100) == pytest.approx(15.0)
    assert cs.weight_by_credibility(10.0, 0) == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# recompute_source / recompute_all (DB-backed)
# ---------------------------------------------------------------------------


def test_recompute_source_persists_score_and_ratio():
    engine = _engine()
    session = db.get_session(engine)
    source = _add_source(session, name="NASA", url="https://www.nasa.gov")
    # net vote_count = up - down; downvotes = count of -1.
    _add_story(session, source, vote_count=7, downvotes=2)  # up=9, total=11
    _add_story(session, source, vote_count=4, downvotes=1)  # up=5, total=6
    session.commit()

    cred = cs.recompute_source(session, source, NOW)
    session.commit()
    # up=14, total=17 -> ratio ~0.8235
    assert cred.vote_ratio == pytest.approx(14 / 17)
    assert cred.is_verified is True
    assert cred.tier == cs.TIER_VERIFIED
    assert cred.updated_at == NOW_NAIVE
    session.close()


def test_recompute_source_no_stories_is_neutral_ratio():
    engine = _engine()
    session = db.get_session(engine)
    source = _add_source(session, name="Empty", url="https://random.example")
    session.commit()
    cred = cs.recompute_source(session, source, NOW)
    session.commit()
    assert cred.vote_ratio == pytest.approx(0.5)
    # neutral votes + unknown domain + no freshness -> below verified.
    assert cred.tier != cs.TIER_VERIFIED
    session.close()


def test_recompute_all_processes_every_source():
    engine = _engine()
    session = db.get_session(engine)
    _add_source(session, name="A", url="https://nasa.gov")
    _add_source(session, name="B", url="https://blog.example")
    session.commit()
    session.close()

    count = cs.recompute_all(engine, now=NOW)
    assert count == 2

    session = db.get_session(engine)
    assert session.query(models.SourceCredibility).count() == 2
    session.close()


# ---------------------------------------------------------------------------
# set_manual_override
# ---------------------------------------------------------------------------


def test_manual_override_wins_and_audits():
    engine = _engine()
    session = db.get_session(engine)
    source = _add_source(session, name="Blog", url="https://random.example")
    session.commit()

    cred = cs.set_manual_override(
        session, source.id, 95.0, moderator="mod1", reason="hand-vetted", now=NOW
    )
    session.commit()

    assert cred.manual_override == pytest.approx(95.0)
    assert cs.effective_score(cred) == pytest.approx(95.0)
    assert cred.tier == cs.TIER_VERIFIED
    assert cred.is_verified is True

    action = session.query(models.ModerationAction).filter_by(
        content_type="source"
    ).one()
    assert action.action == "credibility_override"
    assert action.moderator == "mod1"
    assert "hand-vetted" in action.detail
    session.close()


def test_manual_override_clamped():
    engine = _engine()
    session = db.get_session(engine)
    source = _add_source(session, name="Blog")
    session.commit()
    cred = cs.set_manual_override(
        session, source.id, 250.0, moderator="m", reason="r", now=NOW
    )
    session.commit()
    assert cred.manual_override == pytest.approx(100.0)
    session.close()


def test_clearing_override_reverts_to_computed_score():
    engine = _engine()
    session = db.get_session(engine)
    source = _add_source(session, name="NASA", url="https://www.nasa.gov")
    _add_story(session, source, vote_count=9, downvotes=1)
    session.commit()

    cs.recompute_source(session, source, NOW)
    cs.set_manual_override(
        session, source.id, 5.0, moderator="m", reason="penalty", now=NOW
    )
    session.commit()
    cred = cs.get_or_create_credibility(session, source.id)
    assert cs.effective_score(cred) == pytest.approx(5.0)

    cred = cs.set_manual_override(
        session, source.id, None, moderator="m", reason="lifted", now=NOW
    )
    session.commit()
    assert cred.manual_override is None
    # Effective score now falls back to the computed score, not the override.
    assert cs.effective_score(cred) == pytest.approx(cred.score)
    # Two override actions were audited (set + clear).
    assert session.query(models.ModerationAction).filter_by(
        content_type="source"
    ).count() == 2
    session.close()
