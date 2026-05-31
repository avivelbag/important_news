"""Detect and merge near-duplicate stories.

Multiple sources frequently cover the same story, so the feed would otherwise
show the same item several times. This module groups stories that are the same
story — by normalized URL or by fuzzy title similarity — picks one canonical
survivor per group (the earliest published), links the rest to it via
``Story.canonical_id``, and folds the group's sources and engagement metrics
into the canonical row.
"""

import datetime as dt
import json
from difflib import SequenceMatcher
from urllib.parse import urlparse, urlunparse

from sqlalchemy import select

import src.db as db
import src.models as models

# Two stories whose titles match at or above this ratio are treated as the same
# story even when their URLs differ (e.g. a syndicated copy on another domain).
DEFAULT_TITLE_THRESHOLD = 0.8

# Hosts that only ever serve redirect stubs. We strip these down to the path so
# two short links to the same target collapse together; we never hit the
# network (that would make dedup non-deterministic and slow), so distinct short
# codes still stay distinct — this only helps when the *same* code reappears.
_SHORTENER_HOSTS = frozenset(
    {
        "bit.ly",
        "t.co",
        "goo.gl",
        "tinyurl.com",
        "ow.ly",
        "buff.ly",
        "is.gd",
        "dlvr.it",
    }
)


def normalize_url(url: str) -> str:
    """Return a canonical form of *url* for duplicate comparison.

    Collapses the common variations that make the same article look like two:
    scheme/host case, a leading ``www.``, query strings, fragments, and a
    trailing slash on the path. Shortener hosts are reduced to their bare code
    so repeated short links to one target match. Falls back to the stripped
    original if *url* is empty or unparseable.
    """
    if not url:
        return ""
    cleaned = url.strip()
    parsed = urlparse(cleaned)
    if not parsed.scheme and not parsed.netloc:
        # Bare string with no scheme (e.g. "Example.com/a/"); lower-case and
        # drop a trailing slash so it still normalises predictably.
        return cleaned.lower().rstrip("/")

    scheme = parsed.scheme.lower() or "http"
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]

    path = parsed.path.rstrip("/")
    if netloc in _SHORTENER_HOSTS:
        # Keep only the code; ignore any query the shortener tacked on.
        return urlunparse((scheme, netloc, path, "", "", ""))

    return urlunparse((scheme, netloc, path, "", "", ""))


def title_similarity(a: str, b: str) -> float:
    """Return a 0..1 similarity ratio between two titles.

    Uses :class:`difflib.SequenceMatcher` over case-folded, whitespace-
    collapsed titles so capitalisation and spacing differences do not lower the
    score. Empty titles yield ``0.0``.
    """
    na = " ".join((a or "").lower().split())
    nb = " ".join((b or "").lower().split())
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def _are_duplicates(a, b, threshold: float) -> bool:
    """True if stories *a* and *b* are the same story.

    Matches when their normalised URLs are identical, or when their titles are
    similar enough to clear *threshold*.
    """
    if normalize_url(a.url) == normalize_url(b.url):
        return True
    return title_similarity(a.title, b.title) >= threshold


def _canonical_of(group: list) -> object:
    """Pick the survivor of a duplicate group.

    The earliest ``published_at`` wins (the original report); ties break on the
    lowest id so the choice is deterministic.
    """
    return min(group, key=lambda s: (s.published_at, s.id))


def find_duplicate_groups(
    stories: list, threshold: float = DEFAULT_TITLE_THRESHOLD
) -> list:
    """Cluster *stories* into groups of duplicates.

    Runs a simple transitive (union-find style) grouping: if A matches B and B
    matches C, all three land in one group even if A and C do not directly
    match. Returns only groups with more than one member; singletons are not
    duplicates and are omitted.
    """
    n = len(stories)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(n):
        for j in range(i + 1, n):
            if _are_duplicates(stories[i], stories[j], threshold):
                union(i, j)

    clusters: dict = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(stories[i])
    return [members for members in clusters.values() if len(members) > 1]


def _merge_group(group: list) -> object:
    """Merge a duplicate *group* in place and return its canonical story.

    Keeps the earliest publish time (the canonical row already is the earliest),
    points every other member at the canonical via ``canonical_id``, records the
    distinct contributing source names on the canonical's ``merged_sources``, and
    moves each duplicate's ``raw_score``/``vote_count`` onto the canonical.

    Duplicate metrics are zeroed once folded in, which makes the merge
    idempotent: a re-run re-sums the same totals (the canonical's accumulated
    value plus zeros) rather than double-counting on every pass.
    """
    canonical = _canonical_of(group)

    sources: list = []
    total_raw = 0
    total_votes = 0
    for story in group:
        total_raw += story.raw_score or 0
        total_votes += story.vote_count or 0
        if story.source_name and story.source_name not in sources:
            sources.append(story.source_name)
        if story is not canonical:
            story.canonical_id = canonical.id
            story.raw_score = 0
            story.vote_count = 0

    canonical.canonical_id = None
    canonical.raw_score = total_raw
    canonical.vote_count = total_votes
    canonical.merged_sources = json.dumps(sources)
    return canonical


def deduplicate(
    engine,
    now: dt.datetime | None = None,
    threshold: float = DEFAULT_TITLE_THRESHOLD,
) -> int:
    """Find and merge duplicate stories in the database.

    Loads every story, clusters duplicates, and merges each cluster (see
    :func:`_merge_group`). Idempotent: re-running over an already-deduplicated
    set re-derives the same groups and writes the same canonical state, so it is
    safe to call on every scrape and refresh. Returns the number of stories that
    were marked as duplicates (i.e. linked to a canonical).
    """
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    session = db.get_session(engine)
    try:
        stories = list(session.scalars(select(models.Story)).all())
        groups = find_duplicate_groups(stories, threshold=threshold)
        merged_count = 0
        for group in groups:
            canonical = _merge_group(group)
            merged_count += sum(1 for s in group if s is not canonical)
        session.commit()
        return merged_count
    finally:
        session.close()
