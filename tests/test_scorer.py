import datetime as dt

import pytest

import src.db as db
import src.models as models
import src.scorer as scorer

NOW = dt.datetime(2024, 6, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
DEFAULT = scorer.ScoreWeights()


def _engine():
    engine = db.get_engine("sqlite://")
    db.init_db(engine)
    return engine


def _story(topic="aerospace", hours_old=0.0):
    published = NOW.replace(tzinfo=None) - dt.timedelta(hours=hours_old)
    return models.Story(
        title="t",
        url=f"https://example.com/{topic}-{hours_old}",
        source_name="src",
        topic=topic,
        published_at=published,
        fetched_at=published,
    )


def test_fresh_article_scores_higher_than_old():
    fresh = scorer.compute_score(_story(hours_old=0.0), 1.0, NOW, DEFAULT)
    old = scorer.compute_score(_story(hours_old=336.0), 1.0, NOW, DEFAULT)
    assert fresh == pytest.approx(1.0)
    assert fresh > old
    assert old == pytest.approx(0.25)


def test_half_life_halves_score():
    half_life = DEFAULT.half_life_hours
    score = scorer.compute_score(_story(hours_old=half_life), 1.0, NOW, DEFAULT)
    assert score == pytest.approx(0.5)


def test_source_quality_weight_scales_score():
    base = scorer.compute_score(_story(hours_old=0.0), 1.0, NOW, DEFAULT)
    boosted = scorer.compute_score(_story(hours_old=0.0), 2.0, NOW, DEFAULT)
    assert boosted == pytest.approx(2.0 * base)


def test_category_boost_only_applies_to_trending_topics():
    weights = scorer.ScoreWeights(category_boost=3.0, trending_topics=("ai",))
    ai = scorer.compute_score(_story(topic="ai"), 1.0, NOW, weights)
    aero = scorer.compute_score(_story(topic="aerospace"), 1.0, NOW, weights)
    assert ai == pytest.approx(3.0)
    assert aero == pytest.approx(1.0)


def test_future_publish_date_is_clamped():
    score = scorer.compute_score(_story(hours_old=-100.0), 1.0, NOW, DEFAULT)
    assert score == pytest.approx(1.0)


def test_score_is_deterministic_across_calls():
    story = _story(topic="ai", hours_old=72.0)
    first = scorer.compute_score(story, 1.5, NOW, DEFAULT)
    second = scorer.compute_score(story, 1.5, NOW, DEFAULT)
    assert first == second


def test_recompute_scores_persists_and_orders_feed():
    engine = _engine()
    session = db.get_session(engine)
    source = models.Source(name="src", url="http://src", quality_weight=2.0)
    session.add(source)
    session.add(_story(topic="aerospace", hours_old=0.0))
    session.add(_story(topic="aerospace", hours_old=500.0))
    session.commit()
    session.close()

    count = scorer.recompute_scores(engine, now=NOW, weights=DEFAULT)
    assert count == 2

    check = db.get_session(engine)
    stories = sorted(
        check.query(models.Story).all(), key=lambda s: s.computed_score, reverse=True
    )
    assert stories[0].computed_score > stories[1].computed_score
    assert stories[0].computed_score == pytest.approx(2.0)
    check.close()


def test_recompute_uses_default_weight_for_unknown_source():
    engine = _engine()
    session = db.get_session(engine)
    session.add(_story(topic="aerospace", hours_old=0.0))
    session.commit()
    session.close()

    scorer.recompute_scores(engine, now=NOW, weights=DEFAULT)
    check = db.get_session(engine)
    story = check.query(models.Story).one()
    assert story.computed_score == pytest.approx(1.0)
    check.close()


def test_recompute_on_empty_db_returns_zero():
    engine = _engine()
    assert scorer.recompute_scores(engine, now=NOW, weights=DEFAULT) == 0


def test_recompute_is_reproducible_across_regenerations():
    engine = _engine()
    session = db.get_session(engine)
    session.add(models.Source(name="src", url="http://src", quality_weight=1.5))
    session.add(_story(topic="ai", hours_old=48.0))
    session.commit()
    session.close()

    scorer.recompute_scores(engine, now=NOW, weights=DEFAULT)
    first = db.get_session(engine)
    score_a = first.query(models.Story).one().computed_score
    first.close()

    scorer.recompute_scores(engine, now=NOW, weights=DEFAULT)
    second = db.get_session(engine)
    score_b = second.query(models.Story).one().computed_score
    second.close()

    assert score_a == score_b


def test_weights_from_env_parses_and_falls_back():
    env = {
        "SCORER_HALF_LIFE_HOURS": "72",
        "SCORER_SOURCE_WEIGHT": "2",
        "SCORER_CATEGORY_BOOST": "1.5",
        "SCORER_TRENDING_TOPICS": "ai, aerospace",
    }
    weights = scorer.ScoreWeights.from_env(env)
    assert weights.half_life_hours == pytest.approx(72.0)
    assert weights.source_weight == pytest.approx(2.0)
    assert weights.category_boost == pytest.approx(1.5)
    assert weights.trending_topics == ("ai", "aerospace")


def test_weights_from_env_uses_defaults_on_bad_input():
    env = {"SCORER_HALF_LIFE_HOURS": "not-a-number", "SCORER_SOURCE_WEIGHT": ""}
    weights = scorer.ScoreWeights.from_env(env)
    assert weights.half_life_hours == pytest.approx(168.0)
    assert weights.source_weight == pytest.approx(1.0)
    assert weights.trending_topics == ("ai",)


def test_weights_from_env_rejects_nonpositive_half_life():
    weights = scorer.ScoreWeights.from_env({"SCORER_HALF_LIFE_HOURS": "0"})
    assert weights.half_life_hours == pytest.approx(168.0)
