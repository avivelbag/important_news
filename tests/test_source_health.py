"""Tests for source health tracking, status rollup, and the dashboard report."""

import datetime as dt

import pytest

import src.db as db
import src.models as models
import src.scraper as scraper
import src.source_health as sh

NOW = dt.datetime(2024, 6, 1, 0, 0, 0, tzinfo=dt.timezone.utc)

RSS_AI = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>New neural network beats GPT on a machine learning benchmark</title>
    <link>https://example.com/ai-1</link>
    <pubDate>Mon, 01 Jan 2024 12:00:00 +0000</pubDate>
  </item>
</channel></rss>
"""


def _engine():
    engine = db.get_engine("sqlite://")
    db.init_db(engine)
    return engine


def _make_fetcher(mapping):
    def fetch(url):
        return mapping[url]

    return fetch


def _add_source(session, name="Feed"):
    source = models.Source(name=name, url="http://feed")
    session.add(source)
    session.flush()
    return source


# ---------------------------------------------------------------------------
# status_for_failures / colour mapping
# ---------------------------------------------------------------------------


def test_status_for_failures_thresholds():
    assert sh.status_for_failures(0) == sh.STATUS_HEALTHY
    assert sh.status_for_failures(1, threshold=3) == sh.STATUS_DEGRADED
    assert sh.status_for_failures(2, threshold=3) == sh.STATUS_DEGRADED
    assert sh.status_for_failures(3, threshold=3) == sh.STATUS_BROKEN
    assert sh.status_for_failures(10, threshold=3) == sh.STATUS_BROKEN


def test_status_for_failures_handles_nonpositive_threshold():
    assert sh.status_for_failures(1, threshold=0) == sh.STATUS_BROKEN
    assert sh.status_for_failures(0, threshold=0) == sh.STATUS_HEALTHY


def test_status_color_maps_to_traffic_light():
    assert sh.status_color(sh.STATUS_HEALTHY) == "green"
    assert sh.status_color(sh.STATUS_DEGRADED) == "yellow"
    assert sh.status_color(sh.STATUS_BROKEN) == "red"
    assert sh.status_color("unknown") == "gray"


# ---------------------------------------------------------------------------
# record_fetch
# ---------------------------------------------------------------------------


def test_record_fetch_success_resets_failures_and_logs():
    engine = _engine()
    session = db.get_session(engine)
    source = _add_source(session)
    sh.record_fetch(session, source, "error", NOW, error_message="boom")
    health = sh.record_fetch(session, source, "success", NOW, article_count=5)
    session.commit()

    assert health.consecutive_failures == 0
    assert health.last_error is None
    assert health.status == sh.STATUS_HEALTHY
    logs = session.query(models.SourceFetchLog).order_by(
        models.SourceFetchLog.id
    ).all()
    assert [log.status for log in logs] == ["error", "success"]
    assert logs[-1].article_count == 5
    session.close()


def test_record_fetch_failures_accumulate_to_broken():
    engine = _engine()
    session = db.get_session(engine)
    source = _add_source(session)
    for i in range(3):
        health = sh.record_fetch(
            session, source, "error", NOW, error_message=f"err-{i}",
            failure_threshold=3,
        )
    session.commit()

    assert health.consecutive_failures == 3
    assert health.status == sh.STATUS_BROKEN
    assert health.last_error == "err-2"
    session.close()


def test_record_fetch_rejects_invalid_status():
    engine = _engine()
    session = db.get_session(engine)
    source = _add_source(session)
    with pytest.raises(ValueError, match="invalid fetch status"):
        sh.record_fetch(session, source, "weird", NOW)
    session.close()


# ---------------------------------------------------------------------------
# is_source_broken
# ---------------------------------------------------------------------------


def test_is_source_broken_true_after_threshold():
    engine = _engine()
    session = db.get_session(engine)
    source = _add_source(session, name="Bad")
    for _ in range(3):
        sh.record_fetch(session, source, "error", NOW, failure_threshold=3)
    session.commit()
    assert sh.is_source_broken(session, "Bad", failure_threshold=3) is True
    session.close()


def test_is_source_broken_false_for_unknown_and_healthy_source():
    engine = _engine()
    session = db.get_session(engine)
    source = _add_source(session, name="Good")
    sh.record_fetch(session, source, "success", NOW, article_count=2)
    session.commit()
    assert sh.is_source_broken(session, "Good", failure_threshold=3) is False
    assert sh.is_source_broken(session, "NeverSeen", failure_threshold=3) is False
    session.close()


# ---------------------------------------------------------------------------
# source_report / health_metrics / dashboard
# ---------------------------------------------------------------------------


def test_source_report_metrics_and_success_rate():
    engine = _engine()
    session = db.get_session(engine)
    healthy = _add_source(session, name="Healthy")
    broken = _add_source(session, name="Broken")
    sh.record_fetch(session, healthy, "success", NOW, article_count=4)
    sh.record_fetch(session, healthy, "success", NOW, article_count=2)
    for _ in range(3):
        sh.record_fetch(session, broken, "error", NOW, error_message="down",
                        failure_threshold=3)
    session.commit()

    rows = sh.source_report(session, NOW, failure_threshold=3)
    session.close()

    by_name = {r["name"]: r for r in rows}
    assert by_name["Healthy"]["status"] == sh.STATUS_HEALTHY
    assert by_name["Healthy"]["success_rate"] == pytest.approx(1.0)
    assert by_name["Healthy"]["avg_items"] == pytest.approx(3.0)
    assert by_name["Broken"]["status"] == sh.STATUS_BROKEN
    assert by_name["Broken"]["color"] == "red"
    assert by_name["Broken"]["success_rate"] == pytest.approx(0.0)
    # Broken sources sort first.
    assert rows[0]["name"] == "Broken"

    metrics = sh.health_metrics(rows)
    assert metrics["total_sources"] == 2
    assert metrics["healthy"] == 1
    assert metrics["broken"] == 1
    assert metrics["pct_healthy"] == pytest.approx(0.5)


def test_health_metrics_empty_is_zeroed():
    metrics = sh.health_metrics([])
    assert metrics["total_sources"] == 0
    assert metrics["pct_healthy"] == 0.0
    assert metrics["pct_broken"] == 0.0
    assert metrics["stale"] == 0


def test_source_report_flags_stale_sources():
    engine = _engine()
    session = db.get_session(engine)
    fresh = _add_source(session, name="Fresh")
    stale = _add_source(session, name="Stale")
    now_naive = NOW.replace(tzinfo=None)
    session.add(
        models.Story(
            title="recent", url="https://x/fresh", source_name="Fresh", topic="ai",
            published_at=now_naive, fetched_at=now_naive, source=fresh,
        )
    )
    session.add(
        models.Story(
            title="old", url="https://x/stale", source_name="Stale", topic="ai",
            published_at=now_naive, fetched_at=now_naive - dt.timedelta(days=30),
            source=stale,
        )
    )
    session.commit()

    rows = sh.source_report(session, NOW, stale_days=7)
    session.close()
    by_name = {r["name"]: r for r in rows}
    assert by_name["Fresh"]["stale"] is False
    assert by_name["Stale"]["stale"] is True


def test_health_dashboard_returns_metrics_and_sources():
    engine = _engine()
    session = db.get_session(engine)
    source = _add_source(session, name="Solo")
    sh.record_fetch(session, source, "success", NOW, article_count=1)
    session.commit()
    session.close()

    dashboard = sh.health_dashboard(engine, now=NOW)
    assert dashboard["metrics"]["total_sources"] == 1
    assert dashboard["sources"][0]["name"] == "Solo"


# ---------------------------------------------------------------------------
# scraper integration
# ---------------------------------------------------------------------------


def test_scrape_source_records_success_health():
    engine = _engine()
    spec = scraper.SourceSpec(name="Feed", url="http://feed", kind="rss",
                              category="ai")
    scraper.scrape_source(engine, spec, _make_fetcher({"http://feed": RSS_AI}), NOW)
    session = db.get_session(engine)
    health = session.query(models.SourceHealth).one()
    assert health.status == sh.STATUS_HEALTHY
    assert health.consecutive_failures == 0
    log = session.query(models.SourceFetchLog).one()
    assert log.status == "success"
    assert log.article_count == 1
    session.close()


def test_scrape_source_records_failure_health_and_reraises():
    engine = _engine()
    spec = scraper.SourceSpec(name="Bad", url="http://bad", kind="rss",
                              category="ai")
    with pytest.raises(KeyError):
        scraper.scrape_source(engine, spec, _make_fetcher({}), NOW)
    session = db.get_session(engine)
    health = session.query(models.SourceHealth).one()
    assert health.consecutive_failures == 1
    assert health.status == sh.STATUS_DEGRADED
    assert health.last_error is not None
    log = session.query(models.SourceFetchLog).one()
    assert log.status == "error"
    session.close()


def test_run_scraper_skips_broken_sources():
    engine = _engine()
    spec = scraper.SourceSpec(name="Bad", url="http://bad", kind="rss",
                              category="ai")
    fetch = _make_fetcher({})
    # Drive the source to broken (3 consecutive failures) without skipping.
    for _ in range(3):
        scraper.run_scraper(engine, sources=[spec], fetch=fetch, now=NOW,
                            failure_threshold=3)
    # Now with skip_unhealthy the source is bypassed, no new fetch attempted.
    result = scraper.run_scraper(engine, sources=[spec], fetch=fetch, now=NOW,
                                 skip_unhealthy=True, failure_threshold=3)
    assert result.skipped_sources == ["Bad"]
    assert result.errors == 0
    session = db.get_session(engine)
    # Three error logs from the unskipped runs, none added by the skipped run.
    assert session.query(models.SourceFetchLog).count() == 3
    session.close()
