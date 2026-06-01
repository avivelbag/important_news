"""Keyword search over stored stories with simple relevance scoring.

The Story model has no free-text *description* or *author* column, so search
matches the title (high weight) plus the secondary ``source_name`` and
``topic`` fields (low weight). Results are ordered by relevance score and then
by recency, mirroring how the rest of the app ranks stories.
"""

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import select

from src.models import Story

MIN_QUERY_LEN = 2
MAX_QUERY_LEN = 100

# Allowed values for the ``sort`` filter, mapped to a human note in errors.
SORT_MODES = ("relevance", "recent", "score")

# A title hit is worth more than a hit in a secondary field, so a story whose
# title contains the query always outranks one that only mentions it in its
# source name or topic.
_TITLE_WEIGHT = 3
_SECONDARY_WEIGHT = 1


class SearchError(ValueError):
    """Raised when a search query fails validation (too short/long/empty)."""


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
    """Refinement filters layered on top of a keyword search.

    Every field defaults to "no constraint" so an empty ``SearchFilters()`` is a
    pure pass-through. ``sources`` and ``topics`` are matched as OR-sets (a story
    passes if it matches any listed value); the remaining bounds are AND-combined
    with each other and with the source/topic sets. ``sort`` selects the result
    ordering and must be one of :data:`SORT_MODES`.
    """

    sources: frozenset[str] = field(default_factory=frozenset)
    topics: frozenset[str] = field(default_factory=frozenset)
    min_score: int | None = None
    max_score: int | None = None
    min_comments: int | None = None
    date_from: dt.datetime | None = None
    date_to: dt.datetime | None = None
    sort: str = "relevance"


def _parse_csv(value: str | None) -> frozenset[str]:
    """Split a comma-separated parameter into a lowercased set of tokens.

    Empty/whitespace tokens are dropped, so ``"hn, ,Reddit"`` yields
    ``{"hn", "reddit"}`` and ``None``/``""`` yields the empty set.
    """
    if not value:
        return frozenset()
    return frozenset(part.strip().lower() for part in value.split(",") if part.strip())


def _parse_int(value, name: str) -> int | None:
    """Coerce *value* to an int, raising :class:`SearchError` on bad input.

    ``None`` and empty strings pass through as ``None`` (no constraint).
    """
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SearchError(f"{name} must be an integer") from exc


def _parse_date(value, name: str, end_of_day: bool = False) -> dt.datetime | None:
    """Parse an ISO-8601 date/datetime into a naive-UTC :class:`datetime`.

    A bare ``YYYY-MM-DD`` is anchored to the start of that day, or the *end* of
    the day (``23:59:59.999999``) when *end_of_day* is set, so an inclusive
    ``date_to`` bound covers the whole calendar day. Timezone-aware inputs are
    normalised to naive UTC to match how stories are compared. ``None``/empty
    passes through; anything unparseable raises :class:`SearchError`.
    """
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        try:
            parsed = dt.datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise SearchError(f"{name} must be an ISO 8601 date") from exc
        date_only = len(str(value)) == 10
        if date_only and end_of_day:
            parsed = parsed.replace(
                hour=23, minute=59, second=59, microsecond=999999
            )
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


def _passes_filters(story: Story, filters: SearchFilters) -> bool:
    """Return whether *story* satisfies every constraint in *filters*.

    Source and topic sets are OR-matched (membership in the listed values);
    score, comment-count and date bounds are inclusive AND constraints. A story
    with no ``raw_score``/``comment_count`` still compares correctly because
    those columns default to 0.
    """
    if filters.sources and story.source_name.lower() not in filters.sources:
        return False
    if filters.topics and not any(
        _category_matches(story.topic, topic) for topic in filters.topics
    ):
        return False
    if filters.min_score is not None and story.raw_score < filters.min_score:
        return False
    if filters.max_score is not None and story.raw_score > filters.max_score:
        return False
    if filters.min_comments is not None and story.comment_count < filters.min_comments:
        return False
    if filters.date_from is not None or filters.date_to is not None:
        published = _published_key(story)
        if filters.date_from is not None and published < filters.date_from:
            return False
        if filters.date_to is not None and published > filters.date_to:
            return False
    return True


def _sort_key(sort: str):
    """Return a ``(key_func, reverse)`` pair for the requested *sort* mode.

    Each key operates on a ``(relevance_score, story)`` pair. ``relevance`` keeps
    the existing score→recency→votes ordering; ``recent`` orders purely by
    publication date; ``score`` orders by the story's own ``raw_score``. All
    modes fall back to recency to keep ties stable.
    """
    if sort == "recent":
        return (lambda pair: (_published_key(pair[1]), pair[0]), True)
    if sort == "score":
        return (
            lambda pair: (pair[1].raw_score, _published_key(pair[1]), pair[0]),
            True,
        )
    return (
        lambda pair: (pair[0], _published_key(pair[1]), pair[1].vote_count),
        True,
    )


def _category_matches(topic: str, category: str | None) -> bool:
    """Return whether *topic* belongs to the requested *category*.

    ``None`` means no filter. A "both" story counts as both "ai" and
    "aerospace", matching the semantics used by the static-site filter JS.
    """
    if category is None:
        return True
    if topic == category:
        return True
    if topic == "both" and category in ("ai", "aerospace"):
        return True
    return False


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


def _serialize(story: Story, score: int) -> dict:
    """Render a Story into the JSON-friendly search-result shape."""
    return {
        "id": story.id,
        "title": story.title,
        # No description column exists yet; kept in the contract for the UI.
        "description": "",
        "url": story.url,
        "source": story.source_name,
        "date": story.published_at.isoformat() if story.published_at else None,
        "score": score,
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
    with *category*. Matching is a case-insensitive substring test on each
    whitespace-separated term; only stories with a non-zero score that also pass
    every active filter are returned.
    """
    trimmed = validate_query(query)
    terms = [t for t in trimmed.lower().split() if t]
    if filters is None:
        filters = SearchFilters()

    stories = session.scalars(select(Story)).all()
    scored = []
    for story in stories:
        if not _category_matches(story.topic, category):
            continue
        if not _passes_filters(story, filters):
            continue
        score = score_story(story, terms)
        if score > 0:
            scored.append((score, story))

    key_func, reverse = _sort_key(filters.sort)
    scored.sort(key=key_func, reverse=reverse)
    results = [_serialize(story, score) for score, story in scored]
    if limit is not None and limit >= 0:
        results = results[:limit]
    return results
