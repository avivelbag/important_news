"""Keyword search over stored stories with simple relevance scoring.

The Story model has no free-text *description* or *author* column, so search
matches the title (high weight) plus the secondary ``source_name`` and
``topic`` fields (low weight). Results are ordered by relevance score and then
by recency, mirroring how the rest of the app ranks stories.
"""

import datetime as dt

from sqlalchemy import select

from src.models import Story

MIN_QUERY_LEN = 2
MAX_QUERY_LEN = 100

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
) -> list[dict]:
    """Search stored stories for *query*, optionally filtered by *category*.

    Returns a list of result dicts ``{id, title, description, url, source,
    date, score}`` ordered by score descending, then publication date
    descending (most recent first when relevance is tied), then ``vote_count``
    descending as a final tiebreaker. Raises :class:`SearchError` if the query
    is invalid.

    Matching is a case-insensitive substring test on each whitespace-separated
    term; only stories with a non-zero score are returned.
    """
    trimmed = validate_query(query)
    terms = [t for t in trimmed.lower().split() if t]

    stories = session.scalars(select(Story)).all()
    scored = []
    for story in stories:
        if not _category_matches(story.topic, category):
            continue
        score = score_story(story, terms)
        if score > 0:
            scored.append((score, story))

    # Order by relevance, then recency, then vote_count so votes only break
    # ties left by the primary keys (existing score/recency orderings unchanged).
    scored.sort(
        key=lambda pair: (
            pair[0],
            _published_key(pair[1]),
            pair[1].vote_count,
        ),
        reverse=True,
    )
    results = [_serialize(story, score) for score, story in scored]
    if limit is not None and limit >= 0:
        results = results[:limit]
    return results
