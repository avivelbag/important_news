"""Tests for the /health HTML dashboard route and its template rendering."""

import datetime as dt

import pytest
from fastapi.testclient import TestClient

import src.api as api
import src.models as models
import src.source_health as sh
from src.db import get_engine, get_session, init_db

NOW = dt.datetime(2024, 6, 1, 0, 0, 0, tzinfo=dt.timezone.utc)


@pytest.fixture()
def api_engine(tmp_path, monkeypatch):
    eng = get_engine(f"sqlite:///{tmp_path / 'health.db'}")
    init_db(eng)
    monkeypatch.setattr(api, "_engine", eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def client(api_engine):
    return TestClient(api.app)


def _add_source(session, name):
    source = models.Source(name=name, url=f"http://{name}")
    session.add(source)
    session.flush()
    return source


def _seed(engine):
    session = get_session(engine)
    try:
        healthy = _add_source(session, "HealthyFeed")
        broken = _add_source(session, "BrokenFeed")
        sh.record_fetch(
            session, healthy, "success", NOW, article_count=4
        )
        for _ in range(3):
            sh.record_fetch(
                session,
                broken,
                "error",
                NOW,
                error_message="HTTP 503 service unavailable",
            )
        session.commit()
    finally:
        session.close()


def test_health_page_renders_html(client, api_engine):
    _seed(api_engine)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert "Source Health" in body
    assert "HealthyFeed" in body
    assert "BrokenFeed" in body


def test_health_page_shows_status_badges(client, api_engine):
    _seed(api_engine)
    body = client.get("/health").text
    assert "badge green" in body
    assert "badge red" in body
    assert "HTTP 503 service unavailable"[:80] in body


def test_health_page_shows_metric_counts(client, api_engine):
    _seed(api_engine)
    body = client.get("/health").text
    assert "Sources" in body
    assert "Broken" in body
    assert "Healthy" in body


def test_health_page_empty_database_renders_placeholder(client, api_engine):
    body = client.get("/health").text
    assert "No sources recorded yet." in body
    assert "badge green" not in body
    assert "badge red" not in body


def test_health_page_and_json_api_agree_on_source_count(client, api_engine):
    _seed(api_engine)
    payload = client.get("/api/sources/health").json()
    assert payload["metrics"]["total_sources"] == 2
    html = client.get("/health").text
    for source in payload["sources"]:
        assert source["name"] in html
