"""Fetch, extract, store and prune archived full-text for scraped stories.

Source articles regularly disappear or change URLs; caching the page body keeps
the feed readable after the original is gone. Content extraction uses only the
stdlib :mod:`html.parser` so the project gains no new third-party dependency.
"""

from datetime import datetime, timezone
from html.parser import HTMLParser

import requests

import src.db as db
import src.models as models

DEFAULT_TIMEOUT = 10
USER_AGENT = "important-news-scraper/1.0 (+https://example.com)"

# Tags whose textual content is markup/noise, never article prose. Their inner
# text (and that of their descendants) is dropped so the cached plaintext stays
# readable.
_SKIP_TAGS = {"script", "style", "head", "noscript", "template", "svg"}


class _TextExtractor(HTMLParser):
    """Collect human-readable text from an HTML document.

    Text inside any tag listed in ``_SKIP_TAGS`` (and its descendants) is
    discarded; everything else is accumulated as stripped, newline-joined
    chunks. A depth counter keeps nested skip tags balanced.
    """

    def __init__(self):
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            text = data.strip()
            if text:
                self._chunks.append(text)

    def get_text(self) -> str:
        return "\n".join(self._chunks)


def extract_text(html: str) -> str:
    """Return readable plaintext for an HTML document.

    Tags are stripped and script/style/head content removed, leaving only the
    visible prose. Malformed HTML is tolerated (the stdlib parser is lenient).
    """
    parser = _TextExtractor()
    parser.feed(html)
    return parser.get_text()


def fetch_article(url: str, timeout: int = DEFAULT_TIMEOUT):
    """Fetch ``url`` and return ``(html, text)``, or ``None`` on any failure.

    Network timeouts, connection errors and non-2xx responses all collapse to
    ``None`` so callers can skip caching without special-casing each error type;
    a single dead URL therefore never aborts a bulk sync.
    """
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        resp.raise_for_status()
    except Exception:
        return None
    html = resp.text
    return html, extract_text(html)


def cache_story_content(
    story: models.Story, timeout: int = DEFAULT_TIMEOUT, now: datetime | None = None
) -> bool:
    """Fetch a story's source page and set its cache columns on the instance.

    Returns ``True`` when content was fetched and assigned, ``False`` when the
    fetch failed (the columns are then left untouched). The caller owns the
    session and is responsible for committing. ``now`` is injectable so tests
    stay deterministic.
    """
    result = fetch_article(story.url, timeout=timeout)
    if result is None:
        return False
    html, text = result
    story.cached_html = html
    story.cached_text = text
    story.cache_timestamp = now or datetime.now(timezone.utc)
    return True


def prune_cache(session, older_than: datetime) -> int:
    """Clear cached content for stories cached before ``older_than``.

    Returns the number of stories pruned and commits the change. This bounds
    database growth: cached HTML can be large, so old snapshots are dropped
    while the story row (title, url, scores) is kept intact.
    """
    stale = (
        session.query(models.Story)
        .filter(models.Story.cache_timestamp.isnot(None))
        .filter(models.Story.cache_timestamp < older_than)
        .all()
    )
    for story in stale:
        story.cached_html = None
        story.cached_text = None
        story.cache_timestamp = None
    session.commit()
    return len(stale)


def cache_db_stories(engine, timeout: int = DEFAULT_TIMEOUT) -> int:
    """Fetch and store cached content for stories that have none yet.

    Walks every story whose ``cache_timestamp`` is NULL, fetching and archiving
    its page. Returns the number of stories successfully cached. Per-story fetch
    failures are skipped (left uncached) rather than raised, so one bad URL
    never aborts the batch.
    """
    session = db.get_session(engine)
    cached = 0
    try:
        stories = (
            session.query(models.Story)
            .filter(models.Story.cache_timestamp.is_(None))
            .all()
        )
        for story in stories:
            if cache_story_content(story, timeout=timeout):
                cached += 1
        session.commit()
    finally:
        session.close()
    return cached
