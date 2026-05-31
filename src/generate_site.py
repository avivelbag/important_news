from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import Engine, select

from src.db import get_engine, get_session
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
    stmt = select(Story).order_by(
        Story.computed_score.desc(),
        Story.published_at.desc(),
        Story.id.asc(),
    )
    return list(session.scalars(stmt).all())


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


def _domain(url: str) -> str:
    netloc = urlparse(url).netloc
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def _format_timestamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M")


def render_story(story: Story, index: int) -> str:
    domain = _domain(story.url)
    domain_html = (
        f' <span class="domain">({escape(domain)})</span>' if domain else ""
    )
    points = story.vote_count or story.raw_score or 0
    return (
        '    <li class="story">\n'
        f'      <span class="rank">{index}.</span>\n'
        '      <span class="story-main">\n'
        f'        <a class="title" href="{escape(story.url, quote=True)}">'
        f"{escape(story.title)}</a>{domain_html}\n"
        '        <span class="meta">'
        f"{points} points &middot; {escape(story.source_name)} "
        f"&middot; {escape(_format_timestamp(story.published_at))}</span>\n"
        "      </span>\n"
        "    </li>"
    )


def render_section(topic: str, stories: list[Story]) -> str:
    label = _TOPIC_LABELS.get(topic, topic.title())
    rows = "\n".join(
        render_story(story, i) for i, story in enumerate(stories, start=1)
    )
    topic_attr = escape(topic, quote=True)
    return (
        f'  <section class="topic" id="topic-{topic_attr}" '
        f'data-topic="{topic_attr}">\n'
        f"    <h2>{escape(label)}</h2>\n"
        f'    <ol class="stories">\n{rows}\n    </ol>\n'
        "  </section>"
    )


def render_html(grouped: dict[str, list[Story]]) -> str:
    if grouped:
        body = "\n".join(
            render_section(topic, stories) for topic, stories in grouped.items()
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
        '    <form class="search" role="search" autocomplete="off">\n'
        '      <input type="search" id="search-box" name="q" '
        'placeholder="Search stories…" minlength="2" maxlength="100" '
        'aria-label="Search stories">\n'
        '      <div id="search-results" class="search-results" hidden></div>\n'
        "    </form>\n"
        f'    <nav class="filters">\n{nav}\n    </nav>\n'
        "  </header>\n"
        '  <main>\n'
        f"{body}\n"
        "  </main>\n"
        '  <footer>Generated static site &middot; AI &amp; Aerospace</footer>\n'
        '  <script src="filter.js"></script>\n'
        '  <script src="search.js"></script>\n'
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
        ".story-main { display: flex; flex-direction: column; }\n"
        "a.title { color: #222; text-decoration: none; font-size: 0.95rem; }\n"
        "a.title:hover { text-decoration: underline; }\n"
        ".domain { color: var(--muted); font-size: 0.8rem; }\n"
        ".meta { color: var(--muted); font-size: 0.78rem; }\n"
        ".empty { color: var(--muted); font-style: italic; }\n"
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
        "    fetch(url)\n"
        "      .then(function (resp) { return resp.ok ? resp.json() : []; })\n"
        "      .then(render)\n"
        "      .catch(function () { panel.hidden = true; });\n"
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
    finally:
        session.close()

    (out_path / "index.html").write_text(
        render_html(group_by_topic(stories)), encoding="utf-8"
    )
    (out_path / "style.css").write_text(render_css(), encoding="utf-8")
    (out_path / "filter.js").write_text(render_js(), encoding="utf-8")
    (out_path / "search.js").write_text(render_search_js(), encoding="utf-8")
    write_feeds(out_path, stories)
    return out_path


if __name__ == "__main__":
    target = generate_site()
    print(f"Wrote {target / 'index.html'} and {target / 'style.css'}")
