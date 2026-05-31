import dataclasses
import datetime as dt
import email.utils
import json
import logging
import sys
import typing
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

import src.article_cache as article_cache
import src.db as db
import src.deduplicator as deduplicator
import src.discussions as discussions
import src.models as models
import src.scorer as scorer
import src.source_health as source_health

logger = logging.getLogger("scraper")

FetchFn = typing.Callable[[str], str]

USER_AGENT = "important-news-scraper/1.0 (+https://example.com)"

HN_TOPSTORIES_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{id}.json"

ATOM_NS = "http://www.w3.org/2005/Atom"

AI_KEYWORDS = (
    "artificial intelligence",
    "machine learning",
    "deep learning",
    "neural",
    "llm",
    "gpt",
    "openai",
    "anthropic",
    "transformer",
    "chatbot",
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


@dataclasses.dataclass(frozen=True)
class SourceSpec:
    name: str
    url: str
    kind: str
    category: str
    limit: int = 25


DEFAULT_SOURCES = (
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


@dataclasses.dataclass
class NormalizedItem:
    title: str
    url: str
    category: str
    published_at: dt.datetime | None = None


@dataclasses.dataclass
class ScrapeResult:
    inserted: int = 0
    errors: int = 0
    per_source: dict = dataclasses.field(default_factory=dict)
    skipped_sources: list = dataclasses.field(default_factory=list)


def _http_get(url: str, timeout: float = 15.0) -> str:
    # Several public feeds reject the default urllib agent, so send a real one.
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def categorize(text: str, default: str | None = None) -> str | None:
    lowered = text.lower()
    ai_hits = any(kw in lowered for kw in AI_KEYWORDS)
    aero_hits = any(kw in lowered for kw in AEROSPACE_KEYWORDS)
    if ai_hits and aero_hits:
        return "both"
    if ai_hits:
        return "ai"
    if aero_hits:
        return "aerospace"
    return default


def _strip(text: str | None) -> str | None:
    if text is None:
        return None
    cleaned = text.strip()
    return cleaned or None


def _parse_date(value: str | None) -> dt.datetime | None:
    # Return None rather than raise so one malformed entry never aborts a feed.
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return parsed


def parse_rss(xml_text: str, default_category: str | None = None) -> list:
    root = ET.fromstring(xml_text)
    items: list = []

    def atom(tag: str) -> str:
        return f"{{{ATOM_NS}}}{tag}"

    nodes = list(root.iter("item")) + list(root.iter(atom("entry")))
    for node in nodes:
        title = _strip(node.findtext("title") or node.findtext(atom("title")))
        link = _strip(node.findtext("link"))
        if not link:
            link_el = node.find(atom("link"))
            if link_el is not None:
                link = _strip(link_el.get("href") or link_el.text)
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
                category=category or default_category or "both",
                published_at=published,
            )
        )
    return items


def fetch_rss(spec: SourceSpec, fetch: FetchFn) -> list:
    body = fetch(spec.url)
    items = parse_rss(body, default_category=spec.category)
    return items[: spec.limit]


def fetch_hackernews(spec: SourceSpec, fetch: FetchFn) -> list:
    id_payload = fetch(spec.url)
    ids = json.loads(id_payload)
    if not isinstance(ids, list):
        return []
    items: list = []
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
            published = dt.datetime.fromtimestamp(ts, dt.timezone.utc).replace(
                tzinfo=None
            )
        items.append(
            NormalizedItem(
                title=title,
                url=url,
                category=categorize(title, default=spec.category) or spec.category,
                published_at=published,
            )
        )
    return items


CONNECTORS = {
    "rss": fetch_rss,
    "hn": fetch_hackernews,
}


def ensure_source(session, spec: SourceSpec) -> models.Source:
    existing = session.query(models.Source).filter_by(name=spec.name).one_or_none()
    if existing is not None:
        return existing
    source = models.Source(name=spec.name, url=spec.url)
    session.add(source)
    session.flush()
    return source


def insert_items(
    session, source, items, now: dt.datetime, cache_content: bool = False
) -> tuple:
    # Dedupe against both stored URLs and URLs seen earlier in this same batch.
    existing_urls = {row[0] for row in session.query(models.Story.url).all()}
    inserted = 0
    skipped = 0
    seen: set = set()
    for item in items:
        if item.url in existing_urls or item.url in seen:
            skipped += 1
            continue
        seen.add(item.url)
        story = models.Story(
            title=item.title,
            url=item.url,
            source_name=source.name,
            topic=item.category,
            published_at=item.published_at or now,
            fetched_at=now,
            source=source,
        )
        # Archive the source page when requested; a failed fetch is swallowed
        # (story stays uncached) so one dead URL never aborts the batch.
        if cache_content:
            try:
                article_cache.cache_story_content(story, now=now)
            except Exception:
                pass
        session.add(story)
        inserted += 1
    return inserted, skipped


def scrape_source(
    engine,
    spec: SourceSpec,
    fetch: FetchFn,
    now: dt.datetime | None = None,
    failure_threshold: int = source_health.DEFAULT_FAILURE_THRESHOLD,
    cache_content: bool = False,
) -> int:
    connector = CONNECTORS.get(spec.kind)
    if connector is None:
        raise ValueError(f"unknown source kind: {spec.kind!r}")
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    session = db.get_session(engine)
    try:
        source = ensure_source(session, spec)
        # Record the fetch outcome (success or failure) against source health so
        # the dashboard and skip logic see every attempt, even ones that raise.
        try:
            items = connector(spec, fetch)
        except Exception as exc:
            source_health.record_fetch(
                session,
                source,
                "error",
                now,
                error_message=f"{type(exc).__name__}: {exc}",
                failure_threshold=failure_threshold,
            )
            session.commit()
            raise
        inserted, skipped = insert_items(
            session, source, items, now, cache_content=cache_content
        )
        source_health.record_fetch(
            session,
            source,
            "success",
            now,
            article_count=inserted,
            failure_threshold=failure_threshold,
        )
        session.commit()
    finally:
        session.close()
    logger.info(
        "%s: %d fetched, %d inserted, %d skipped",
        spec.name,
        len(items),
        inserted,
        skipped,
    )
    return inserted


def run_scraper(
    engine,
    sources=DEFAULT_SOURCES,
    fetch: FetchFn = _http_get,
    now: dt.datetime | None = None,
    skip_unhealthy: bool = False,
    failure_threshold: int = source_health.DEFAULT_FAILURE_THRESHOLD,
    search_fn=discussions.default_search_fn,
    cache_content: bool = False,
) -> ScrapeResult:
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    result = ScrapeResult()
    for spec in sources:
        if skip_unhealthy:
            # Avoid wasting time/quota re-fetching sources already marked broken.
            session = db.get_session(engine)
            try:
                broken = source_health.is_source_broken(
                    session, spec.name, failure_threshold
                )
            finally:
                session.close()
            if broken:
                logger.warning("skipping broken source %s", spec.name)
                result.skipped_sources.append(spec.name)
                result.per_source[spec.name] = 0
                continue
        try:
            inserted = scrape_source(
                engine, spec, fetch, now, failure_threshold, cache_content
            )
        except Exception as exc:  # isolate per-source failures from the run
            logger.error("source %s failed: %s", spec.name, exc)
            result.errors += 1
            result.per_source[spec.name] = 0
            continue
        result.inserted += inserted
        result.per_source[spec.name] = inserted
    # Merge near-duplicates before scoring so the canonical row carries the
    # aggregated engagement metrics the scorer reads. Runs on every scrape and,
    # transitively, on every refresh.
    deduplicator.deduplicate(engine, now=now)
    scorer.recompute_scores(engine, now=now)
    # Link external Reddit/GitHub/HN threads to the freshly scored stories. Runs
    # after dedup so only canonical stories are matched; search_fn is injectable
    # (None disables it) so tests stay offline.
    if search_fn is not None:
        session = db.get_session(engine)
        try:
            discussions.discover_discussions_for_stories(session, search_fn, now=now)
        finally:
            session.close()
    return result


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    engine = db.get_engine()
    db.init_db(engine)
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
