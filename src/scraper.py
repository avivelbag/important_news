"""Data-source connectors and scraper for the news database.

This module fetches AI and aerospace content from several public sources,
normalizes the heterogeneous payloads into the ``Article`` schema and inserts
them into the database while skipping URLs that already exist.

Network access is fully isolated behind an injectable ``fetch`` callable so the
parsing, normalization, categorization and deduplication logic can be tested
deterministically without touching the network. Only :func:`_http_get` performs
real I/O, and it is used purely as the default ``fetch`` implementation.

Run locally with::

    python src/scraper.py

which initializes the database (``NEWS_DB_PATH`` or ``news-data.db``), scrapes
every configured source and logs progress and errors to stdout.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Callable, Iterable

from sqlalchemy.engine import Engine

from .db import init_db, make_engine, session_scope
from .models import Article, Source

logger = logging.getLogger("scraper")

FetchFn = Callable[[str], str]

USER_AGENT = "important-news-scraper/1.0 (+https://example.com)"

HN_TOPSTORIES_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{id}.json"

AI_KEYWORDS = (
    "ai",
    "artificial intelligence",
    "machine learning",
    "deep learning",
    "neural",
    "llm",
    "gpt",
    "openai",
    "anthropic",
    "transformer",
    "model",
)

AEROSPACE_KEYWORDS = (
    "aerospace",
    "space",
    "rocket",
    "satellite",
    "nasa",
    "spacex",
    "orbit",
    "launch",
    "aircraft",
    "aviation",
    "spacecraft",
    "mars",
)


@dataclass(frozen=True)
class SourceSpec:
    """Static description of a public source to scrape.

    Attributes:
        name: Human-readable unique name, also used as the DB ``Source`` name.
        url: Endpoint to fetch (an RSS feed URL or an API endpoint).
        kind: Connector type; either ``"rss"`` or ``"hn"``.
        category: Default category for items lacking obvious keywords.
        limit: Maximum number of items to pull from this source per run.
    """

    name: str
    url: str
    kind: str
    category: str
    limit: int = 25


DEFAULT_SOURCES: tuple[SourceSpec, ...] = (
    SourceSpec(
        name="Hacker News",
        url=HN_TOPSTORIES_URL,
        kind="hn",
        category="ai",
        limit=25,
    ),
    SourceSpec(
        name="NASA Breaking News",
        url="https://www.nasa.gov/feed/",
        kind="rss",
        category="aerospace",
        limit=25,
    ),
    SourceSpec(
        name="MIT Technology Review AI",
        url="https://www.technologyreview.com/topic/artificial-intelligence/feed",
        kind="rss",
        category="ai",
        limit=25,
    ),
)


@dataclass
class NormalizedItem:
    """A source-agnostic representation of a single fetched entry.

    This is the intermediate shape produced by every connector before it is
    persisted as an :class:`~src.models.Article`.
    """

    title: str
    url: str
    summary: str | None = None
    category: str | None = None
    published_at: _dt.datetime | None = None


@dataclass
class ScrapeResult:
    """Aggregate counters describing the outcome of a scrape run."""

    inserted: int = 0
    skipped: int = 0
    errors: int = 0
    per_source: dict[str, int] = field(default_factory=dict)


def _http_get(url: str, timeout: float = 15.0) -> str:
    """Perform a blocking HTTP GET and return the decoded body.

    This is the only function that touches the network; tests inject their own
    fetch callable instead of calling this. A descriptive User-Agent is sent
    because several public feeds reject the default urllib agent.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def categorize(text: str, default: str | None = None) -> str | None:
    """Tag free text as ``"ai"`` or ``"aerospace"`` via keyword matching.

    The category with the most keyword hits wins. Ties and zero-hit inputs fall
    back to ``default`` so a source's intrinsic topic is preserved when the text
    itself is ambiguous.
    """
    lowered = text.lower()
    ai_hits = sum(1 for kw in AI_KEYWORDS if kw in lowered)
    aero_hits = sum(1 for kw in AEROSPACE_KEYWORDS if kw in lowered)
    if ai_hits == 0 and aero_hits == 0:
        return default
    if ai_hits == aero_hits:
        return default
    return "ai" if ai_hits > aero_hits else "aerospace"


def _strip(text: str | None) -> str | None:
    """Return ``text`` trimmed of surrounding whitespace, or ``None`` if empty."""
    if text is None:
        return None
    cleaned = text.strip()
    return cleaned or None


def _parse_date(value: str | None) -> _dt.datetime | None:
    """Parse an RSS RFC-822 date string into a naive UTC ``datetime``.

    Returns ``None`` for missing or unparseable values rather than raising, so
    a single malformed entry never aborts a whole feed.
    """
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return parsed


def parse_rss(xml_text: str, default_category: str | None = None) -> list[NormalizedItem]:
    """Parse an RSS/Atom XML document into normalized items.

    Supports both RSS ``<item>`` and Atom ``<entry>`` elements. Entries lacking
    a title or link are skipped. Malformed XML raises ``ET.ParseError`` to the
    caller, which treats it as a per-source error.
    """
    root = ET.fromstring(xml_text)
    items: list[NormalizedItem] = []

    def atom(tag: str) -> str:
        return f"{{http://www.w3.org/2005/Atom}}{tag}"

    nodes = list(root.iter("item")) + list(root.iter(atom("entry")))
    for node in nodes:
        title = node.findtext("title") or node.findtext(atom("title"))
        link = node.findtext("link")
        if not link:
            link_el = node.find(atom("link"))
            if link_el is not None:
                link = link_el.get("href") or link_el.text
        title = _strip(title)
        link = _strip(link)
        if not title or not link:
            continue
        summary = _strip(
            node.findtext("description")
            or node.findtext(atom("summary"))
            or node.findtext(atom("content"))
        )
        published = _parse_date(
            node.findtext("pubDate") or node.findtext(atom("updated"))
        )
        category = categorize(f"{title} {summary or ''}", default=default_category)
        items.append(
            NormalizedItem(
                title=title,
                url=link,
                summary=summary,
                category=category,
                published_at=published,
            )
        )
    return items


def fetch_rss(spec: SourceSpec, fetch: FetchFn) -> list[NormalizedItem]:
    """Fetch and parse an RSS source, truncated to ``spec.limit`` items."""
    body = fetch(spec.url)
    items = parse_rss(body, default_category=spec.category)
    return items[: spec.limit]


def fetch_hackernews(spec: SourceSpec, fetch: FetchFn) -> list[NormalizedItem]:
    """Fetch top Hacker News stories via the public Firebase API.

    The story-id list is fetched once, then each item is fetched individually up
    to ``spec.limit``. Items without a URL (Ask HN/text posts) or that fail to
    parse are skipped so the run continues. Categorization is applied to the
    title, defaulting to the source category.
    """
    id_payload = fetch(spec.url)
    ids = json.loads(id_payload)
    if not isinstance(ids, list):
        return []
    items: list[NormalizedItem] = []
    for story_id in ids[: spec.limit]:
        try:
            raw = fetch(HN_ITEM_URL.format(id=story_id))
            story = json.loads(raw)
        except (urllib.error.URLError, json.JSONDecodeError, ValueError):
            logger.warning("HN item %s failed to fetch/parse", story_id)
            continue
        if not isinstance(story, dict):
            continue
        url = _strip(story.get("url"))
        title = _strip(story.get("title"))
        if not url or not title:
            continue
        published = None
        ts = story.get("time")
        if isinstance(ts, (int, float)):
            published = _dt.datetime.utcfromtimestamp(ts)
        items.append(
            NormalizedItem(
                title=title,
                url=url,
                category=categorize(title, default=spec.category),
                published_at=published,
            )
        )
    return items


CONNECTORS: dict[str, Callable[[SourceSpec, FetchFn], list[NormalizedItem]]] = {
    "rss": fetch_rss,
    "hn": fetch_hackernews,
}


def ensure_source(session, spec: SourceSpec) -> Source:
    """Return the existing :class:`Source` row for ``spec`` or create it.

    Sources are keyed by their unique ``name`` so repeated runs reuse the same
    row instead of failing the unique constraint.
    """
    existing = session.query(Source).filter_by(name=spec.name).one_or_none()
    if existing is not None:
        return existing
    source = Source(name=spec.name, url=spec.url, kind=spec.kind)
    session.add(source)
    session.flush()
    return source


def insert_items(
    session, source: Source, items: Iterable[NormalizedItem]
) -> tuple[int, int]:
    """Insert normalized items for ``source``, skipping duplicate URLs.

    Deduplication checks both the URLs already stored in the database and the
    URLs seen earlier within this same batch, so a feed that repeats a link does
    not create duplicates. Returns ``(inserted, skipped)`` counts.
    """
    existing_urls = {
        row[0] for row in session.query(Article.url).all()
    }
    inserted = 0
    skipped = 0
    seen: set[str] = set()
    for item in items:
        if item.url in existing_urls or item.url in seen:
            skipped += 1
            continue
        seen.add(item.url)
        session.add(
            Article(
                title=item.title,
                url=item.url,
                summary=item.summary,
                category=item.category,
                source=source,
                published_at=item.published_at,
            )
        )
        inserted += 1
    return inserted, skipped


def scrape_source(engine: Engine, spec: SourceSpec, fetch: FetchFn) -> int:
    """Scrape a single source and persist new articles in one transaction.

    Returns the number of newly inserted articles. Exceptions from the connector
    propagate to the caller, which records them as per-source errors.
    """
    connector = CONNECTORS.get(spec.kind)
    if connector is None:
        raise ValueError(f"unknown source kind: {spec.kind!r}")
    items = connector(spec, fetch)
    with session_scope(engine) as session:
        source = ensure_source(session, spec)
        inserted, skipped = insert_items(session, source, items)
    logger.info(
        "%s: %d fetched, %d inserted, %d skipped",
        spec.name,
        len(items),
        inserted,
        skipped,
    )
    return inserted


def run_scraper(
    engine: Engine,
    sources: Iterable[SourceSpec] = DEFAULT_SOURCES,
    fetch: FetchFn = _http_get,
) -> ScrapeResult:
    """Scrape every source and return aggregate counters.

    Each source is processed independently: a failure in one source is logged
    and counted but does not prevent the others from running, keeping the run
    idempotent and resilient.
    """
    result = ScrapeResult()
    for spec in sources:
        try:
            inserted = scrape_source(engine, spec, fetch)
        except Exception as exc:  # noqa: BLE001 - isolate per-source failures
            logger.error("source %s failed: %s", spec.name, exc)
            result.errors += 1
            result.per_source[spec.name] = 0
            continue
        result.inserted += inserted
        result.per_source[spec.name] = inserted
    return result


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: initialize the DB, scrape all sources, log a summary."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    engine = make_engine()
    init_db(engine)
    logger.info("starting scrape of %d sources", len(DEFAULT_SOURCES))
    result = run_scraper(engine)
    logger.info(
        "done: %d inserted, %d errors across %d sources",
        result.inserted,
        result.errors,
        len(DEFAULT_SOURCES),
    )
    return 1 if result.errors and result.inserted == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
