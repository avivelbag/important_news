import datetime as dt

from sqlalchemy import func

import src.db as db
import src.models as models

DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_STALE_DAYS = 7

STATUS_HEALTHY = "healthy"
STATUS_DEGRADED = "degraded"
STATUS_BROKEN = "broken"

_STATUS_COLORS = {
    STATUS_HEALTHY: "green",
    STATUS_DEGRADED: "yellow",
    STATUS_BROKEN: "red",
}


def status_for_failures(
    consecutive_failures: int, threshold: int = DEFAULT_FAILURE_THRESHOLD
) -> str:
    """Map a consecutive-failure count to a health status string."""
    if consecutive_failures <= 0:
        return STATUS_HEALTHY
    if consecutive_failures >= max(threshold, 1):
        return STATUS_BROKEN
    return STATUS_DEGRADED


def status_color(status: str) -> str:
    """Return the badge colour (green/yellow/red) for a status string."""
    return _STATUS_COLORS.get(status, "gray")


def get_or_create_health(session, source_id: int) -> models.SourceHealth:
    health = (
        session.query(models.SourceHealth)
        .filter_by(source_id=source_id)
        .one_or_none()
    )
    if health is None:
        health = models.SourceHealth(source_id=source_id)
        session.add(health)
        session.flush()
    return health


def record_fetch(
    session,
    source: models.Source,
    status: str,
    now: dt.datetime,
    *,
    article_count: int = 0,
    error_message: str | None = None,
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
) -> models.SourceHealth:
    """Log a fetch attempt and update the source's rolled-up health."""
    if status not in ("success", "error"):
        raise ValueError(f"invalid fetch status: {status!r}")

    session.add(
        models.SourceFetchLog(
            source_id=source.id,
            fetch_time=now,
            status=status,
            error_message=error_message,
            article_count=article_count,
        )
    )

    health = get_or_create_health(session, source.id)
    health.last_fetch_time = now
    if status == "success":
        health.consecutive_failures = 0
        health.last_error = None
    else:
        health.consecutive_failures += 1
        health.last_error = error_message
    health.status = status_for_failures(
        health.consecutive_failures, failure_threshold
    )
    session.flush()
    return health


def is_source_broken(
    session,
    source_name: str,
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
) -> bool:
    """Return True when the named source is currently in the broken state."""
    health = (
        session.query(models.SourceHealth)
        .join(models.Source, models.Source.id == models.SourceHealth.source_id)
        .filter(models.Source.name == source_name)
        .one_or_none()
    )
    if health is None:
        return False
    return status_for_failures(health.consecutive_failures, failure_threshold) == (
        STATUS_BROKEN
    )


def _source_stats(session, source_id: int) -> tuple[int, int, float]:
    total = (
        session.query(func.count(models.SourceFetchLog.id))
        .filter_by(source_id=source_id)
        .scalar()
        or 0
    )
    successes = (
        session.query(func.count(models.SourceFetchLog.id))
        .filter_by(source_id=source_id, status="success")
        .scalar()
        or 0
    )
    avg_items = (
        session.query(func.avg(models.SourceFetchLog.article_count))
        .filter_by(source_id=source_id, status="success")
        .scalar()
    )
    return total, successes, float(avg_items or 0.0)


def _last_article_time(session, source_id: int) -> dt.datetime | None:
    return (
        session.query(func.max(models.Story.fetched_at))
        .filter_by(source_id=source_id)
        .scalar()
    )


def _naive(value: dt.datetime) -> dt.datetime:
    # SQLite stores naive datetimes; drop tzinfo so tz-aware "now" compares.
    if value.tzinfo is not None:
        return value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value


def source_report(
    session,
    now: dt.datetime,
    *,
    stale_days: int = DEFAULT_STALE_DAYS,
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
) -> list[dict]:
    """Build a per-source health summary (broken first) for the dashboard."""
    now_naive = _naive(now)
    stale_cutoff = now_naive - dt.timedelta(days=stale_days)
    rows: list[dict] = []
    for source in session.query(models.Source).all():
        health = (
            session.query(models.SourceHealth)
            .filter_by(source_id=source.id)
            .one_or_none()
        )
        failures = health.consecutive_failures if health else 0
        status = status_for_failures(failures, failure_threshold)
        total, successes, avg_items = _source_stats(session, source.id)
        last_article = _last_article_time(session, source.id)
        stale = last_article is None or _naive(last_article) < stale_cutoff
        rows.append(
            {
                "source_id": source.id,
                "name": source.name,
                "status": status,
                "color": status_color(status),
                "consecutive_failures": failures,
                "last_fetch_time": (
                    health.last_fetch_time.isoformat()
                    if health and health.last_fetch_time
                    else None
                ),
                "last_error": health.last_error if health else None,
                "total_fetches": total,
                "success_rate": (successes / total) if total else 0.0,
                "avg_items": avg_items,
                "last_article_time": (
                    last_article.isoformat() if last_article else None
                ),
                "stale": stale,
            }
        )
    rows.sort(key=lambda r: (r["status"] != STATUS_BROKEN, r["name"]))
    return rows


def health_metrics(rows: list[dict]) -> dict:
    """Aggregate per-source rows into dashboard-level metrics."""
    total = len(rows)
    healthy = sum(1 for r in rows if r["status"] == STATUS_HEALTHY)
    degraded = sum(1 for r in rows if r["status"] == STATUS_DEGRADED)
    broken = sum(1 for r in rows if r["status"] == STATUS_BROKEN)
    stale = sum(1 for r in rows if r["stale"])
    return {
        "total_sources": total,
        "healthy": healthy,
        "degraded": degraded,
        "broken": broken,
        "stale": stale,
        "pct_healthy": (healthy / total) if total else 0.0,
        "pct_broken": (broken / total) if total else 0.0,
    }


def health_dashboard(
    engine,
    now: dt.datetime | None = None,
    *,
    stale_days: int = DEFAULT_STALE_DAYS,
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
) -> dict:
    """Return ``{"metrics": ..., "sources": ...}`` for the health dashboard."""
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    session = db.get_session(engine)
    try:
        rows = source_report(
            session,
            now,
            stale_days=stale_days,
            failure_threshold=failure_threshold,
        )
    finally:
        session.close()
    return {"metrics": health_metrics(rows), "sources": rows}
