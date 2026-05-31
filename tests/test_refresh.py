import datetime as dt
from pathlib import Path

import pytest

import scripts.refresh as refresh_mod
import src.db as db
import src.models as models
import src.scraper as scraper

NOW = dt.datetime(2024, 6, 1, 0, 0, 0)


@pytest.fixture()
def engine():
    eng = db.get_engine("sqlite://")
    db.init_db(eng)
    yield eng
    eng.dispose()


def _insert_stories(engine, titles):
    session = db.get_session(engine)
    try:
        for i, title in enumerate(titles):
            session.add(
                models.Story(
                    title=title,
                    url=f"https://example.com/story-{i}",
                    source_name="Test Source",
                    topic="ai",
                    published_at=NOW,
                    fetched_at=NOW,
                )
            )
        session.commit()
    finally:
        session.close()


def _fake_scraper(per_source, errors=0, titles=()):
    def run(engine, *args, **kwargs):
        _insert_stories(engine, titles)
        return scraper.ScrapeResult(
            inserted=sum(per_source.values()),
            errors=errors,
            per_source=dict(per_source),
        )

    return run


def test_refresh_scrapes_then_writes_site(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(
        scraper,
        "run_scraper",
        _fake_scraper({"Hacker News": 2}, titles=["GPT model news", "LLM update"]),
    )

    summary = refresh_mod.refresh(engine=engine, out_dir=tmp_path)

    assert summary.inserted == 2
    assert summary.errors == 0
    assert summary.per_source == {"Hacker News": 2}
    index = tmp_path / "index.html"
    assert index.exists()
    assert (tmp_path / "style.css").exists()
    html = index.read_text(encoding="utf-8")
    assert "GPT model news" in html
    assert "LLM update" in html


def test_refresh_empty_db_still_writes_placeholder_site(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(scraper, "run_scraper", _fake_scraper({}, titles=()))

    summary = refresh_mod.refresh(engine=engine, out_dir=tmp_path)

    assert summary.inserted == 0
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "No stories yet." in html


def test_refresh_summary_format_lines_singular_and_per_source(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(
        scraper,
        "run_scraper",
        _fake_scraper({"NASA": 1, "HN": 0}, errors=1, titles=["Rocket launch"]),
    )

    summary = refresh_mod.refresh(engine=engine, out_dir=tmp_path)
    lines = summary.format_lines()

    assert lines[0] == "Inserted 1 new story"
    assert "Errors: 1" in lines[1]
    assert any("HN: 0" in line for line in lines)
    assert any("NASA: 1" in line for line in lines)


def test_refresh_creates_missing_out_dir(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(scraper, "run_scraper", _fake_scraper({"HN": 0}))
    nested = tmp_path / "docs" / "nested"

    summary = refresh_mod.refresh(engine=engine, out_dir=nested)

    assert Path(summary.out_dir) == nested
    assert (nested / "index.html").exists()


def test_main_returns_1_on_total_outage(monkeypatch):
    monkeypatch.setattr(
        refresh_mod,
        "refresh",
        lambda: refresh_mod.RefreshSummary(
            inserted=0, errors=3, per_source={}, out_dir=Path("docs")
        ),
    )
    assert refresh_mod.main() == 1


def test_main_returns_0_when_some_inserted_despite_errors(monkeypatch):
    monkeypatch.setattr(
        refresh_mod,
        "refresh",
        lambda: refresh_mod.RefreshSummary(
            inserted=5, errors=2, per_source={"HN": 5}, out_dir=Path("docs")
        ),
    )
    assert refresh_mod.main() == 0
