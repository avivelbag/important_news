"""RSS 2.0 feed generation for scraped stories.

The project ships as a static site (GitHub Pages), so rather than a live
endpoint the feed is rendered to a string and written next to the generated
HTML under ``docs/``. Category filtering is expressed as a function argument
and materialised as one ``feed-<category>.rss`` file per category, which gives
the same effect as a ``/feed.rss?category=ai`` query for a static host.

No third-party dependencies (``feedgen`` is not installed in this project); the
feed is built with :mod:`xml.etree.ElementTree` so escaping and well-formedness
are handled by the stdlib serializer. ``pubDate``/``lastBuildDate`` use
:func:`email.utils.format_datetime` to emit RFC 822 dates as RSS requires.
"""

from datetime import datetime, timezone
from email.utils import format_datetime
from xml.etree import ElementTree as ET

from src.models import Story

FEED_TITLE = "Important News"
FEED_LINK = "https://example.com/"
FEED_DESCRIPTION = "AI and aerospace headlines, ranked by relevance."

# Topics a reader can filter on. A story tagged "both" (AI & aerospace) appears
# under each, mirroring the site's filter.js semantics, so it is not listed here.
CATEGORY_FILTERS = ("ai", "aerospace")


def story_in_category(topic: str, category: str) -> bool:
    """Whether a story ``topic`` belongs in the ``category`` feed.

    ``"both"`` stories surface under both ``ai`` and ``aerospace`` so that
    cross-topic stories are never hidden from either category subscriber.
    """
    if topic == category:
        return True
    if topic == "both":
        return category in ("ai", "aerospace")
    return False


def _points(story: Story) -> int:
    """Display score for a story: live votes, else the scraped raw score."""
    return story.vote_count or story.raw_score or 0


def _as_utc(value: datetime) -> datetime:
    """Attach UTC to the naive datetimes stored in the DB for RFC 822 output."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def generate_rss(
    stories: list[Story],
    *,
    category: str | None = None,
    title: str = FEED_TITLE,
    link: str = FEED_LINK,
    description: str = FEED_DESCRIPTION,
    build_date: datetime | None = None,
) -> str:
    """Render ``stories`` as an RSS 2.0 feed document.

    Args:
        stories: Stories to include. Filtered by ``category`` when given and
            always emitted in descending publish order (newest first).
        category: When set (e.g. ``"ai"``), only stories whose topic matches
            (see :func:`_matches`) are included. ``None`` includes everything.
        title, link, description: RSS channel metadata.
        build_date: Value for ``<lastBuildDate>``. Defaults to the newest story
            publish date so the rendered feed is a deterministic function of its
            input (no wall-clock dependency).

    Returns:
        A UTF-8 XML string beginning with an ``<?xml ...?>`` declaration.
    """
    if category is not None:
        stories = [s for s in stories if story_in_category(s.topic, category)]
    stories = sorted(stories, key=lambda s: s.published_at, reverse=True)

    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = title
    ET.SubElement(channel, "link").text = link
    ET.SubElement(channel, "description").text = description

    if build_date is None and stories:
        build_date = max(s.published_at for s in stories)
    if build_date is not None:
        ET.SubElement(channel, "lastBuildDate").text = format_datetime(
            _as_utc(build_date)
        )

    for story in stories:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = story.title
        ET.SubElement(item, "link").text = story.url
        ET.SubElement(item, "description").text = (
            f"{_points(story)} points · {story.source_name}"
        )
        # Article URL doubles as a permalink GUID so readers dedupe on it.
        guid = ET.SubElement(item, "guid", isPermaLink="true")
        guid.text = story.url
        ET.SubElement(item, "category").text = story.topic
        # source attribution; url is required on <source>, point it at the story.
        source = ET.SubElement(item, "source", url=story.url)
        source.text = story.source_name
        ET.SubElement(item, "pubDate").text = format_datetime(
            _as_utc(story.published_at)
        )

    # No encoding attribute in the declaration: ElementTree.fromstring rejects a
    # unicode string that declares an encoding, and consumers parse the str
    # directly. The file is written as UTF-8, which is the XML default anyway.
    body = ET.tostring(rss, encoding="unicode")
    return '<?xml version="1.0"?>\n' + body
