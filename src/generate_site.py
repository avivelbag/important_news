from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import Engine, select

from src.db import get_engine, get_session
from src.models import Story

_DEFAULT_OUT_DIR = Path("docs")

_TOPIC_LABELS = {
    "ai": "AI",
    "aerospace": "Aerospace",
    "both": "AI & Aerospace",
}

# Order sections are rendered in; topics outside this list are appended after,
# sorted alphabetically, so an unexpected topic value never crashes the build.
_TOPIC_ORDER = ["ai", "aerospace", "both"]


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
    return (
        f'  <section class="topic" id="topic-{escape(topic, quote=True)}">\n'
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
        f'      <a href="#topic-{escape(topic, quote=True)}">'
        f"{escape(_TOPIC_LABELS.get(topic, topic.title()))}</a>"
        for topic in grouped
    )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "  <title>Important News</title>\n"
        '  <link rel="stylesheet" href="style.css">\n'
        "</head>\n"
        "<body>\n"
        "  <header>\n"
        '    <h1>Important News</h1>\n'
        f'    <nav>\n{nav}\n    </nav>\n'
        "  </header>\n"
        '  <main>\n'
        f"{body}\n"
        "  </main>\n"
        '  <footer>Generated static site &middot; AI &amp; Aerospace</footer>\n'
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
        "nav { margin-top: 0.25rem; }\n"
        "nav a { color: #fff; margin-right: 1rem; text-decoration: none; font-size: 0.85rem; }\n"
        "nav a:hover { text-decoration: underline; }\n"
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


def generate_site(
    engine: Engine | None = None, out_dir: Path | str = _DEFAULT_OUT_DIR
) -> Path:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    if engine is None:
        engine = get_engine()
    session = get_session(engine)
    try:
        grouped = group_by_topic(fetch_stories(session))
    finally:
        session.close()

    (out_path / "index.html").write_text(render_html(grouped), encoding="utf-8")
    (out_path / "style.css").write_text(render_css(), encoding="utf-8")
    return out_path


if __name__ == "__main__":
    target = generate_site()
    print(f"Wrote {target / 'index.html'} and {target / 'style.css'}")
