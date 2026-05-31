"""Discover and link external discussion threads (Reddit, GitHub, HN) to stories.

A story rarely lives in isolation: the same AI/aerospace article is often
debated on Reddit, raised as a GitHub issue, or dissected on Hacker News.
This module finds those off-site threads and stores references to them so the
site can show "Discuss on Reddit / GitHub / HN" links with engagement context.

Network access is deliberately *injected*, never performed here. Discovery
takes a ``search_fn(platform, query) -> list[dict]`` and verification takes a
``verify_fn(discussion) -> dict | None``. The caller wires those to PRAW / the
GitHub API / the HN API in production and to a stub in tests, which keeps this
module deterministic and free of rate-limit / timeout flakiness.

Candidates are matched to a story by keyword overlap between titles (a
Jaccard-style token score), deduplicated by a normalised URL so the same
thread reached via different URL spellings collapses to one row, and cached:
discovery is skipped for a story whose links were refreshed within a TTL.
Verification re-checks each stored link, refreshing its metadata and deleting
dead ones (link rot).
"""

import datetime as dt
import re
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select

from src.models import ExternalDiscussion, Story

PLATFORMS = ("reddit", "github", "hn")

# Default cache window: a story discovered within this is not re-searched.
DEFAULT_TTL = dt.timedelta(hours=24)

# Minimum title-overlap score for a candidate to count as "about" the story.
DEFAULT_MIN_SCORE = 0.15

# Short/common words carry no topical signal and would inflate the overlap
# score, so they are dropped before comparing titles.
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
        "is", "are", "was", "how", "why", "what", "new", "this", "that", "from",
        "as", "at", "by", "be", "it", "its", "we", "you", "your", "via",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _now() -> dt.datetime:
    # Naive UTC: SQLite drops tzinfo on round-trip, so storing naive keeps
    # written and read-back timestamps directly comparable for the cache guard.
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _as_naive(value: dt.datetime) -> dt.datetime:
    """Strip tzinfo so DB-read (naive) and caller-supplied times compare."""
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def _tokenize(text: str) -> set[str]:
    """Lowercase *text* into the set of meaningful word tokens.

    Tokens shorter than three characters and stopwords are dropped so the
    overlap score reflects topical words (e.g. "transformer", "starship")
    rather than glue words.
    """
    return {
        tok
        for tok in _TOKEN_RE.findall((text or "").lower())
        if len(tok) >= 3 and tok not in _STOPWORDS
    }


def match_score(story_title: str, candidate_title: str) -> float:
    """Return a 0..1 keyword-overlap score between two titles.

    The score is the Jaccard index of their token sets (intersection over
    union). Two titles with no meaningful tokens score 0.0 rather than dividing
    by zero, so an empty or all-stopword title never matches anything.
    """
    a = _tokenize(story_title)
    b = _tokenize(candidate_title)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def normalize_url(url: str) -> str:
    """Canonicalise *url* so URL spelling variants collapse to one key.

    Lowercases the scheme and host, drops a leading ``www.``, treats http and
    https as the same by forcing ``https``, strips the query string, fragment,
    and a trailing slash on the path. This is what makes dedup resilient to the
    URL variations the same thread is linked with across platforms.
    """
    parts = urlsplit((url or "").strip())
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/")
    # Force https and drop query/fragment so trackers and ?utm= do not split rows.
    return urlunsplit(("https", host, path, "", ""))


def discovered_within(
    session,
    story_id: int,
    ttl: dt.timedelta = DEFAULT_TTL,
    now: dt.datetime | None = None,
) -> bool:
    """Return True if *story_id* already has a link discovered within *ttl*.

    Used as the cache guard: when this is true the caller should skip the
    external search entirely to avoid repeated API calls. ``now`` is injectable
    for deterministic tests.
    """
    now = _as_naive(now or _now())
    cutoff = now - ttl
    latest = session.scalar(
        select(ExternalDiscussion.discovered_at)
        .where(ExternalDiscussion.story_id == story_id)
        .order_by(ExternalDiscussion.discovered_at.desc())
        .limit(1)
    )
    return latest is not None and _as_naive(latest) >= cutoff


def discover_for_story(
    session,
    story: Story,
    search_fn,
    *,
    platforms=PLATFORMS,
    min_score: float = DEFAULT_MIN_SCORE,
    ttl: dt.timedelta = DEFAULT_TTL,
    force: bool = False,
    now: dt.datetime | None = None,
) -> list[ExternalDiscussion]:
    """Find external threads for *story* via *search_fn* and persist new ones.

    For each platform ``search_fn(platform, story.title)`` is called and is
    expected to return an iterable of candidate dicts with ``url`` and
    ``title`` keys (``comment_count`` / ``engagement_score`` optional, default
    0). Candidates whose title overlap with the story is below *min_score* are
    dropped, the rest are deduplicated by normalised URL — both against rows
    already stored and within this batch — and the survivors inserted with
    ``discovered_at`` / ``last_verified_at`` set to *now*.

    Caching: if the story already has a link discovered within *ttl* the search
    is skipped entirely and ``[]`` returned, unless *force* is set. A
    ``search_fn`` that raises for one platform (rate limit, timeout) is caught
    so the remaining platforms still contribute. ``now`` is injectable for
    deterministic tests. Returns the newly created rows.
    """
    now = _as_naive(now or _now())
    if not force and discovered_within(session, story.id, ttl, now):
        return []

    existing = set(
        session.scalars(
            select(ExternalDiscussion.url).where(
                ExternalDiscussion.story_id == story.id
            )
        ).all()
    )

    created: list[ExternalDiscussion] = []
    for platform in platforms:
        try:
            candidates = search_fn(platform, story.title)
        except Exception:
            # One flaky platform must not abort discovery for the others.
            continue
        for cand in candidates or []:
            url = cand.get("url")
            title = cand.get("title")
            if not url or not title:
                continue
            if match_score(story.title, title) < min_score:
                continue
            norm = normalize_url(url)
            if norm in existing:
                continue
            existing.add(norm)
            row = ExternalDiscussion(
                story_id=story.id,
                platform=platform,
                url=norm,
                title=title,
                comment_count=int(cand.get("comment_count") or 0),
                engagement_score=int(cand.get("engagement_score") or 0),
                discovered_at=now,
                last_verified_at=now,
            )
            session.add(row)
            created.append(row)

    if created:
        session.commit()
    return created


def verify_discussions(
    session,
    verify_fn,
    *,
    story_id: int | None = None,
    now: dt.datetime | None = None,
) -> dict:
    """Re-verify stored links via *verify_fn*, refreshing or pruning each.

    Optionally scoped to a single *story_id* (default: all). For each stored
    discussion ``verify_fn(discussion)`` is called and must return either an
    updated-metadata dict (any of ``title`` / ``comment_count`` /
    ``engagement_score``) for a live link, or ``None`` to signal the link is
    dead and should be removed. Live links have their fields refreshed and
    ``last_verified_at`` stamped to *now*; dead links are deleted. A
    ``verify_fn`` that raises for one row leaves that row untouched (treated as
    "could not verify", not "dead"). ``now`` is injectable. Returns a summary
    dict ``{"verified": n, "removed": n, "errors": n}``.
    """
    now = _as_naive(now or _now())
    stmt = select(ExternalDiscussion)
    if story_id is not None:
        stmt = stmt.where(ExternalDiscussion.story_id == story_id)
    rows = session.scalars(stmt).all()

    verified = removed = errors = 0
    for row in rows:
        try:
            result = verify_fn(row)
        except Exception:
            errors += 1
            continue
        if result is None:
            session.delete(row)
            removed += 1
            continue
        if "title" in result and result["title"]:
            row.title = result["title"]
        if "comment_count" in result and result["comment_count"] is not None:
            row.comment_count = int(result["comment_count"])
        if "engagement_score" in result and result["engagement_score"] is not None:
            row.engagement_score = int(result["engagement_score"])
        row.last_verified_at = now
        verified += 1

    session.commit()
    return {"verified": verified, "removed": removed, "errors": errors}


def get_discussions(session, story_id: int) -> list[dict]:
    """Return *story_id*'s external discussions as dicts, ranked for display.

    Ordered by engagement (then comment count, then id for a stable tie-break)
    so the most active thread per platform surfaces first. Returns ``[]`` for a
    story with no linked discussions. The dicts are render-ready: each carries
    the platform, url, title, counts, and a human ``platform_label``.
    """
    rows = session.scalars(
        select(ExternalDiscussion)
        .where(ExternalDiscussion.story_id == story_id)
        .order_by(
            ExternalDiscussion.engagement_score.desc(),
            ExternalDiscussion.comment_count.desc(),
            ExternalDiscussion.id.asc(),
        )
    ).all()
    return [
        {
            "id": r.id,
            "platform": r.platform,
            "platform_label": _PLATFORM_LABELS.get(r.platform, r.platform.title()),
            "url": r.url,
            "title": r.title,
            "comment_count": r.comment_count,
            "engagement_score": r.engagement_score,
        }
        for r in rows
    ]


_PLATFORM_LABELS = {"reddit": "Reddit", "github": "GitHub", "hn": "Hacker News"}
