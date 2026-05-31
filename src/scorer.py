import dataclasses
import datetime as dt
import os

from sqlalchemy import select

import src.db as db
import src.models as models

_DEFAULT_HALF_LIFE_HOURS = 168.0
_DEFAULT_SOURCE_WEIGHT = 1.0
_DEFAULT_CATEGORY_BOOST = 1.0
_DEFAULT_TRENDING_TOPICS = ("ai",)


def _parse_float(value, fallback):
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


@dataclasses.dataclass(frozen=True)
class ScoreWeights:
    half_life_hours: float = _DEFAULT_HALF_LIFE_HOURS
    source_weight: float = _DEFAULT_SOURCE_WEIGHT
    category_boost: float = _DEFAULT_CATEGORY_BOOST
    trending_topics: tuple = _DEFAULT_TRENDING_TOPICS

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        half_life = _parse_float(
            env.get("SCORER_HALF_LIFE_HOURS"), _DEFAULT_HALF_LIFE_HOURS
        )
        # A zero/negative half-life is meaningless and would divide by zero, so
        # fall back to the default rather than crash a scrape run.
        if half_life <= 0:
            half_life = _DEFAULT_HALF_LIFE_HOURS
        topics_raw = env.get("SCORER_TRENDING_TOPICS")
        if topics_raw is None:
            topics = _DEFAULT_TRENDING_TOPICS
        else:
            topics = tuple(t.strip() for t in topics_raw.split(",") if t.strip())
        return cls(
            half_life_hours=half_life,
            source_weight=_parse_float(
                env.get("SCORER_SOURCE_WEIGHT"), _DEFAULT_SOURCE_WEIGHT
            ),
            category_boost=_parse_float(
                env.get("SCORER_CATEGORY_BOOST"), _DEFAULT_CATEGORY_BOOST
            ),
            trending_topics=topics,
        )


def _naive_utc(value: dt.datetime) -> dt.datetime:
    # Stories are persisted as naive UTC; normalise any tz-aware input to match
    # so the subtraction below never mixes aware and naive datetimes.
    if value.tzinfo is not None:
        return value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value


def compute_score(
    story: models.Story,
    quality_weight: float,
    now: dt.datetime,
    weights: ScoreWeights,
) -> float:
    published = _naive_utc(story.published_at)
    now = _naive_utc(now)
    hours_old = max(0.0, (now - published).total_seconds() / 3600.0)
    decay = 0.5 ** (hours_old / weights.half_life_hours)
    quality = max(0.0, quality_weight) ** weights.source_weight
    boost = weights.category_boost if story.topic in weights.trending_topics else 1.0
    return decay * quality * boost


def recompute_scores(engine, now: dt.datetime | None = None, weights=None) -> int:
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    if weights is None:
        weights = ScoreWeights.from_env()
    session = db.get_session(engine)
    try:
        quality_by_name = {
            source.name: source.quality_weight
            for source in session.scalars(select(models.Source)).all()
        }
        stories = list(session.scalars(select(models.Story)).all())
        for story in stories:
            quality_weight = quality_by_name.get(story.source_name, 1.0)
            story.computed_score = compute_score(story, quality_weight, now, weights)
        session.commit()
        return len(stories)
    finally:
        session.close()
