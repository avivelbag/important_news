from datetime import datetime, timezone
from html.parser import HTMLParser

import requests

from .models import Story

DEFAULT_TIMEOUT = 10
USER_AGENT = "important-news-scraper/1.0 (+https://github.com/)"

# Tags whose textual content is markup/noise, never article prose. Their inner
# text is dropped entirely so the cached plaintext stays readable.
_SKIP_TAGS = {"script", "style", "head", "noscript", "template", "svg"}


class _TextExtractor(HTMLParser):
    """Collect human-readable text from an HTML document.

    Text inside any tag listed in ``_SKIP_TAGS`` (and its descendants) is
    discarded; everything else is accumulated as stripped, newline-joined
    chunks. A depth counter is used so nested skip tags are balanced correctly.
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
    ``None`` so callers can skip caching without special-casing each error type.
    """
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
    except Exception:
        return None
    html = resp.text
    return html, extract_text(html)


def cache_story_content(
    session, story: Story, timeout: int = DEFAULT_TIMEOUT, now: datetime | None = None
) -> bool:
    """Fetch a story's source page and persist its HTML/plaintext.

    Returns ``True`` when content was cached, ``False`` when the fetch failed.
    A failed fetch leaves the story's cache columns untouched so a single dead
    URL never aborts a bulk sync. ``now`` is injectable for deterministic tests.
    """
    result = fetch_article(story.url, timeout=timeout)
    if result is None:
        return False
    html, text = result
    story.cached_html = html
    story.cached_text = text
    story.cache_timestamp = now or datetime.now(timezone.utc)
    session.commit()
    return True


def prune_cache(session, older_than: datetime) -> int:
    """Clear cached content for stories cached before ``older_than``.

    Returns the number of stories pruned. This bounds database growth: cached
    HTML can be large, so old entries are dropped while the story row (title,
    url, scores) is kept intact.
    """
    stale = (
        session.query(Story)
        .filter(Story.cache_timestamp.isnot(None))
        .filter(Story.cache_timestamp < older_than)
        .all()
    )
    for story in stale:
        story.cached_html = None
        story.cached_text = None
        story.cache_timestamp = None
    session.commit()
    return len(stale)
