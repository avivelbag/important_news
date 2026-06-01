import pytest
from fastapi.testclient import TestClient

import src.api as api
from src.db import get_engine, get_session, init_db
from src.saved_searches import (
    SavedSearchError,
    create_saved_search,
    delete_saved_search,
    list_saved_searches,
)


@pytest.fixture()
def engine():
    eng = get_engine("sqlite://")
    init_db(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine):
    sess = get_session(engine)
    yield sess
    sess.close()


# --- service layer ----------------------------------------------------------


def test_create_and_list_round_trip(session):
    saved = create_saved_search(
        session, "alice", "Hot aerospace", "q=rocket&sources=hn&min_score=10"
    )
    assert saved["name"] == "Hot aerospace"
    assert saved["query_params"] == "q=rocket&sources=hn&min_score=10"
    rows = list_saved_searches(session, "alice")
    assert len(rows) == 1
    assert rows[0]["id"] == saved["id"]


def test_create_strips_leading_question_mark(session):
    saved = create_saved_search(session, "bob", "preset", "?q=ai")
    assert saved["query_params"] == "q=ai"


def test_list_is_private_per_user(session):
    create_saved_search(session, "alice", "a", "q=ai")
    create_saved_search(session, "bob", "b", "q=mars")
    assert [r["name"] for r in list_saved_searches(session, "alice")] == ["a"]
    assert [r["name"] for r in list_saved_searches(session, "bob")] == ["b"]


def test_list_empty_for_unknown_user(session):
    assert list_saved_searches(session, "nobody") == []


def test_duplicate_name_rejected(session):
    create_saved_search(session, "alice", "dup", "q=ai")
    with pytest.raises(SavedSearchError):
        create_saved_search(session, "alice", "dup", "q=mars")


def test_same_name_allowed_for_different_users(session):
    create_saved_search(session, "alice", "shared", "q=ai")
    # Not a clash: scoped per user.
    saved = create_saved_search(session, "bob", "shared", "q=mars")
    assert saved["name"] == "shared"


def test_empty_name_rejected(session):
    with pytest.raises(SavedSearchError):
        create_saved_search(session, "alice", "   ", "q=ai")


def test_empty_query_params_rejected(session):
    with pytest.raises(SavedSearchError):
        create_saved_search(session, "alice", "preset", "  ")


def test_empty_user_rejected(session):
    with pytest.raises(SavedSearchError):
        create_saved_search(session, "  ", "preset", "q=ai")


def test_delete_removes_only_owned_row(session):
    saved = create_saved_search(session, "alice", "a", "q=ai")
    delete_saved_search(session, "alice", saved["id"])
    assert list_saved_searches(session, "alice") == []


def test_delete_unknown_raises_not_found(session):
    with pytest.raises(SavedSearchError) as exc:
        delete_saved_search(session, "alice", 999)
    assert exc.value.not_found is True


def test_delete_other_users_row_is_not_found(session):
    saved = create_saved_search(session, "alice", "a", "q=ai")
    with pytest.raises(SavedSearchError) as exc:
        delete_saved_search(session, "bob", saved["id"])
    assert exc.value.not_found is True
    # Alice's preset survives the failed cross-user delete.
    assert len(list_saved_searches(session, "alice")) == 1


# --- API endpoint tests -----------------------------------------------------


@pytest.fixture()
def api_engine(tmp_path, monkeypatch):
    eng = get_engine(f"sqlite:///{tmp_path / 'api.db'}")
    init_db(eng)
    monkeypatch.setattr(api, "_engine", eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def client(api_engine):
    return TestClient(api.app)


def test_api_create_list_delete_flow(client):
    resp = client.post(
        "/api/user/saved-searches",
        json={"name": "mine", "query_params": "q=rocket&sort=recent"},
    )
    assert resp.status_code == 201
    saved = resp.json()
    assert saved["name"] == "mine"
    # The voter_id cookie set on create is reused by the TestClient.
    listed = client.get("/api/user/saved-searches")
    assert listed.status_code == 200
    assert [r["id"] for r in listed.json()] == [saved["id"]]

    deleted = client.delete(f"/api/user/saved-searches/{saved['id']}")
    assert deleted.status_code == 200
    assert client.get("/api/user/saved-searches").json() == []


def test_api_list_empty_without_cookie(client):
    resp = client.get("/api/user/saved-searches")
    assert resp.status_code == 200
    assert resp.json() == []


def test_api_create_rejects_missing_name(client):
    resp = client.post(
        "/api/user/saved-searches", json={"query_params": "q=rocket"}
    )
    assert resp.status_code == 400


def test_api_delete_without_cookie_is_404(client):
    resp = client.delete("/api/user/saved-searches/1")
    assert resp.status_code == 404
