import json
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import Engine, select

from src.credibility_scorer import credibility_badge, credibility_tier
from src.db import get_engine, get_session
from src.discussions import get_discussions
from src.models import Story
from src.rss_generator import CATEGORY_FILTERS, generate_rss, story_in_category

_DEFAULT_OUT_DIR = Path("docs")

_TOPIC_LABELS = {
    "ai": "AI",
    "aerospace": "Aerospace",
    "both": "AI & Aerospace",
}

# Order sections are rendered in; topics outside this list are appended after,
# sorted alphabetically, so an unexpected topic value never crashes the build.
_TOPIC_ORDER = ["ai", "aerospace", "both"]

# Category filter buttons rendered in the nav. Values match the data-filter
# attribute that filter.js reads; "all" is the default state.
_FILTERS = [("all", "All"), ("ai", "AI"), ("aerospace", "Aerospace")]


def fetch_stories(session) -> list[Story]:
    # Only canonical stories are rendered; duplicates (canonical_id set) are
    # folded into their canonical row so each story appears at most once.
    # Moderation-hidden stories (auto-hidden on flags or hidden by a moderator)
    # are withheld from the public site pending review.
    stmt = (
        select(Story)
        .where(Story.canonical_id.is_(None), Story.is_hidden.is_(False))
        .order_by(
            Story.computed_score.desc(),
            Story.published_at.desc(),
            Story.id.asc(),
        )
    )
    return list(session.scalars(stmt).all())


def _merged_sources(story: Story) -> list[str]:
    """Return the list of source names that contributed to *story*.

    Reads the JSON ``merged_sources`` column when present (a merged story),
    otherwise falls back to the story's single ``source_name``. Malformed JSON
    degrades gracefully to the single source rather than crashing the build.
    """
    if story.merged_sources:
        try:
            sources = json.loads(story.merged_sources)
        except (ValueError, TypeError):
            sources = None
        if isinstance(sources, list) and sources:
            return [str(s) for s in sources]
    return [story.source_name]


def group_by_topic(stories: list[Story]) -> dict[str, list[Story]]:
    grouped: dict[str, list[Story]] = {}
    for story in stories:
        grouped.setdefault(story.topic, []).append(story)

    ordered: dict[str, list[Story]] = {}
    for topic in _TOPIC_ORDER:
        if topic in grouped:
            ordered[topic] = grouped.pop(topic)
    for topic in sorted(grouped):
        ordered[topic] = grouped[topic]
    return ordered


def _profile_href(username: str) -> str:
    """Return the profile-page URL for *username*, query-string encoded.

    The static site has no server-side routing, so a profile lives at
    ``user.html?u=<username>`` (resolved client-side by ``user.js``) rather than
    a true ``/user/<username>`` path. The username is fully escaped for use in an
    HTML attribute.
    """
    from urllib.parse import quote

    return "user.html?u=" + escape(quote(username, safe=""), quote=True)


def _domain(url: str) -> str:
    netloc = urlparse(url).netloc
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def _format_timestamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M")


def render_discussion_links(discussions: list[dict]) -> str:
    """Render "Discuss on <platform>" links for a story's external threads.

    Returns an empty string when there are no discussions so an unlinked story
    renders exactly as before. Each link shows the platform, thread title, and
    a comment-count engagement indicator, grouping by platform in the ranked
    order :func:`src.discussions.get_discussions` returns.
    """
    if not discussions:
        return ""
    items = []
    for d in discussions:
        label = escape(str(d.get("platform_label") or d.get("platform", "")))
        count = d.get("comment_count") or 0
        count_label = "1 comment" if count == 1 else f"{count} comments"
        items.append(
            '          <li class="discussion">'
            f'<a class="discussion-link" href="{escape(str(d.get("url", "")), quote=True)}" '
            f'title="{escape(str(d.get("title", "")), quote=True)}">'
            f"Discuss on {label}</a> "
            f'<span class="discussion-count">{escape(count_label)}</span></li>'
        )
    return (
        '        <ul class="discussions-external">\n'
        + "\n".join(items)
        + "\n        </ul>"
    )


def _cached_block(story: Story) -> str:
    """Render the archived-content disclosure for a story, or "" when uncached.

    When ``cached_text`` is present the reader gets a "View cached version"
    toggle holding the archived plaintext plus a "View source" link to the live
    URL. Stories without cached content return an empty string, so older rows
    gracefully degrade to a metadata-only card with no broken toggle shown.
    """
    cached_text = getattr(story, "cached_text", None)
    if not cached_text:
        return ""
    body = escape(cached_text)
    url_safe = escape(story.url, quote=True)
    return (
        '\n        <details class="cached">\n'
        "          <summary>View cached version</summary>\n"
        f'          <div class="cached-content">{body}</div>\n'
        f'          <a class="cached-source" href="{url_safe}">View source</a>\n'
        "        </details>"
    )


def render_story(story: Story, index: int, discussions: list[dict] | None = None) -> str:
    domain = _domain(story.url)
    domain_html = (
        f' <span class="domain">({escape(domain)})</span>' if domain else ""
    )
    points = story.vote_count or story.raw_score or 0
    downvotes = story.downvotes or 0
    downvotes_html = (
        f' &middot; <span class="downvotes">{downvotes} downvotes</span>'
        if downvotes
        else ""
    )
    # A merged story lists every contributing source ("via HN, Reddit"); an
    # unmerged one just shows its single source.
    sources = _merged_sources(story)
    if len(sources) > 1:
        sources_html = "via " + ", ".join(escape(s) for s in sources)
    else:
        sources_html = escape(sources[0])
    cred = story.credibility_score if story.credibility_score is not None else 50.0
    cred_tier = credibility_tier(cred)
    cred_badge_html = (
        f' <span class="cred-badge cred-{cred_tier}">'
        f"{escape(credibility_badge(cred))}</span>"
    )
    story_attr = escape(str(story.id), quote=True) if story.id is not None else ""
    comment_count = story.comment_count or 0
    comments_label = "1 comment" if comment_count == 1 else f"{comment_count} comments"
    bookmark_count = story.bookmark_count or 0
    # Link the submitter's name to their profile page so user profiles are
    # reachable from author names throughout the site.
    submitter_html = ""
    if story.submitted_by:
        submitter_html = (
            f' &middot; by <a class="author" href="{_profile_href(story.submitted_by)}">'
            f"{escape(story.submitted_by)}</a>"
        )
    return (
        f'    <li class="story" data-story-id="{story_attr}">\n'
        f'      <span class="rank">{index}.</span>\n'
        '      <span class="votes">\n'
        '        <button type="button" class="vote up" '
        'aria-label="Upvote">&#9650;</button>\n'
        '        <button type="button" class="vote down" '
        'aria-label="Downvote">&#9660;</button>\n'
        "      </span>\n"
        '      <span class="story-main">\n'
        f'        <a class="title" href="{escape(story.url, quote=True)}">'
        f"{escape(story.title)}</a>{domain_html}{cred_badge_html}\n"
        '        <span class="meta">'
        f'<span class="points">{points} points</span>{downvotes_html} '
        f"&middot; {sources_html}{submitter_html} "
        f"&middot; {escape(_format_timestamp(story.published_at))} "
        '&middot; <button type="button" class="comments-toggle">'
        f'<span class="comment-count">{comments_label}</span></button> '
        '&middot; <button type="button" class="bookmark-toggle" '
        'aria-label="Save for later" aria-pressed="false">&#9734; '
        f'<span class="bookmark-count">{bookmark_count}</span></button></span>'
        f"{_cached_block(story)}\n"
        '        <div class="comments" hidden></div>\n'
        f"{render_discussion_links(discussions or [])}\n"
        "      </span>\n"
        "    </li>"
    )


def render_section(
    topic: str,
    stories: list[Story],
    discussions_map: dict[int, list[dict]] | None = None,
) -> str:
    label = _TOPIC_LABELS.get(topic, topic.title())
    discussions_map = discussions_map or {}
    rows = "\n".join(
        render_story(story, i, discussions_map.get(story.id))
        for i, story in enumerate(stories, start=1)
    )
    topic_attr = escape(topic, quote=True)
    return (
        f'  <section class="topic" id="topic-{topic_attr}" '
        f'data-topic="{topic_attr}">\n'
        f"    <h2>{escape(label)}</h2>\n"
        f'    <ol class="stories">\n{rows}\n    </ol>\n'
        "  </section>"
    )


def render_html(
    grouped: dict[str, list[Story]],
    discussions_map: dict[int, list[dict]] | None = None,
) -> str:
    if grouped:
        body = "\n".join(
            render_section(topic, stories, discussions_map)
            for topic, stories in grouped.items()
        )
    else:
        body = '  <p class="empty">No stories yet.</p>'

    nav = "\n".join(
        f'      <button type="button" class="filter" data-filter="{value}">'
        f"{escape(label)}</button>"
        for value, label in _FILTERS
    )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "  <title>Important News</title>\n"
        '  <link rel="stylesheet" href="style.css">\n'
        '  <link rel="alternate" type="application/rss+xml" '
        'href="feed.rss" title="Important News">\n'
        "</head>\n"
        "<body>\n"
        "  <header>\n"
        '    <h1>Important News</h1>\n'
        '    <nav class="site-nav">\n'
        '      <a href="index.html">Home</a>\n'
        '      <a href="bookmarks.html">Saved</a>\n'
        '      <a href="leaderboard.html">Leaderboard</a>\n'
        "    </nav>\n"
        '    <form class="search" role="search" autocomplete="off">\n'
        '      <input type="search" id="search-box" name="q" '
        'placeholder="Search stories…" minlength="2" maxlength="100" '
        'aria-label="Search stories">\n'
        '      <div class="search-filters" id="search-filters">\n'
        '        <label>Sources <input type="text" id="filter-sources" '
        'name="sources" placeholder="e.g. Hacker News, Reddit" '
        'aria-label="Filter by sources (comma-separated)"></label>\n'
        '        <label>Topics <input type="text" id="filter-topics" '
        'name="topics" placeholder="e.g. ai, aerospace" '
        'aria-label="Filter by topics (comma-separated)"></label>\n'
        '        <label>Min score <input type="number" id="filter-min-score" '
        'name="min_score" min="0" step="1" aria-label="Minimum score"></label>\n'
        '        <label>Min comments <input type="number" id="filter-min-comments" '
        'name="min_comments" min="0" step="1" aria-label="Minimum comments"></label>\n'
        '        <label>From <input type="date" id="filter-date-from" '
        'name="date_from" aria-label="From date"></label>\n'
        '        <label>To <input type="date" id="filter-date-to" '
        'name="date_to" aria-label="To date"></label>\n'
        '        <label>Sort <select id="filter-sort" name="sort" '
        'aria-label="Sort results">\n'
        '          <option value="relevance">Relevance</option>\n'
        '          <option value="recent">Most recent</option>\n'
        '          <option value="score">Highest score</option>\n'
        "        </select></label>\n"
        '        <button type="button" id="filter-clear">Clear filters</button>\n'
        "      </div>\n"
        '      <div id="search-results" class="search-results" hidden></div>\n'
        "    </form>\n"
        f'    <nav class="filters">\n{nav}\n    </nav>\n'
        "  </header>\n"
        '  <main>\n'
        '    <section id="personalized-feed" class="personalized" hidden>\n'
        '      <div class="feed-controls">\n'
        '        <span class="feed-label">Your feed</span>\n'
        '        <div class="algo-switch">\n'
        '          <button type="button" class="algo" data-algo="balanced">'
        "Balanced</button>\n"
        '          <button type="button" class="algo" data-algo="trending">'
        "Trending</button>\n"
        '          <button type="button" class="algo" data-algo="recent">'
        "Recent</button>\n"
        '          <button type="button" class="algo" data-algo="followed">'
        "Followed</button>\n"
        "        </div>\n"
        '        <button type="button" id="toggle-feed" class="toggle-feed">'
        "View all stories</button>\n"
        "      </div>\n"
        '      <ol id="personalized-list" class="stories"></ol>\n'
        '      <p id="personalized-empty" class="empty" hidden>'
        "No personalized stories yet — vote and follow topics to train your "
        "feed.</p>\n"
        "    </section>\n"
        f'    <div id="global-feed">\n{body}\n    </div>\n'
        "  </main>\n"
        '  <footer>Generated static site &middot; AI &amp; Aerospace</footer>\n'
        '  <script src="filter.js"></script>\n'
        '  <script src="search.js"></script>\n'
        '  <script src="vote.js"></script>\n'
        '  <script src="comments.js"></script>\n'
        '  <script src="bookmark.js"></script>\n'
        '  <script src="feed.js"></script>\n'
        "</body>\n"
        "</html>\n"
    )


def render_css() -> str:
    return (
        ":root { --accent: #ff6600; --muted: #828282; --bg: #f6f6ef; }\n"
        "* { box-sizing: border-box; }\n"
        "body {\n"
        "  margin: 0;\n"
        "  font-family: Verdana, Geneva, -apple-system, system-ui, sans-serif;\n"
        "  font-size: 14px;\n"
        "  line-height: 1.5;\n"
        "  color: #222;\n"
        "  background: var(--bg);\n"
        "}\n"
        "header {\n"
        "  background: var(--accent);\n"
        "  padding: 0.5rem 1rem;\n"
        "}\n"
        "header h1 { margin: 0; font-size: 1.1rem; color: #fff; }\n"
        "nav.filters { margin-top: 0.35rem; display: flex; flex-wrap: wrap; gap: 0.4rem; }\n"
        "button.filter {\n"
        "  border: 1px solid rgba(255, 255, 255, 0.6);\n"
        "  background: transparent;\n"
        "  color: #fff;\n"
        "  font: inherit;\n"
        "  font-size: 0.8rem;\n"
        "  padding: 0.15rem 0.6rem;\n"
        "  border-radius: 3px;\n"
        "  cursor: pointer;\n"
        "}\n"
        "button.filter:hover { background: rgba(255, 255, 255, 0.18); }\n"
        "button.filter.active { background: #fff; color: var(--accent); font-weight: bold; }\n"
        "form.search { position: relative; margin-top: 0.4rem; }\n"
        "#search-box {\n"
        "  width: 100%;\n"
        "  max-width: 320px;\n"
        "  padding: 0.25rem 0.5rem;\n"
        "  font: inherit;\n"
        "  font-size: 0.85rem;\n"
        "  border: 1px solid rgba(255, 255, 255, 0.6);\n"
        "  border-radius: 3px;\n"
        "}\n"
        ".search-results {\n"
        "  position: absolute;\n"
        "  z-index: 10;\n"
        "  background: #fff;\n"
        "  border: 1px solid #ddd;\n"
        "  border-radius: 3px;\n"
        "  max-width: 320px;\n"
        "  width: 100%;\n"
        "  max-height: 60vh;\n"
        "  overflow-y: auto;\n"
        "}\n"
        ".search-result { padding: 0.3rem 0.5rem; border-bottom: 1px solid #eee; }\n"
        ".search-result a { color: #222; text-decoration: none; font-size: 0.85rem; }\n"
        ".search-result a:hover { text-decoration: underline; }\n"
        ".search-empty { padding: 0.4rem 0.5rem; color: var(--muted); font-style: italic; }\n"
        "main { max-width: 760px; margin: 0 auto; padding: 1rem; }\n"
        "h2 { font-size: 1rem; border-bottom: 1px solid #ddd; padding-bottom: 0.25rem; }\n"
        "ol.stories { list-style: none; padding: 0; margin: 0 0 2rem; }\n"
        "li.story {\n"
        "  display: flex;\n"
        "  gap: 0.5rem;\n"
        "  padding: 0.4rem 0.25rem;\n"
        "  border-radius: 3px;\n"
        "}\n"
        "li.story:hover { background: #fff; }\n"
        ".rank { color: var(--muted); min-width: 1.5rem; text-align: right; }\n"
        ".votes { display: flex; flex-direction: column; gap: 0.1rem; }\n"
        "button.vote {\n"
        "  border: none;\n"
        "  background: transparent;\n"
        "  color: var(--muted);\n"
        "  font: inherit;\n"
        "  font-size: 0.7rem;\n"
        "  line-height: 1;\n"
        "  padding: 0;\n"
        "  cursor: pointer;\n"
        "}\n"
        "button.vote:hover { color: var(--accent); }\n"
        "button.vote.voted { color: var(--accent); font-weight: bold; }\n"
        ".story-main { display: flex; flex-direction: column; }\n"
        "a.title { color: #222; text-decoration: none; font-size: 0.95rem; }\n"
        "a.title:hover { text-decoration: underline; }\n"
        ".domain { color: var(--muted); font-size: 0.8rem; }\n"
        ".cred-badge { font-size: 0.7rem; padding: 0 0.35rem; border-radius: "
        "0.6rem; margin-left: 0.3rem; vertical-align: middle; }\n"
        ".cred-verified { background: #1f7a3d; color: #fff; }\n"
        ".cred-community { background: #b9770e; color: #fff; }\n"
        ".cred-unverified { background: #555; color: #fff; }\n"
        ".meta { color: var(--muted); font-size: 0.78rem; }\n"
        "button.comments-toggle {\n"
        "  border: none;\n"
        "  background: transparent;\n"
        "  color: var(--muted);\n"
        "  font: inherit;\n"
        "  font-size: inherit;\n"
        "  padding: 0;\n"
        "  cursor: pointer;\n"
        "  text-decoration: underline;\n"
        "}\n"
        ".comments { margin-top: 0.4rem; }\n"
        ".comment { margin: 0.3rem 0; }\n"
        ".comment-replies { margin-left: 1rem; border-left: 1px solid #ddd; padding-left: 0.6rem; }\n"
        ".comment-meta { color: var(--muted); font-size: 0.72rem; }\n"
        ".comment-body { font-size: 0.82rem; white-space: pre-wrap; }\n"
        ".comment.collapsed > .comment-content { display: none; }\n"
        ".comment-votes { display: inline-flex; align-items: center; gap: 0.2rem; }\n"
        "button.cvote {\n"
        "  border: none;\n"
        "  background: transparent;\n"
        "  color: var(--muted);\n"
        "  font: inherit;\n"
        "  line-height: 1;\n"
        "  padding: 0;\n"
        "  cursor: pointer;\n"
        "}\n"
        "button.cvote:hover { color: var(--accent); }\n"
        "button.cvote.voted { color: var(--accent); font-weight: bold; }\n"
        ".comment-score { font-weight: bold; color: #444; }\n"
        ".comment-op {\n"
        "  color: var(--accent);\n"
        "  font-weight: bold;\n"
        "  border: 1px solid var(--accent);\n"
        "  border-radius: 3px;\n"
        "  padding: 0 0.2rem;\n"
        "  font-size: 0.68rem;\n"
        "}\n"
        "button.comment-collapse-toggle {\n"
        "  border: none;\n"
        "  background: transparent;\n"
        "  color: var(--muted);\n"
        "  font: inherit;\n"
        "  cursor: pointer;\n"
        "  padding: 0;\n"
        "}\n"
        ".comment-sort { font-size: 0.75rem; color: var(--muted); margin-bottom: 0.3rem; }\n"
        ".comment-form textarea { width: 100%; max-width: 480px; font: inherit; }\n"
        ".comments-empty { color: var(--muted); font-style: italic; }\n"
        "ul.discussions-external { list-style: none; padding: 0; margin: 0.3rem 0 0; "
        "display: flex; flex-wrap: wrap; gap: 0.5rem; }\n"
        "li.discussion { font-size: 0.76rem; }\n"
        "a.discussion-link { color: var(--accent); text-decoration: none; font-weight: bold; }\n"
        "a.discussion-link:hover { text-decoration: underline; }\n"
        ".discussion-count { color: var(--muted); }\n"
        ".empty { color: var(--muted); font-style: italic; }\n"
        ".muted { color: var(--muted); }\n"
        ".personalized { margin-bottom: 1.2rem; }\n"
        ".feed-controls { display: flex; align-items: center; gap: 0.5rem; "
        "flex-wrap: wrap; margin-bottom: 0.6rem; }\n"
        ".feed-label { font-weight: bold; }\n"
        ".algo-switch { display: flex; gap: 0.3rem; }\n"
        "button.algo, button.toggle-feed { border: 1px solid var(--accent); "
        "background: #fff; color: var(--accent); border-radius: 4px; "
        "padding: 0.2rem 0.6rem; cursor: pointer; font-size: 0.8rem; }\n"
        "button.algo.active { background: var(--accent); color: #fff; }\n"
        "button.toggle-feed { margin-left: auto; }\n"
        "nav.site-nav { margin-top: 0.35rem; display: flex; gap: 0.8rem; }\n"
        "nav.site-nav a { color: #fff; font-size: 0.8rem; text-decoration: none; }\n"
        "nav.site-nav a:hover { text-decoration: underline; }\n"
        "a.author { color: var(--accent); text-decoration: none; }\n"
        "a.author:hover { text-decoration: underline; }\n"
        ".profile-header { border-bottom: 1px solid #ddd; padding-bottom: 0.5rem; }\n"
        ".profile-karma { font-size: 1.4rem; font-weight: bold; color: var(--accent); }\n"
        ".profile-stats { color: var(--muted); font-size: 0.85rem; }\n"
        ".profile-bio { margin: 0.5rem 0; }\n"
        ".profile-tabs { display: flex; gap: 0.5rem; margin: 0.8rem 0 0.4rem; }\n"
        "button.tab {\n"
        "  border: 1px solid #ddd;\n"
        "  background: #fff;\n"
        "  font: inherit;\n"
        "  font-size: 0.82rem;\n"
        "  padding: 0.2rem 0.7rem;\n"
        "  border-radius: 3px;\n"
        "  cursor: pointer;\n"
        "}\n"
        "button.tab.active { background: var(--accent); color: #fff; border-color: var(--accent); }\n"
        ".activity-item { padding: 0.3rem 0; border-bottom: 1px solid #eee; font-size: 0.85rem; }\n"
        ".activity-kind { color: var(--muted); font-size: 0.75rem; }\n"
        ".pager { display: flex; gap: 0.6rem; align-items: center; margin-top: 0.6rem; }\n"
        "button.page-btn { font: inherit; font-size: 0.8rem; cursor: pointer; }\n"
        "button.page-btn[disabled] { color: var(--muted); cursor: default; }\n"
        "ol.leaderboard { list-style: none; padding: 0; margin: 0; }\n"
        "ol.leaderboard li { padding: 0.35rem 0; border-bottom: 1px solid #eee; display: flex; gap: 0.6rem; }\n"
        ".lb-rank { color: var(--muted); min-width: 1.6rem; text-align: right; }\n"
        ".lb-karma { margin-left: auto; color: var(--accent); font-weight: bold; }\n"
        ".private-note { color: var(--muted); font-style: italic; }\n"
        "footer {\n"
        "  max-width: 760px;\n"
        "  margin: 0 auto;\n"
        "  padding: 1rem;\n"
        "  color: var(--muted);\n"
        "  font-size: 0.8rem;\n"
        "}\n"
        "@media (max-width: 480px) {\n"
        "  body { font-size: 13px; }\n"
        "  main, footer { padding: 0.75rem; }\n"
        "  .rank { min-width: 1.2rem; }\n"
        "}\n"
    )


def render_js() -> str:
    # A section's data-topic of "both" (AI & Aerospace) is shown under both the
    # "ai" and "aerospace" filters; unknown topics only appear under "all".
    return (
        "(function () {\n"
        '  var STORAGE_KEY = "category-filter";\n'
        '  var VALID = ["all", "ai", "aerospace"];\n'
        "\n"
        "  function readState() {\n"
        '    var hash = (window.location.hash || "").replace(/^#/, "");\n'
        "    if (VALID.indexOf(hash) !== -1) return hash;\n"
        "    try {\n"
        "      var stored = window.localStorage.getItem(STORAGE_KEY);\n"
        "      if (VALID.indexOf(stored) !== -1) return stored;\n"
        "    } catch (e) {}\n"
        '    return "all";\n'
        "  }\n"
        "\n"
        "  function matches(topic, filter) {\n"
        '    if (filter === "all") return true;\n'
        "    if (topic === filter) return true;\n"
        '    if (topic === "both") return filter === "ai" || filter === "aerospace";\n'
        "    return false;\n"
        "  }\n"
        "\n"
        "  function apply(filter) {\n"
        '    var sections = document.querySelectorAll("section.topic");\n'
        "    for (var i = 0; i < sections.length; i++) {\n"
        '      var topic = sections[i].getAttribute("data-topic");\n'
        "      sections[i].hidden = !matches(topic, filter);\n"
        "    }\n"
        '    var buttons = document.querySelectorAll("button.filter");\n'
        "    for (var j = 0; j < buttons.length; j++) {\n"
        '      var f = buttons[j].getAttribute("data-filter");\n'
        '      buttons[j].classList.toggle("active", f === filter);\n'
        "    }\n"
        "  }\n"
        "\n"
        "  function setState(filter) {\n"
        "    try {\n"
        "      window.localStorage.setItem(STORAGE_KEY, filter);\n"
        "    } catch (e) {}\n"
        '    if (window.location.hash.replace(/^#/, "") !== filter) {\n'
        '      window.location.hash = filter;\n'
        "    }\n"
        "    apply(filter);\n"
        "  }\n"
        "\n"
        "  function init() {\n"
        '    var buttons = document.querySelectorAll("button.filter");\n'
        "    for (var i = 0; i < buttons.length; i++) {\n"
        '      buttons[i].addEventListener("click", function () {\n'
        '        setState(this.getAttribute("data-filter"));\n'
        "      });\n"
        "    }\n"
        '    window.addEventListener("hashchange", function () {\n'
        "      apply(readState());\n"
        "    });\n"
        "    apply(readState());\n"
        "  }\n"
        "\n"
        '  if (document.readyState === "loading") {\n'
        '    document.addEventListener("DOMContentLoaded", init);\n'
        "  } else {\n"
        "    init();\n"
        "  }\n"
        "})();\n"
    )


def render_search_js() -> str:
    # Debounces keystrokes (300ms) before hitting /api/search so the server is
    # not pinged on every character; Enter submits immediately. Queries shorter
    # than 2 chars are treated as empty (the API rejects them anyway).
    return (
        "(function () {\n"
        '  var DEBOUNCE_MS = 300;\n'
        '  var box = document.getElementById("search-box");\n'
        '  var panel = document.getElementById("search-results");\n'
        "  if (!box || !panel) return;\n"
        "  var timer = null;\n"
        "\n"
        "  function activeCategory() {\n"
        '    var active = document.querySelector("button.filter.active");\n'
        '    var f = active ? active.getAttribute("data-filter") : "all";\n'
        '    return f && f !== "all" ? f : null;\n'
        "  }\n"
        "\n"
        "  // Optional advanced-filter controls; absent on pages without them.\n"
        '  var FILTER_IDS = ["filter-sources", "filter-topics",\n'
        '    "filter-min-score", "filter-min-comments",\n'
        '    "filter-date-from", "filter-date-to", "filter-sort"];\n'
        '  var FILTER_PARAMS = ["sources", "topics", "min_score",\n'
        '    "min_comments", "date_from", "date_to", "sort"];\n'
        "\n"
        "  function filterParams() {\n"
        '    var out = "";\n'
        "    for (var i = 0; i < FILTER_IDS.length; i++) {\n"
        "      var el = document.getElementById(FILTER_IDS[i]);\n"
        '      if (!el) continue;\n'
        "      var v = el.value.trim();\n"
        '      if (!v || v === "relevance") continue;\n'
        '      out += "&" + FILTER_PARAMS[i] + "=" + encodeURIComponent(v);\n'
        "    }\n"
        "    return out;\n"
        "  }\n"
        "\n"
        "  function syncUrl(query) {\n"
        "    if (!window.history || !window.history.replaceState) return;\n"
        '    window.history.replaceState(null, "", "?" + query);\n'
        "  }\n"
        "\n"
        "  function render(results) {\n"
        "    if (!results.length) {\n"
        '      panel.innerHTML = \'<p class="search-empty">No results</p>\';\n'
        "      panel.hidden = false;\n"
        "      return;\n"
        "    }\n"
        '    var html = "";\n'
        "    for (var i = 0; i < results.length; i++) {\n"
        "      var r = results[i];\n"
        '      var a = document.createElement("a");\n'
        "      a.href = r.url;\n"
        "      a.textContent = r.title;\n"
        '      var item = document.createElement("div");\n'
        '      item.className = "search-result";\n'
        "      item.appendChild(a);\n"
        "      html += item.outerHTML;\n"
        "    }\n"
        "    panel.innerHTML = html;\n"
        "    panel.hidden = false;\n"
        "  }\n"
        "\n"
        "  function run() {\n"
        "    var q = box.value.trim();\n"
        "    if (q.length < 2) {\n"
        '      panel.hidden = true;\n'
        '      panel.innerHTML = "";\n'
        "      return;\n"
        "    }\n"
        '    var url = "/api/search?q=" + encodeURIComponent(q);\n'
        "    var cat = activeCategory();\n"
        '    if (cat) url += "&category=" + encodeURIComponent(cat);\n'
        "    url += filterParams();\n"
        '    syncUrl(url.slice(url.indexOf("?") + 1));\n'
        "    fetch(url)\n"
        "      .then(function (resp) { return resp.ok ? resp.json() : []; })\n"
        "      .then(render)\n"
        "      .catch(function () { panel.hidden = true; });\n"
        "  }\n"
        "\n"
        '  var clear = document.getElementById("filter-clear");\n'
        "  if (clear) {\n"
        '    clear.addEventListener("click", function () {\n'
        "      for (var i = 0; i < FILTER_IDS.length; i++) {\n"
        "        var el = document.getElementById(FILTER_IDS[i]);\n"
        '        if (!el) continue;\n'
        '        if (el.tagName === "SELECT") { el.value = "relevance"; }\n'
        '        else { el.value = ""; }\n'
        "      }\n"
        "      run();\n"
        "    });\n"
        "  }\n"
        "  for (var fi = 0; fi < FILTER_IDS.length; fi++) {\n"
        "    var fel = document.getElementById(FILTER_IDS[fi]);\n"
        '    if (fel) fel.addEventListener("change", run);\n'
        "  }\n"
        "\n"
        '  box.addEventListener("input", function () {\n'
        "    if (timer) clearTimeout(timer);\n"
        "    timer = setTimeout(run, DEBOUNCE_MS);\n"
        "  });\n"
        '  box.addEventListener("keydown", function (e) {\n'
        '    if (e.key === "Enter") {\n'
        "      e.preventDefault();\n"
        "      if (timer) clearTimeout(timer);\n"
        "      run();\n"
        "    }\n"
        "  });\n"
        "})();\n"
    )


def render_vote_js() -> str:
    # Wires the up/down buttons to POST /api/vote, updates the shown points from
    # the returned distribution, and remembers the user's choice per story in
    # localStorage so the indicator survives a reload. Cookies carry the voter
    # id; fetch sends them with credentials: "same-origin".
    return (
        "(function () {\n"
        '  var STORAGE_KEY = "voted-stories";\n'
        "\n"
        "  function readVotes() {\n"
        "    try {\n"
        '      return JSON.parse(window.localStorage.getItem(STORAGE_KEY) || "{}");\n'
        "    } catch (e) {\n"
        "      return {};\n"
        "    }\n"
        "  }\n"
        "\n"
        "  function writeVotes(state) {\n"
        "    try {\n"
        "      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));\n"
        "    } catch (e) {}\n"
        "  }\n"
        "\n"
        "  function mark(li, value) {\n"
        '    var up = li.querySelector("button.vote.up");\n'
        '    var down = li.querySelector("button.vote.down");\n'
        '    if (up) up.classList.toggle("voted", value === 1);\n'
        '    if (down) down.classList.toggle("voted", value === -1);\n'
        "  }\n"
        "\n"
        "  function send(li, value) {\n"
        '    var id = li.getAttribute("data-story-id");\n'
        "    if (!id) return;\n"
        '    fetch("/api/vote", {\n'
        '      method: "POST",\n'
        '      headers: { "Content-Type": "application/json" },\n'
        '      credentials: "same-origin",\n'
        '      body: JSON.stringify({ story_id: Number(id), vote_value: value })\n'
        "    })\n"
        "      .then(function (resp) { return resp.ok ? resp.json() : null; })\n"
        "      .then(function (data) {\n"
        "        if (!data) return;\n"
        '        var points = li.querySelector(".points");\n'
        '        if (points) points.textContent = data.points + " points";\n'
        '        var down = li.querySelector(".downvotes");\n'
        '        if (down) down.textContent = data.downvotes + " downvotes";\n'
        "        var state = readVotes();\n"
        "        if (value === 0) { delete state[id]; } else { state[id] = value; }\n"
        "        writeVotes(state);\n"
        "        mark(li, value);\n"
        "      })\n"
        "      .catch(function () {});\n"
        "  }\n"
        "\n"
        "  function click(button) {\n"
        '    var li = button.closest("li.story");\n'
        "    if (!li) return;\n"
        '    var id = li.getAttribute("data-story-id");\n'
        "    var current = readVotes()[id] || 0;\n"
        '    var value = button.classList.contains("up") ? 1 : -1;\n'
        "    if (current === value) value = 0;\n"
        "    send(li, value);\n"
        "  }\n"
        "\n"
        "  function init() {\n"
        '    var state = readVotes();\n'
        '    var items = document.querySelectorAll("li.story");\n'
        "    for (var i = 0; i < items.length; i++) {\n"
        '      var id = items[i].getAttribute("data-story-id");\n'
        "      if (id && state[id]) mark(items[i], state[id]);\n"
        "    }\n"
        '    var buttons = document.querySelectorAll("button.vote");\n'
        "    for (var j = 0; j < buttons.length; j++) {\n"
        '      buttons[j].addEventListener("click", function () { click(this); });\n'
        "    }\n"
        "  }\n"
        "\n"
        '  if (document.readyState === "loading") {\n'
        '    document.addEventListener("DOMContentLoaded", init);\n'
        "  } else {\n"
        "    init();\n"
        "  }\n"
        "})();\n"
    )


def render_feed_js() -> str:
    # On load, asks /api/user/feed whether the caller is a known voter. If so it
    # hides the global feed, shows the personalized list, and wires the algorithm
    # switch (persisted via POST /api/user/preferences) and the "View all
    # stories" toggle. Anonymous callers (user_id null) keep the global feed.
    return (
        "(function () {\n"
        '  var section = document.getElementById("personalized-feed");\n'
        '  var global = document.getElementById("global-feed");\n'
        '  var list = document.getElementById("personalized-list");\n'
        '  var empty = document.getElementById("personalized-empty");\n'
        '  var toggle = document.getElementById("toggle-feed");\n'
        "  if (!section || !global || !list || !toggle) return;\n"
        '  var showingGlobal = false;\n'
        '  var algorithm = "balanced";\n'
        "\n"
        "  function escapeText(value) {\n"
        '    var div = document.createElement("div");\n'
        '    div.textContent = value == null ? "" : String(value);\n'
        "    return div.innerHTML;\n"
        "  }\n"
        "\n"
        "  function render(stories) {\n"
        '    list.innerHTML = "";\n'
        "    if (!stories || !stories.length) {\n"
        "      empty.hidden = false;\n"
        "      return;\n"
        "    }\n"
        "    empty.hidden = true;\n"
        "    for (var i = 0; i < stories.length; i++) {\n"
        "      var s = stories[i];\n"
        '      var li = document.createElement("li");\n'
        '      li.className = "story";\n'
        '      li.setAttribute("data-story-id", s.id);\n'
        "      li.innerHTML =\n"
        '        \'<a class="title" href="\' + escapeText(s.url) + \'">\' +\n'
        "        escapeText(s.title) +\n"
        "        '</a> <span class=\"meta\">' +\n"
        '        escapeText(s.source_name || "") + \' &middot; \' +\n'
        "        escapeText(s.vote_count) + ' points</span>';\n"
        "      list.appendChild(li);\n"
        "    }\n"
        "  }\n"
        "\n"
        "  function markActive() {\n"
        '    var buttons = section.querySelectorAll("button.algo");\n'
        "    for (var i = 0; i < buttons.length; i++) {\n"
        '      var on = buttons[i].getAttribute("data-algo") === algorithm;\n'
        '      buttons[i].classList.toggle("active", on);\n'
        "    }\n"
        "  }\n"
        "\n"
        "  function load() {\n"
        '    fetch("/api/user/feed?algorithm=" + encodeURIComponent(algorithm) +\n'
        '      "&limit=20", { credentials: "same-origin" })\n'
        "      .then(function (resp) { return resp.ok ? resp.json() : null; })\n"
        "      .then(function (data) {\n"
        "        if (!data || data.user_id == null) return;\n"
        "        section.hidden = false;\n"
        "        if (!showingGlobal) global.hidden = true;\n"
        "        algorithm = data.algorithm || algorithm;\n"
        "        markActive();\n"
        "        render(data.stories);\n"
        "      })\n"
        "      .catch(function () {});\n"
        "  }\n"
        "\n"
        "  function pickAlgorithm(value) {\n"
        "    algorithm = value;\n"
        "    markActive();\n"
        '    fetch("/api/user/preferences", {\n'
        '      method: "POST",\n'
        '      headers: { "Content-Type": "application/json" },\n'
        '      credentials: "same-origin",\n'
        '      body: JSON.stringify({ algorithm: value })\n'
        "    }).catch(function () {});\n"
        "    load();\n"
        "  }\n"
        "\n"
        "  function toggleView() {\n"
        "    showingGlobal = !showingGlobal;\n"
        "    global.hidden = !showingGlobal;\n"
        '    list.hidden = showingGlobal;\n'
        "    empty.hidden = showingGlobal || empty.hidden;\n"
        "    toggle.textContent =\n"
        '      showingGlobal ? "Back to your feed" : "View all stories";\n'
        "  }\n"
        "\n"
        "  function init() {\n"
        '    var buttons = section.querySelectorAll("button.algo");\n'
        "    for (var i = 0; i < buttons.length; i++) {\n"
        '      buttons[i].addEventListener("click", function () {\n'
        '        pickAlgorithm(this.getAttribute("data-algo"));\n'
        "      });\n"
        "    }\n"
        '    toggle.addEventListener("click", toggleView);\n'
        "    load();\n"
        "  }\n"
        "\n"
        '  if (document.readyState === "loading") {\n'
        '    document.addEventListener("DOMContentLoaded", init);\n'
        "  } else {\n"
        "    init();\n"
        "  }\n"
        "})();\n"
    )


def render_comments_js() -> str:
    # Lazily loads each story's thread from /api/articles/{id}/comments when its
    # "N comments" button is first toggled, renders the nested tree (indenting
    # replies and stubbing [deleted] nodes), and posts new comments / replies
    # back through the comments API. Each comment carries up/down vote arrows
    # wired to /api/comments/{id}/upvote|downvote with optimistic score updates
    # (reverted on error), a per-thread sort control (score/newest/oldest), and
    # low-score comments (collapsed flag) hidden behind a show/hide toggle.
    # Cookies carry the author/voter id.
    return (
        "(function () {\n"
        "  function esc(s) {\n"
        '    var d = document.createElement("div");\n'
        '    d.textContent = s == null ? "" : String(s);\n'
        "    return d.innerHTML;\n"
        "  }\n"
        "\n"
        "  function renderNode(c) {\n"
        '    var author = c.deleted ? "[deleted]" : (c.user_id || "anonymous");\n'
        "    var authorHtml = (!c.deleted && c.user_id)\n"
        '      ? \'<a class="author" href="user.html?u=\' +\n'
        "        encodeURIComponent(c.user_id) + '\">' + esc(author) + '</a>'\n"
        "      : esc(author);\n"
        "    var opHtml = c.is_op ? ' <span class=\"comment-op\">OP</span>' : '';\n"
        '    var children = "";\n'
        "    if (c.replies && c.replies.length) {\n"
        "      for (var i = 0; i < c.replies.length; i++) {\n"
        "        children += renderNode(c.replies[i]);\n"
        "      }\n"
        "    }\n"
        "    var reply = c.deleted ? '' :\n"
        '      \'<button type="button" class="comment-reply-toggle">Reply</button>\' +\n'
        '      \'<form class="comment-form comment-reply-form" data-parent-id="\' +\n'
        "      esc(c.id) + '\" hidden>' +\n"
        '      \'<textarea name="body" placeholder="Reply"></textarea>\' +\n'
        '      \'<button type="submit">Post reply</button></form>\';\n'
        "    var up = c.user_vote === 1 ? ' voted' : '';\n"
        "    var down = c.user_vote === -1 ? ' voted' : '';\n"
        "    var score = (c.score == null ? c.vote_count : c.score);\n"
        "    var votes = c.deleted ? '' :\n"
        '      \'<span class="comment-votes">\' +\n'
        "      '<button type=\"button\" class=\"cvote up' + up +\n"
        "      '\" aria-label=\"Upvote\">&#9650;</button>' +\n"
        "      '<span class=\"comment-score\">' + esc(score) + ' points</span>' +\n"
        "      '<button type=\"button\" class=\"cvote down' + down +\n"
        "      '\" aria-label=\"Downvote\">&#9660;</button></span> &middot; ';\n"
        "    var collapsed = c.collapsed ? ' collapsed' : '';\n"
        "    var toggle = c.collapsed ?\n"
        '      \' <button type="button" class="comment-collapse-toggle">\' +\n'
        "      '[+]</button>' : '';\n"
        '    return \'<div class="comment\' + collapsed + \'" data-comment-id="\' +\n'
        "      esc(c.id) + '\" data-user-vote=\"' + esc(c.user_vote || 0) + '\">' +\n"
        '      \'<div class="comment-meta">\' + votes + authorHtml + opHtml +\n'
        "      ' &middot; ' + esc(c.created_at || '') + toggle + '</div>' +\n"
        '      \'<div class="comment-content">\' +\n'
        '      \'<div class="comment-body">\' + esc(c.body) + \'</div>\' + reply +\n'
        '      \'<div class="comment-replies">\' + children + \'</div></div></div>\';\n'
        "  }\n"
        "\n"
        "  function load(story, panel) {\n"
        '    var id = story.getAttribute("data-story-id");\n'
        "    var sort = panel.getAttribute('data-sort') || 'score';\n"
        '    fetch("/api/articles/" + encodeURIComponent(id) + "/comments?sort=" +\n'
        "      encodeURIComponent(sort), { credentials: \"same-origin\" })\n"
        "      .then(function (r) { return r.ok ? r.json() : []; })\n"
        "      .then(function (thread) {\n"
        "        var html = '<div class=\"comment-sort\">Sort: ' +\n"
        "          '<select class=\"comment-sort-select\">' +\n"
        "          '<option value=\"score\">Score</option>' +\n"
        "          '<option value=\"newest\">Newest</option>' +\n"
        "          '<option value=\"oldest\">Oldest</option></select></div>';\n"
        "        for (var i = 0; i < thread.length; i++) {\n"
        "          html += renderNode(thread[i]);\n"
        "        }\n"
        "        html += '<form class=\"comment-form\">' +\n"
        "          '<textarea name=\"body\" placeholder=\"Add a comment\"></textarea>' +\n"
        "          '<button type=\"submit\">Post</button></form>';\n"
        "        panel.innerHTML = html;\n"
        "        var sel = panel.querySelector('.comment-sort-select');\n"
        "        if (sel) sel.value = sort;\n"
        "      })\n"
        "      .catch(function () {});\n"
        "  }\n"
        "\n"
        "  function vote(comment, direction) {\n"
        "    var id = comment.getAttribute('data-comment-id');\n"
        "    if (!id) return;\n"
        "    var prev = Number(comment.getAttribute('data-user-vote')) || 0;\n"
        "    var next = prev === direction ? 0 : direction;\n"
        "    var scoreEl = comment.querySelector('.comment-score');\n"
        "    var prevText = scoreEl ? scoreEl.textContent : null;\n"
        "    // Optimistic: shift the displayed score by the vote delta at once.\n"
        "    applyVote(comment, next, scoreEl, prev !== 0 || next !== 0 ?\n"
        "      (next - prev) : 0);\n"
        "    var path = direction === 1 ? 'upvote' : 'downvote';\n"
        "    fetch('/api/comments/' + encodeURIComponent(id) + '/' + path, {\n"
        '      method: "POST",\n'
        '      headers: { "Content-Type": "application/json" },\n'
        '      credentials: "same-origin"\n'
        "    })\n"
        "      .then(function (r) { return r.ok ? r.json() : null; })\n"
        "      .then(function (data) {\n"
        "        if (!data) throw new Error('vote failed');\n"
        "        comment.setAttribute('data-user-vote', data.user_vote);\n"
        "        if (scoreEl) scoreEl.textContent = data.score + ' points';\n"
        "        markVote(comment, data.user_vote);\n"
        "      })\n"
        "      .catch(function () {\n"
        "        // Revert the optimistic change on any error.\n"
        "        comment.setAttribute('data-user-vote', prev);\n"
        "        if (scoreEl && prevText != null) scoreEl.textContent = prevText;\n"
        "        markVote(comment, prev);\n"
        "      });\n"
        "  }\n"
        "\n"
        "  function applyVote(comment, next, scoreEl, delta) {\n"
        "    comment.setAttribute('data-user-vote', next);\n"
        "    markVote(comment, next);\n"
        "    if (scoreEl && delta) {\n"
        "      var cur = parseInt(scoreEl.textContent, 10);\n"
        "      if (!isNaN(cur)) scoreEl.textContent = (cur + delta) + ' points';\n"
        "    }\n"
        "  }\n"
        "\n"
        "  function markVote(comment, value) {\n"
        "    var up = comment.querySelector(':scope > .comment-meta .cvote.up');\n"
        "    var down = comment.querySelector(':scope > .comment-meta .cvote.down');\n"
        "    if (up) up.classList.toggle('voted', value === 1);\n"
        "    if (down) down.classList.toggle('voted', value === -1);\n"
        "  }\n"
        "\n"
        "  function submit(story, panel, form) {\n"
        '    var ta = form.querySelector("textarea");\n'
        "    var body = ta ? ta.value.trim() : '';\n"
        "    if (!body) return;\n"
        '    var id = story.getAttribute("data-story-id");\n'
        '    var parent = form.getAttribute("data-parent-id");\n'
        "    var payload = { story_id: Number(id), body: body };\n"
        "    if (parent) payload.parent_comment_id = Number(parent);\n"
        '    fetch("/api/comments", {\n'
        '      method: "POST",\n'
        '      headers: { "Content-Type": "application/json" },\n'
        '      credentials: "same-origin",\n'
        "      body: JSON.stringify(payload)\n"
        "    })\n"
        "      .then(function (r) { return r.ok ? r.json() : null; })\n"
        "      .then(function (data) { if (data) load(story, panel); });\n"
        "  }\n"
        "\n"
        "  function init() {\n"
        '    var stories = document.querySelectorAll("li.story");\n'
        "    for (var i = 0; i < stories.length; i++) {\n"
        "      (function (story) {\n"
        '        var toggle = story.querySelector(".comments-toggle");\n'
        '        var panel = story.querySelector(".comments");\n'
        "        if (!toggle || !panel) return;\n"
        "        var loaded = false;\n"
        '        toggle.addEventListener("click", function () {\n'
        "          panel.hidden = !panel.hidden;\n"
        "          if (!panel.hidden && !loaded) { loaded = true; load(story, panel); }\n"
        "        });\n"
        '        panel.addEventListener("submit", function (e) {\n'
        '          if (e.target && e.target.classList.contains("comment-form")) {\n'
        "            e.preventDefault();\n"
        "            submit(story, panel, e.target);\n"
        "          }\n"
        "        });\n"
        '        panel.addEventListener("change", function (e) {\n'
        "          if (e.target &&\n"
        '              e.target.classList.contains("comment-sort-select")) {\n'
        "            panel.setAttribute('data-sort', e.target.value);\n"
        "            load(story, panel);\n"
        "          }\n"
        "        });\n"
        '        panel.addEventListener("click", function (e) {\n'
        "          var t = e.target;\n"
        "          if (!t) return;\n"
        '          if (t.classList.contains("comment-reply-toggle")) {\n'
        '            var f = t.nextElementSibling;\n'
        "            if (f) f.hidden = !f.hidden;\n"
        '          } else if (t.classList.contains("comment-collapse-toggle")) {\n'
        '            var node = t.closest(".comment");\n'
        '            if (node) node.classList.toggle("collapsed");\n'
        '          } else if (t.classList.contains("cvote")) {\n'
        '            var c = t.closest(".comment");\n'
        "            if (c) vote(c, t.classList.contains('up') ? 1 : -1);\n"
        "          }\n"
        "        });\n"
        "      })(stories[i]);\n"
        "    }\n"
        "  }\n"
        "\n"
        '  if (document.readyState === "loading") {\n'
        '    document.addEventListener("DOMContentLoaded", init);\n'
        "  } else {\n"
        "    init();\n"
        "  }\n"
        "})();\n"
    )


def _page_shell(title: str, main_inner: str, scripts: list[str]) -> str:
    """Return a full HTML document reusing the site chrome (header, nav, css).

    *main_inner* is the raw HTML placed inside ``<main>`` (a shell the page's JS
    fills in), and *scripts* are script filenames loaded at the end of body.
    """
    script_tags = "\n".join(
        f'  <script src="{escape(s, quote=True)}"></script>' for s in scripts
    )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"  <title>{escape(title)}</title>\n"
        '  <link rel="stylesheet" href="style.css">\n'
        "</head>\n"
        "<body>\n"
        "  <header>\n"
        "    <h1>Important News</h1>\n"
        '    <nav class="site-nav">\n'
        '      <a href="index.html">Home</a>\n'
        '      <a href="bookmarks.html">Saved</a>\n'
        '      <a href="leaderboard.html">Leaderboard</a>\n'
        "    </nav>\n"
        "  </header>\n"
        "  <main>\n"
        f"{main_inner}\n"
        "  </main>\n"
        '  <footer>Generated static site &middot; AI &amp; Aerospace</footer>\n'
        f"{script_tags}\n"
        "</body>\n"
        "</html>\n"
    )


def render_user_page() -> str:
    """Return the static profile-page shell (``user.html``).

    The username is read from the ``?u=`` query string by ``user.js``, which
    fills ``#profile`` with the user's karma, bio, counts, and paginated
    activity tabs fetched from the profile API.
    """
    return _page_shell(
        "User Profile",
        '    <section id="profile" data-loading="true">\n'
        '      <p class="muted">Loading profile…</p>\n'
        "    </section>",
        ["user.js"],
    )


def render_leaderboard_page() -> str:
    """Return the static leaderboard-page shell (``leaderboard.html``).

    ``leaderboard.js`` fills ``#leaderboard`` with the top users by karma fetched
    from ``/api/users/leaderboard``; each row links to that user's profile page.
    """
    return _page_shell(
        "Leaderboard",
        "    <h2>Top users by karma</h2>\n"
        '    <ol class="leaderboard" id="leaderboard">\n'
        '      <li class="muted">Loading…</li>\n'
        "    </ol>",
        ["leaderboard.js"],
    )


def render_user_js() -> str:
    # Resolves the username from ?u=, fetches the public profile, and renders
    # karma + cached counts. Two tabs (articles, comments) lazily page through
    # the activity APIs; a private or unknown user renders a stub with no
    # activity. Karma is re-fetched whenever the profile tab is shown so a vote
    # cast elsewhere is reflected on the next visit.
    return (
        "(function () {\n"
        "  function esc(s) {\n"
        '    var d = document.createElement("div");\n'
        '    d.textContent = s == null ? "" : String(s);\n'
        "    return d.innerHTML;\n"
        "  }\n"
        "  function param(name) {\n"
        "    var m = new RegExp('[?&]' + name + '=([^&]*)').exec(\n"
        '      window.location.search);\n'
        '    return m ? decodeURIComponent(m[1].replace(/\\+/g, " ")) : "";\n'
        "  }\n"
        "\n"
        '  var root = document.getElementById("profile");\n'
        "  var user = param('u');\n"
        "  if (!root) return;\n"
        "  if (!user) {\n"
        "    root.innerHTML = '<p class=\"muted\">No user specified.</p>';\n"
        "    return;\n"
        "  }\n"
        "\n"
        "  var state = { tab: 'articles', pages: { articles: 1, comments: 1 } };\n"
        "\n"
        "  function renderActivity(panel, data, kind) {\n"
        "    var html = '';\n"
        "    var items = (data && data.items) || [];\n"
        "    if (!items.length) {\n"
        "      html = '<p class=\"muted\">Nothing here yet.</p>';\n"
        "    } else {\n"
        "      for (var i = 0; i < items.length; i++) {\n"
        "        var it = items[i];\n"
        "        if (kind === 'articles') {\n"
        "          html += '<div class=\"activity-item\">' +\n"
        "            '<a class=\"title\" href=\"' + esc(it.url) + '\">' +\n"
        "            esc(it.title) + '</a> <span class=\"activity-kind\">' +\n"
        "            esc(it.activity) + ' &middot; ' + esc(it.timestamp || '') +\n"
        "            '</span></div>';\n"
        "        } else {\n"
        "          html += '<div class=\"activity-item\">' + esc(it.body) +\n"
        "            ' <span class=\"activity-kind\">' + esc(it.vote_count) +\n"
        "            ' points &middot; ' + esc(it.timestamp || '') +\n"
        "            '</span></div>';\n"
        "        }\n"
        "      }\n"
        "    }\n"
        "    var total = (data && data.total) || 0;\n"
        "    var perPage = (data && data.per_page) || 20;\n"
        "    var page = state.pages[kind];\n"
        "    var hasNext = page * perPage < total;\n"
        "    html += '<div class=\"pager\">' +\n"
        "      '<button type=\"button\" class=\"page-btn\" data-dir=\"-1\"' +\n"
        "      (page <= 1 ? ' disabled' : '') + '>Prev</button>' +\n"
        "      '<span>Page ' + page + '</span>' +\n"
        "      '<button type=\"button\" class=\"page-btn\" data-dir=\"1\"' +\n"
        "      (hasNext ? '' : ' disabled') + '>Next</button></div>';\n"
        "    panel.innerHTML = html;\n"
        "    var btns = panel.querySelectorAll('button.page-btn');\n"
        "    for (var j = 0; j < btns.length; j++) {\n"
        "      btns[j].addEventListener('click', function () {\n"
        "        if (this.disabled) return;\n"
        "        state.pages[kind] += Number(this.getAttribute('data-dir'));\n"
        "        if (state.pages[kind] < 1) state.pages[kind] = 1;\n"
        "        loadActivity(panel, kind);\n"
        "      });\n"
        "    }\n"
        "  }\n"
        "\n"
        "  function loadActivity(panel, kind) {\n"
        "    var url = '/api/users/' + encodeURIComponent(user) + '/' + kind +\n"
        "      '?page=' + state.pages[kind];\n"
        "    fetch(url)\n"
        "      .then(function (r) { return r.ok ? r.json() : null; })\n"
        "      .then(function (data) {\n"
        "        if (data) renderActivity(panel, data, kind);\n"
        "      })\n"
        "      .catch(function () {});\n"
        "  }\n"
        "\n"
        "  function renderProfile(p) {\n"
        "    if (p.is_private) {\n"
        "      root.innerHTML = '<div class=\"profile-header\"><h2>' +\n"
        "        esc(p.username) + '</h2><p class=\"private-note\">' +\n"
        "        'This profile is private.</p></div>';\n"
        "      return;\n"
        "    }\n"
        "    root.innerHTML = '<div class=\"profile-header\">' +\n"
        "      '<h2>' + esc(p.username) + '</h2>' +\n"
        "      '<div class=\"profile-karma\">' + esc(p.karma) + ' karma</div>' +\n"
        "      (p.bio ? '<p class=\"profile-bio\">' + esc(p.bio) + '</p>' : '') +\n"
        "      '<div class=\"profile-stats\">' + esc(p.submission_count) +\n"
        "      ' submitted &middot; ' + esc(p.vote_count) + ' votes &middot; ' +\n"
        "      esc(p.comment_count) + ' comments</div></div>' +\n"
        "      '<div class=\"profile-tabs\">' +\n"
        "      '<button type=\"button\" class=\"tab active\" data-tab=\"articles\">' +\n"
        "      'Articles</button>' +\n"
        "      '<button type=\"button\" class=\"tab\" data-tab=\"comments\">' +\n"
        "      'Comments</button></div>' +\n"
        "      '<div id=\"activity\"></div>';\n"
        "    var panel = document.getElementById('activity');\n"
        "    var tabs = root.querySelectorAll('button.tab');\n"
        "    for (var i = 0; i < tabs.length; i++) {\n"
        "      tabs[i].addEventListener('click', function () {\n"
        "        var kind = this.getAttribute('data-tab');\n"
        "        state.tab = kind;\n"
        "        for (var k = 0; k < tabs.length; k++) {\n"
        "          tabs[k].classList.toggle('active',\n"
        "            tabs[k].getAttribute('data-tab') === kind);\n"
        "        }\n"
        "        loadActivity(panel, kind);\n"
        "      });\n"
        "    }\n"
        "    loadActivity(panel, 'articles');\n"
        "  }\n"
        "\n"
        "  fetch('/api/users/' + encodeURIComponent(user))\n"
        "    .then(function (r) {\n"
        "      if (r.status === 404) return { _missing: true };\n"
        "      return r.ok ? r.json() : null;\n"
        "    })\n"
        "    .then(function (p) {\n"
        "      if (!p) { root.innerHTML = '<p class=\"muted\">Could not load profile.</p>'; return; }\n"
        "      if (p._missing) { root.innerHTML = '<p class=\"muted\">User not found.</p>'; return; }\n"
        "      renderProfile(p);\n"
        "    })\n"
        "    .catch(function () {\n"
        "      root.innerHTML = '<p class=\"muted\">Could not load profile.</p>';\n"
        "    });\n"
        "})();\n"
    )


def render_leaderboard_js() -> str:
    # Fetches the top users by karma from /api/users/leaderboard and renders a
    # ranked list; each username links to its profile page.
    return (
        "(function () {\n"
        "  function esc(s) {\n"
        '    var d = document.createElement("div");\n'
        '    d.textContent = s == null ? "" : String(s);\n'
        "    return d.innerHTML;\n"
        "  }\n"
        '  var root = document.getElementById("leaderboard");\n'
        "  if (!root) return;\n"
        "  fetch('/api/users/leaderboard')\n"
        "    .then(function (r) { return r.ok ? r.json() : []; })\n"
        "    .then(function (rows) {\n"
        "      if (!rows.length) {\n"
        "        root.innerHTML = '<li class=\"muted\">No users yet.</li>';\n"
        "        return;\n"
        "      }\n"
        "      var html = '';\n"
        "      for (var i = 0; i < rows.length; i++) {\n"
        "        var u = rows[i];\n"
        "        html += '<li><span class=\"lb-rank\">' + esc(u.rank) +\n"
        "          '.</span><a class=\"author\" href=\"user.html?u=' +\n"
        "          encodeURIComponent(u.username) + '\">' + esc(u.username) +\n"
        "          '</a><span class=\"lb-karma\">' + esc(u.karma) +\n"
        "          ' karma</span></li>';\n"
        "      }\n"
        "      root.innerHTML = html;\n"
        "    })\n"
        "    .catch(function () {\n"
        "      root.innerHTML = '<li class=\"muted\">Could not load leaderboard.</li>';\n"
        "    });\n"
        "})();\n"
    )


def render_bookmarks_page() -> str:
    """Return the static "Saved" page shell (``bookmarks.html``).

    ``bookmarks.js`` fills ``#bookmarks`` with the caller's private saved-story
    list fetched from ``/api/user/bookmarks``, offers a category filter, and
    supports individual and bulk removal.
    """
    return _page_shell(
        "Saved",
        "    <h2>Saved for later</h2>\n"
        '    <div class="bookmarks-controls">\n'
        '      <select id="bookmark-category" aria-label="Filter by category">\n'
        '        <option value="">All categories</option>\n'
        '        <option value="ai">AI</option>\n'
        '        <option value="aerospace">Aerospace</option>\n'
        '        <option value="both">Both</option>\n'
        "      </select>\n"
        '      <button type="button" id="bookmark-bulk-delete">'
        "Delete selected</button>\n"
        "    </div>\n"
        '    <ul class="bookmarks" id="bookmarks">\n'
        '      <li class="muted">Loading…</li>\n'
        "    </ul>",
        ["bookmark.js"],
    )


def render_bookmark_js() -> str:
    # Two responsibilities, one file: on listing pages it wires every
    # .bookmark-toggle button to POST /api/articles/{id}/bookmark and updates the
    # icon (★/☆) + count from the response; on bookmarks.html it fills #bookmarks
    # from /api/user/bookmarks with a category filter, per-item remove, and bulk
    # delete. Cookies carry the user id via credentials: "same-origin".
    return (
        "(function () {\n"
        "  function esc(s) {\n"
        '    var d = document.createElement("div");\n'
        '    d.textContent = s == null ? "" : String(s);\n'
        "    return d.innerHTML;\n"
        "  }\n"
        "\n"
        "  function setToggle(button, bookmarked, count) {\n"
        '    button.setAttribute("aria-pressed", bookmarked ? "true" : "false");\n'
        '    button.classList.toggle("saved", !!bookmarked);\n'
        '    var star = bookmarked ? "\\u2605" : "\\u2606";\n'
        '    var c = button.querySelector(".bookmark-count");\n'
        '    button.textContent = star + " ";\n'
        '    var span = document.createElement("span");\n'
        '    span.className = "bookmark-count";\n'
        '    span.textContent = count == null ? (c ? c.textContent : "0") : count;\n'
        "    button.appendChild(span);\n"
        "  }\n"
        "\n"
        "  function toggle(button) {\n"
        '    var li = button.closest("li.story");\n'
        '    var id = li && li.getAttribute("data-story-id");\n'
        "    if (!id) return;\n"
        '    fetch("/api/articles/" + Number(id) + "/bookmark", {\n'
        '      method: "POST",\n'
        '      credentials: "same-origin"\n'
        "    })\n"
        "      .then(function (r) { return r.ok ? r.json() : null; })\n"
        "      .then(function (data) {\n"
        "        if (!data) return;\n"
        "        setToggle(button, data.bookmarked, data.bookmark_count);\n"
        "      })\n"
        "      .catch(function () {});\n"
        "  }\n"
        "\n"
        "  function initToggles() {\n"
        '    var buttons = document.querySelectorAll("button.bookmark-toggle");\n'
        "    for (var i = 0; i < buttons.length; i++) {\n"
        '      buttons[i].addEventListener("click", function () { toggle(this); });\n'
        "    }\n"
        "  }\n"
        "\n"
        '  var listRoot = document.getElementById("bookmarks");\n'
        "\n"
        "  function renderList(data) {\n"
        "    var items = (data && data.items) || [];\n"
        "    if (!items.length) {\n"
        "      listRoot.innerHTML = '<li class=\"muted\">No saved stories yet.</li>';\n"
        "      return;\n"
        "    }\n"
        "    var html = '';\n"
        "    for (var i = 0; i < items.length; i++) {\n"
        "      var it = items[i];\n"
        "      html += '<li class=\"bookmark\" data-story-id=\"' + esc(it.story_id) +\n"
        "        '\"><label><input type=\"checkbox\" class=\"bookmark-select\"> ' +\n"
        "        '</label><a class=\"title\" href=\"' + esc(it.url) + '\">' +\n"
        "        esc(it.title) + '</a> <span class=\"meta\">Bookmarked on ' +\n"
        "        esc(it.created_at || '') + ' &middot; ' + esc(it.topic) +\n"
        "        '</span> <button type=\"button\" class=\"bookmark-remove\">"
        "Remove</button></li>';\n"
        "    }\n"
        "    listRoot.innerHTML = html;\n"
        "    bindListActions();\n"
        "  }\n"
        "\n"
        "  function load() {\n"
        '    var cat = document.getElementById("bookmark-category");\n'
        '    var url = "/api/user/bookmarks";\n'
        "    if (cat && cat.value) url += '?category=' + encodeURIComponent(cat.value);\n"
        '    fetch(url, { credentials: "same-origin" })\n'
        "      .then(function (r) { return r.ok ? r.json() : null; })\n"
        "      .then(function (data) { if (data) renderList(data); })\n"
        "      .catch(function () {\n"
        "        listRoot.innerHTML = '<li class=\"muted\">Could not load bookmarks.</li>';\n"
        "      });\n"
        "  }\n"
        "\n"
        "  function removeOne(id) {\n"
        '    fetch("/api/articles/" + Number(id) + "/bookmark", {\n'
        '      method: "DELETE",\n'
        '      credentials: "same-origin"\n'
        "    }).then(function () { load(); }).catch(function () {});\n"
        "  }\n"
        "\n"
        "  function bulkDelete() {\n"
        '    var boxes = listRoot.querySelectorAll(".bookmark-select:checked");\n'
        "    var ids = [];\n"
        "    for (var i = 0; i < boxes.length; i++) {\n"
        '      var li = boxes[i].closest("li.bookmark");\n'
        '      if (li) ids.push(Number(li.getAttribute("data-story-id")));\n'
        "    }\n"
        "    if (!ids.length) return;\n"
        '    fetch("/api/user/bookmarks/bulk-delete", {\n'
        '      method: "POST",\n'
        '      headers: { "Content-Type": "application/json" },\n'
        '      credentials: "same-origin",\n'
        '      body: JSON.stringify({ story_ids: ids })\n'
        "    }).then(function () { load(); }).catch(function () {});\n"
        "  }\n"
        "\n"
        "  function bindListActions() {\n"
        '    var removes = listRoot.querySelectorAll(".bookmark-remove");\n'
        "    for (var i = 0; i < removes.length; i++) {\n"
        "      removes[i].addEventListener('click', function () {\n"
        '        var li = this.closest("li.bookmark");\n'
        '        if (li) removeOne(li.getAttribute("data-story-id"));\n'
        "      });\n"
        "    }\n"
        "  }\n"
        "\n"
        "  function init() {\n"
        "    initToggles();\n"
        "    if (listRoot) {\n"
        '      var cat = document.getElementById("bookmark-category");\n'
        '      if (cat) cat.addEventListener("change", load);\n'
        '      var bulk = document.getElementById("bookmark-bulk-delete");\n'
        '      if (bulk) bulk.addEventListener("click", bulkDelete);\n'
        "      load();\n"
        "    }\n"
        "  }\n"
        "\n"
        '  if (document.readyState === "loading") {\n'
        '    document.addEventListener("DOMContentLoaded", init);\n'
        "  } else {\n"
        "    init();\n"
        "  }\n"
        "})();\n"
    )


def write_feeds(out_path: Path, stories: list[Story]) -> list[Path]:
    """Write the main RSS feed plus one feed per category with stories.

    Produces ``feed.rss`` (all stories) and ``feed-<category>.rss`` for every
    category in :data:`CATEGORY_FILTERS` that has at least one matching story,
    mirroring a ``/feed.rss?category=<cat>`` query on a static host. Returns
    the paths written.
    """
    written = [out_path / "feed.rss"]
    written[0].write_text(generate_rss(stories), encoding="utf-8")
    for category in CATEGORY_FILTERS:
        if any(story_in_category(s.topic, category) for s in stories):
            path = out_path / f"feed-{category}.rss"
            path.write_text(
                generate_rss(stories, category=category), encoding="utf-8"
            )
            written.append(path)
    return written


def generate_site(
    engine: Engine | None = None, out_dir: Path | str = _DEFAULT_OUT_DIR
) -> Path:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    if engine is None:
        engine = get_engine()
    session = get_session(engine)
    try:
        stories = fetch_stories(session)
        discussions_map = {
            s.id: get_discussions(session, s.id) for s in stories
        }
    finally:
        session.close()

    (out_path / "index.html").write_text(
        render_html(group_by_topic(stories), discussions_map), encoding="utf-8"
    )
    (out_path / "style.css").write_text(render_css(), encoding="utf-8")
    (out_path / "filter.js").write_text(render_js(), encoding="utf-8")
    (out_path / "search.js").write_text(render_search_js(), encoding="utf-8")
    (out_path / "vote.js").write_text(render_vote_js(), encoding="utf-8")
    (out_path / "comments.js").write_text(render_comments_js(), encoding="utf-8")
    (out_path / "feed.js").write_text(render_feed_js(), encoding="utf-8")
    (out_path / "user.html").write_text(render_user_page(), encoding="utf-8")
    (out_path / "user.js").write_text(render_user_js(), encoding="utf-8")
    (out_path / "leaderboard.html").write_text(
        render_leaderboard_page(), encoding="utf-8"
    )
    (out_path / "leaderboard.js").write_text(
        render_leaderboard_js(), encoding="utf-8"
    )
    (out_path / "bookmarks.html").write_text(
        render_bookmarks_page(), encoding="utf-8"
    )
    (out_path / "bookmark.js").write_text(render_bookmark_js(), encoding="utf-8")
    write_feeds(out_path, stories)
    return out_path


if __name__ == "__main__":
    target = generate_site()
    print(f"Wrote {target / 'index.html'} and {target / 'style.css'}")
