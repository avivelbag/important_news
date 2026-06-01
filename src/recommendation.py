"""Deterministic personalized feed ranked from a user's follows and votes."""

import datetime as dt

from sqlalchemy import select

from src.models import (
    ArticleTopic,
    Story,
    UserPreferences,
    UserTopicFollow,
    Vote,
)

VALID_ALGORITHMS = ("balanced", "trending", "recent", "followed")

# Fixed blends for the non-``balanced`` algorithms. ``balanced`` instead reads
# the user's stored topic/source/recency weights so it stays fully tunable.
_ALGORITHM_WEIGHTS = {
    "trending": {"topic": 0.2, "source": 0.1, "recency": 0.2, "trending": 0.5},
    "recent": {"topic": 0.2, "source": 0.1, "recency": 0.7, "trending": 0.0},
    "followed": {"topic": 1.0, "source": 0.0, "recency": 0.0, "trending": 0.0},
}


class RecommendationError(ValueError):
    pass


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _clean(user_id: str) -> str:
    cleaned = (user_id or "").strip()
    if not cleaned:
        raise RecommendationError("user_id must not be empty")
    return cleaned


def get_preferences(session, user_id: str) -> dict:
    name = _clean(user_id)
    prefs = session.scalars(
        select(UserPreferences).where(UserPreferences.user_id == name)
    ).first()
    if prefs is None:
        prefs = UserPreferences(user_id=name, created_at=_now())
        session.add(prefs)
        session.commit()
    return _prefs_dict(prefs)


def set_preferences(
    session,
    user_id: str,
    *,
    algorithm: str | None = None,
    min_score_threshold: float | None = None,
    topic_weight: float | None = None,
    source_weight: float | None = None,
    recency_weight: float | None = None,
) -> dict:
    name = _clean(user_id)
    prefs = session.scalars(
        select(UserPreferences).where(UserPreferences.user_id == name)
    ).first()
    if prefs is None:
        prefs = UserPreferences(user_id=name, created_at=_now())
        session.add(prefs)
        session.flush()

    if algorithm is not None:
        if algorithm not in VALID_ALGORITHMS:
            raise RecommendationError(
                f"algorithm must be one of {VALID_ALGORITHMS}"
            )
        prefs.recommendation_algorithm = algorithm
    if min_score_threshold is not None:
        prefs.min_score_threshold = float(min_score_threshold)

    weights = {
        "topic_weight": topic_weight,
        "source_weight": source_weight,
        "recency_weight": recency_weight,
    }
    for attr, value in weights.items():
        if value is None:
            continue
        value = float(value)
        if value < 0:
            raise RecommendationError(f"{attr} must not be negative")
        setattr(prefs, attr, value)
    if (
        prefs.topic_weight + prefs.source_weight + prefs.recency_weight
    ) <= 0:
        raise RecommendationError("weights must not all be zero")

    prefs.updated_at = _now()
    session.commit()
    return _prefs_dict(prefs)


def _prefs_dict(prefs: UserPreferences) -> dict:
    return {
        "user_id": prefs.user_id,
        "algorithm": prefs.recommendation_algorithm,
        "min_score_threshold": prefs.min_score_threshold,
        "topic_weight": prefs.topic_weight,
        "source_weight": prefs.source_weight,
        "recency_weight": prefs.recency_weight,
    }


def build_profile(session, user_id: str) -> dict:
    name = _clean(user_id)

    followed_topic_ids = set(
        session.scalars(
            select(UserTopicFollow.topic_id).where(
                UserTopicFollow.user_id == name
            )
        ).all()
    )

    upvoted = session.execute(
        select(Story.id, Story.source_name)
        .join(Vote, Vote.story_id == Story.id)
        .where(Vote.user_id == name, Vote.vote_value == 1)
    ).all()
    upvoted_story_ids = {row[0] for row in upvoted}
    source_counts: dict[str, int] = {}
    for _, source_name in upvoted:
        if source_name:
            source_counts[source_name] = source_counts.get(source_name, 0) + 1

    topic_ids = set(followed_topic_ids)
    if upvoted_story_ids:
        topic_ids.update(
            session.scalars(
                select(ArticleTopic.topic_id).where(
                    ArticleTopic.story_id.in_(upvoted_story_ids)
                )
            ).all()
        )

    return {
        "topic_ids": topic_ids,
        "source_counts": source_counts,
        "upvoted_story_ids": upvoted_story_ids,
    }


def _normalize(values: list[float]) -> list[float]:
    # All-equal inputs map to 0.0 so a non-varying signal does not bias ranking.
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.0 for _ in values]
    span = hi - lo
    return [(v - lo) / span for v in values]


def _resolve_weights(prefs: dict) -> dict:
    algorithm = prefs["algorithm"]
    if algorithm == "balanced":
        return {
            "topic": prefs["topic_weight"],
            "source": prefs["source_weight"],
            "recency": prefs["recency_weight"],
            "trending": 0.0,
        }
    return _ALGORITHM_WEIGHTS[algorithm]


def personalized_feed(
    session,
    user_id: str,
    *,
    algorithm: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    name = _clean(user_id)
    if algorithm is not None and algorithm not in VALID_ALGORITHMS:
        raise RecommendationError(f"algorithm must be one of {VALID_ALGORITHMS}")
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))

    prefs = get_preferences(session, name)
    if algorithm is not None:
        prefs = {**prefs, "algorithm": algorithm}
    active = prefs["algorithm"]
    weights = _resolve_weights(prefs)

    profile = build_profile(session, name)
    topic_ids = profile["topic_ids"]
    source_counts = profile["source_counts"]
    upvoted_story_ids = profile["upvoted_story_ids"]

    stmt = select(Story).where(
        Story.canonical_id.is_(None),
        Story.computed_score >= prefs["min_score_threshold"],
    )
    if upvoted_story_ids:
        stmt = stmt.where(Story.id.notin_(upvoted_story_ids))
    if active == "followed":
        if not topic_ids:
            return {
                "user_id": name,
                "algorithm": active,
                "limit": limit,
                "offset": offset,
                "total": 0,
                "stories": [],
            }
        tagged = select(ArticleTopic.story_id).where(
            ArticleTopic.topic_id.in_(topic_ids)
        )
        stmt = stmt.where(Story.id.in_(tagged))

    candidates = session.scalars(stmt).all()
    if not candidates:
        return {
            "user_id": name,
            "algorithm": active,
            "limit": limit,
            "offset": offset,
            "total": 0,
            "stories": [],
        }

    story_topic_ids = _story_topic_map(session, [s.id for s in candidates])
    max_source = max(source_counts.values()) if source_counts else 0

    topic_raw = [
        1.0 if story_topic_ids.get(s.id, set()) & topic_ids else 0.0
        for s in candidates
    ]
    source_raw = [
        (source_counts.get(s.source_name, 0) / max_source) if max_source else 0.0
        for s in candidates
    ]
    recency_raw = _normalize(
        [s.published_at.timestamp() if s.published_at else 0.0 for s in candidates]
    )
    trending_raw = _normalize([float(s.vote_count) for s in candidates])

    scored = []
    for i, story in enumerate(candidates):
        score = (
            weights["topic"] * topic_raw[i]
            + weights["source"] * source_raw[i]
            + weights["recency"] * recency_raw[i]
            + weights.get("trending", 0.0) * trending_raw[i]
        )
        scored.append((score, story))

    scored.sort(key=lambda pair: (pair[0], pair[1].id), reverse=True)
    total = len(scored)
    page = scored[offset : offset + limit]

    return {
        "user_id": name,
        "algorithm": active,
        "limit": limit,
        "offset": offset,
        "total": total,
        "stories": [_story_brief(story, score) for score, story in page],
    }


def _story_topic_map(session, story_ids: list[int]) -> dict[int, set[int]]:
    rows = session.execute(
        select(ArticleTopic.story_id, ArticleTopic.topic_id).where(
            ArticleTopic.story_id.in_(story_ids)
        )
    ).all()
    mapping: dict[int, set[int]] = {}
    for story_id, topic_id in rows:
        mapping.setdefault(story_id, set()).add(topic_id)
    return mapping


def _story_brief(story: Story, score: float) -> dict:
    return {
        "id": story.id,
        "url": story.url,
        "title": story.title,
        "topic": story.topic,
        "source_name": story.source_name,
        "computed_score": story.computed_score,
        "vote_count": story.vote_count,
        "relevance": round(score, 6),
        "published_at": (
            story.published_at.isoformat() if story.published_at else None
        ),
    }
