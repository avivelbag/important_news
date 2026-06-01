"""Keyword search over stored stories with simple relevance scoring.

The Story model has no free-text *description* or *author* column, so search
matches the title (high weight) plus the secondary ``source_name`` and
``topic`` fields (low weight). Advanced refinement filters (source, topic,
score, comment-count, date range) are pushed into the SQL ``WHERE`` clause so
the database narrows rows on indexed columns before Python scores them for
relevance and applies the final ordering.
"""

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import func, select

from src.credibility_scorer import credibility_badge
from src.models import Story

MIN_QUERY_LEN = 2
MAX_QUERY_LEN = 100

# Allowed values for the ``sort`` filter, mapped to a human note in errors.
SORT_MODES = ("relevance", "recent", "score")

# Categories that a "both" story is considered a member of, so a topic filter
# for "ai" or "aerospace" also matches cross-cutting stories.
_BOTH_CATEGORIES = ("ai", "aerospace")

# A title hit is worth more than a hit in a secondary field, so a story whose
# title contains the query always outranks one that only mentions it in its
# source name or topic.
_TITLE_WEIGHT = 3
_SECONDARY_WEIGHT = 1


class SearchError(ValueError):
    """Raised when a search query or filter value fails validation."""


def validate_query(query: str) -> str:
    """Return the trimmed query if valid, else raise :class:`SearchError`.

    A valid query is between ``MIN_QUERY_LEN`` and ``MAX_QUERY_LEN`` characters
    after stripping surrounding whitespace.
    """
    if query is None:
        raise SearchError("query is required")
    trimmed = query.strip()
    if len(trimmed) < MIN_QUERY_LEN:
        raise SearchError(f"query must be at least {MIN_QUERY_LEN} characters")
    if len(trimmed) > MAX_QUERY_LEN:
        raise SearchError(f"query must be at most {MAX_QUERY_LEN} characters")
    return trimmed


@dataclass(frozen=True)
class SearchFilters:
    # ``sources``/``topics`` are OR-matched within each set; every other bound is
    # AND-combined. An empty ``SearchFilters()`` is a pure pass-through.
    sources: frozenset[str] = field(default_factory=frozenset)
    topics: frozenset[str] = field(default_factory=frozenset)
    min_score: int | None = None
    max_score: int | None = None
    min_comments: int | None = None
    date_from: dt.datetime | None = None
    date_to: dt.datetime | None = None
    sort: str = "relevance"


def _parse_csv(value: str | None) -> frozenset[str]:
    if not value:
        return frozenset()
    return frozenset(part.strip().lower() for part in value.split(",") if part.strip())


def _parse_int(value, name: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SearchError(f"{name} must be an integer") from exc


def _parse_date(value, name: str, end_of_day: bool = False) -> dt.datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        try:
            parsed = dt.datetime.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise SearchError(f"{name} must be an ISO 8601 date") from exc
        # A bare YYYY-MM-DD carries no time component; treat an inclusive
        # ``date_to`` bound as the very end of that calendar day.
        date_only = isinstance(value, str) and "T" not in value and len(value) == 10
        if date_only and end_of_day:
            parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return parsed


def build_filters(
    sources=None,
    topics=None,
    min_score=None,
    max_score=None,
    min_comments=None,
    date_from=None,
    date_to=None,
    sort="relevance",
) -> SearchFilters:
    """Build a validated :class:`SearchFilters` from raw request parameters.

    Comma-separated ``sources``/``topics`` strings are split into sets, numeric
    bounds are coerced to ints, and ISO dates are parsed (``date_to`` made
    end-of-day inclusive). Raises :class:`SearchError` for a malformed value or
    an unknown ``sort`` mode, so the API layer can surface a single 400.
    """
    if sort not in SORT_MODES:
        raise SearchError(f"sort must be one of {', '.join(SORT_MODES)}")
    return SearchFilters(
        sources=_parse_csv(sources),
        topics=_parse_csv(topics),
        min_score=_parse_int(min_score, "min_score"),
        max_score=_parse_int(max_score, "max_score"),
        min_comments=_parse_int(min_comments, "min_comments"),
        date_from=_parse_date(date_from, "date_from"),
        date_to=_parse_date(date_to, "date_to", end_of_day=True),
        sort=sort,
    )


def _topic_values(categories) -> set[str]:
    # Expand the requested categories into the concrete ``Story.topic`` values
    # that satisfy them: a "both" story counts for "ai" and "aerospace".
    values = set(categories)
    if values & set(_BOTH_CATEGORIES):
        values.add("both")
    return values


def _filter_clauses(filters: SearchFilters, category: str | None):
    # SQLAlchemy column predicates pushed into the WHERE clause so the database
    # filters on indexed columns before any row reaches Python.
    clauses = []
    if filters.sources:
        clauses.append(func.lower(Story.source_name).in_(filters.sources))
    if filters.topics:
        clauses.append(Story.topic.in_(_topic_values(filters.topics)))
    if category is not None:
        clauses.append(Story.topic.in_(_topic_values({category})))
    if filters.min_score is not None:
        clauses.append(Story.raw_score >= filters.min_score)
    if filters.max_score is not None:
        clauses.append(Story.raw_score <= filters.max_score)
    if filters.min_comments is not None:
        clauses.append(Story.comment_count >= filters.min_comments)
    if filters.date_from is not None:
        clauses.append(Story.published_at >= filters.date_from)
    if filters.date_to is not None:
        clauses.append(Story.published_at <= filters.date_to)
    return clauses


def _sort_key(sort: str):
    # Each key operates on a ``(relevance_score, story)`` pair; all modes fall
    # back to recency to keep ties stable. ``relevance`` keeps the existing
    # score→recency→votes ordering.
    if sort == "recent":
        return (lambda pair: (_published_key(pair[1]), pair[0]), True)
    if sort == "score":
        return (
            lambda pair: (pair[1].raw_score, _published_key(pair[1]), pair[0]),
            True,
        )
    # Relevance ties break on source credibility first (so high-trust sources
    # surface earlier), then recency, then votes.
    return (
        lambda pair: (
            pair[0],
            _credibility_key(pair[1]),
            _published_key(pair[1]),
            pair[1].vote_count,
        ),
        True,
    )


def score_story(story: Story, terms: list[str]) -> int:
    """Return the relevance score of *story* for the given lowercased *terms*.

    Each term contributes ``_TITLE_WEIGHT`` when found in the title and
    ``_SECONDARY_WEIGHT`` when found in the source name or topic. A score of 0
    means the story does not match and should be excluded from results.
    """
    title = story.title.lower()
    secondary = f"{story.source_name} {story.topic}".lower()
    score = 0
    for term in terms:
        if term in title:
            score += _TITLE_WEIGHT
        if term in secondary:
            score += _SECONDARY_WEIGHT
    return score


def _credibility_key(story: Story) -> float:
    return story.credibility_score if story.credibility_score is not None else 50.0


def _serialize(story: Story, score: int) -> dict:
    cred = _credibility_key(story)
    return {
        "id": story.id,
        "title": story.title,
        # No description column exists yet; kept in the contract for the UI.
        "description": "",
        "url": story.url,
        "source": story.source_name,
        "date": story.published_at.isoformat() if story.published_at else None,
        "score": score,
        "credibility_score": cred,
        "credibility_badge": credibility_badge(cred),
    }


def _published_key(story: Story) -> dt.datetime:
    # Treat a missing timestamp as the epoch so it sorts last (oldest).
    if story.published_at is None:
        return dt.datetime.min
    value = story.published_at
    if value.tzinfo is not None:
        value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value


def search_stories(
    session,
    query: str,
    category: str | None = None,
    limit: int = 50,
    filters: SearchFilters | None = None,
) -> list[dict]:
    """Search stored stories for *query*, optionally refined by *filters*.

    Returns a list of result dicts ``{id, title, description, url, source,
    date, score}``. Ordering follows ``filters.sort`` (default ``relevance``:
    score descending, then publication date descending, then ``vote_count`` as a
    final tiebreaker). Raises :class:`SearchError` if the query is invalid.

    *category* is the legacy single-topic filter kept for backwards
    compatibility; *filters* (a :class:`SearchFilters`) layers the advanced
    source/topic/score/comment-count/date constraints on top and may be combined
    with *category*. All such constraints are applied as SQL ``WHERE`` clauses on
    indexed columns; only the relevance scoring and final ordering run in Python.
    Matching is a case-insensitive substring test on each whitespace-separated
    term; only stories with a non-zero score are returned.
    """
    trimmed = validate_query(query)
    terms = [t for t in trimmed.lower().split() if t]
    if filters is None:
        filters = SearchFilters()

    stmt = select(Story)
    for clause in _filter_clauses(filters, category):
        stmt = stmt.where(clause)

    scored = []
    for story in session.scalars(stmt).all():
        score = score_story(story, terms)
        if score > 0:
            scored.append((score, story))

    key_func, reverse = _sort_key(filters.sort)
    scored.sort(key=key_func, reverse=reverse)
    results = [_serialize(story, score) for score, story in scored]
    if limit is not None and limit >= 0:
        results = results[:limit]
    return results
