"""Discover, store, verify, and render external discussion threads for stories."""

import datetime as dt
import json
import re
from urllib.parse import quote_plus, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from sqlalchemy import select

from src.models import ExternalDiscussion, Story

PLATFORMS = ("reddit", "github", "hn")

# Default cache window: a story discovered within this is not re-searched.
DEFAULT_TTL = dt.timedelta(hours=24)

# Minimum title-overlap score for a candidate to count as "about" the story.
DEFAULT_MIN_SCORE = 0.15

HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search"
HN_USER_AGENT = "important-news-scraper/1.0 (+https://example.com)"

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

_PLATFORM_LABELS = {"reddit": "Reddit", "github": "GitHub", "hn": "Hacker News"}


def _now() -> dt.datetime:
    # Naive UTC: SQLite drops tzinfo on round-trip, so storing naive keeps
    # written and read-back timestamps directly comparable for the cache guard.
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _as_naive(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def _tokenize(text: str) -> set[str]:
    return {
        tok
        for tok in _TOKEN_RE.findall((text or "").lower())
        if len(tok) >= 3 and tok not in _STOPWORDS
    }


def match_score(story_title: str, candidate_title: str) -> float:
    """Jaccard overlap of the two titles' topical tokens (0.0 when either is empty)."""
    a = _tokenize(story_title)
    b = _tokenize(candidate_title)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def normalize_url(url: str) -> str:
    """Canonicalise a URL so http/https, www, query, and trailing-slash variants collapse."""
    parts = urlsplit((url or "").strip())
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/")
    # Force https and drop query/fragment so trackers and ?utm= do not split rows.
    return urlunsplit(("https", host, path, "", ""))


def _http_get_json(url: str, *, timeout: float = 10.0) -> dict:
    request = Request(url, headers={"User-Agent": HN_USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return json.loads(response.read().decode(charset, errors="replace"))


def hn_search_fn(platform: str, query: str, *, fetch=_http_get_json, max_results: int = 5) -> list[dict]:
    """Real search adapter: query the auth-free HN Algolia API for the ``hn`` platform.

    Returns ``[]`` for every other platform (Reddit/GitHub adapters are not yet
    wired) and for any network/parse failure, so discovery degrades gracefully
    offline. Each hit becomes a candidate dict shaped for ``discover_for_story``.
    """
    if platform != "hn" or not (query or "").strip():
        return []
    url = f"{HN_SEARCH_URL}?query={quote_plus(query)}&tags=story&hitsPerPage={max_results}"
    try:
        data = fetch(url)
    except Exception:
        return []
    candidates = []
    for hit in data.get("hits", []):
        object_id = hit.get("objectID")
        if not object_id:
            continue
        candidates.append(
            {
                "platform": "hn",
                "url": f"https://news.ycombinator.com/item?id={object_id}",
                "title": hit.get("title") or hit.get("story_title") or "",
                "comment_count": int(hit.get("num_comments") or 0),
                "engagement_score": int(hit.get("points") or 0),
            }
        )
    return candidates


# Default production search function; injectable so tests stay offline.
default_search_fn = hn_search_fn


def discovered_within(
    session,
    story_id: int,
    ttl: dt.timedelta = DEFAULT_TTL,
    now: dt.datetime | None = None,
) -> bool:
    """True if the story already has a link discovered within ``ttl`` (cache guard)."""
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
    """Find external threads for a story via ``search_fn(platform, query)`` and persist new ones."""
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


def discover_discussions_for_stories(
    session,
    search_fn=default_search_fn,
    *,
    min_score: float = DEFAULT_MIN_SCORE,
    now: dt.datetime | None = None,
) -> list[ExternalDiscussion]:
    """Run discovery for every canonical story; the pipeline entry point."""
    stories = session.scalars(
        select(Story).where(Story.canonical_id.is_(None))
    ).all()
    created: list[ExternalDiscussion] = []
    for story in stories:
        created.extend(
            discover_for_story(session, story, search_fn, min_score=min_score, now=now)
        )
    return created


def verify_discussions(
    session,
    verify_fn,
    *,
    story_id: int | None = None,
    now: dt.datetime | None = None,
) -> dict:
    """Re-verify stored links via ``verify_fn``: refresh live metadata, prune dead links."""
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
    """Return a story's external discussions as render-ready dicts, ranked by engagement."""
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
