import datetime as dt

import pytest
from fastapi.testclient import TestClient

import src.api as api
import src.models as models
from src.db import get_engine, get_session, init_db

ADMIN_HEADERS = {"X-Admin-Token": api.ADMIN_TOKEN}
NOW = dt.datetime(2024, 6, 1, 12, 0, 0)


@pytest.fixture()
def api_engine(tmp_path, monkeypatch):
    eng = get_engine(f"sqlite:///{tmp_path / 'merge_api.db'}")
    init_db(eng)
    monkeypatch.setattr(api, "_engine", eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def client(api_engine):
    return TestClient(api.app)


def _add_story(session, *, title, url, source="A", votes=0):
    story = models.Story(
        title=title,
        url=url,
        source_name=source,
        topic="ai",
        vote_count=votes,
        comment_count=0,
        published_at=NOW,
        fetched_at=NOW,
    )
    session.add(story)
    session.flush()
    return story


def _two_dupes(api_engine):
    session = get_session(api_engine)
    try:
        target = _add_story(session, title="SpaceX lands a rocket today", url="https://a.com/1")
        source = _add_story(
            session, title="SpaceX lands a rocket today", url="https://b.com/2", source="B", votes=4,
        )
        session.commit()
        return target.id, source.id
    finally:
        session.close()


def test_merge_endpoint_records_acting_admin(client, api_engine):
    target_id, source_id = _two_dupes(api_engine)
    resp = client.post(
        f"/api/admin/articles/{source_id}/merge-into/{target_id}",
        headers={**ADMIN_HEADERS, "X-Admin-User": "mod-jane"},
    )
    assert resp.status_code == 200
    log = client.get("/api/admin/merges", headers=ADMIN_HEADERS).json()
    assert log[0]["merged_by"] == "mod-jane"


def test_merge_endpoint_falls_back_to_generic_admin(client, api_engine):
    target_id, source_id = _two_dupes(api_engine)
    client.post(
        f"/api/admin/articles/{source_id}/merge-into/{target_id}",
        headers=ADMIN_HEADERS,
    )
    log = client.get("/api/admin/merges", headers=ADMIN_HEADERS).json()
    assert log[0]["merged_by"] == "admin"


def test_rollback_endpoint_records_acting_admin(client, api_engine):
    target_id, source_id = _two_dupes(api_engine)
    merge = client.post(
        f"/api/admin/articles/{source_id}/merge-into/{target_id}",
        headers={**ADMIN_HEADERS, "X-Admin-User": "mod-jane"},
    ).json()
    resp = client.post(
        f"/api/admin/merges/{merge['merge_id']}/rollback",
        headers={**ADMIN_HEADERS, "X-Admin-User": "mod-bob"},
    )
    assert resp.status_code == 200
    log = client.get("/api/admin/merges", headers=ADMIN_HEADERS).json()
    assert log[0]["rolled_back_by"] == "mod-bob"


def test_merge_endpoints_require_admin_token(client, api_engine):
    target_id, source_id = _two_dupes(api_engine)
    assert client.post(
        f"/api/admin/articles/{source_id}/merge-into/{target_id}"
    ).status_code == 403
    assert client.get("/api/admin/duplicate-flags").status_code == 403


def test_duplicate_flags_endpoint_lists_queue(client, api_engine):
    target_id, source_id = _two_dupes(api_engine)
    session = get_session(api_engine)
    try:
        session.add(models.DuplicateCandidate(
            story_id=source_id, candidate_id=target_id, similarity=1.0,
            detected_at=NOW, resolved=False,
        ))
        session.commit()
    finally:
        session.close()

    resp = client.get("/api/admin/duplicate-flags", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    flags = resp.json()["flags"]
    assert len(flags) == 1
    assert flags[0]["story_id"] == source_id
    assert flags[0]["candidate_id"] == target_id
