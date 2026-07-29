import json
import logging

from xwing import audit_store
from xwing.app import create_app
from xwing.config import Settings

from conftest import TestClient


def test_audit_store_records_filters_and_purges(tmp_path):
    db_path = tmp_path / "audit.db"
    audit_store.init_db(db_path)
    audit_store.record_event(
        db_path=db_path,
        username="alice",
        method="PUT",
        path="/notes.txt",
        details="hello",
        status_code=204,
        duration_ms=1.5,
    )
    assert audit_store.list_events(db_path, username="alice")[0]["details"] == "hello"
    assert audit_store.purge_events(db_path, 0) == 1


def test_authenticated_text_write_is_audited_as_upload(
    root, tmp_dir, users_yaml, tmp_path
):
    db_path = tmp_path / "audit.db"
    settings = Settings(
        root_dir=root,
        tmp_dir=tmp_dir,
        users_config=users_yaml,
        require_auth=True,
        trusted_auth_proxies=["testclient"],
        audit_db=db_path,
    )
    with TestClient(create_app(settings)) as client:
        response = client.put(
            "/notes.txt",
            content="hello audit",
            headers={
                "X-Forwarded-User": "alice",
                "Content-Type": "text/plain; charset=utf-8",
            },
        )

    assert response.status_code == 204
    event = audit_store.list_events(db_path, username="alice")[0]
    assert event["method"] == "upload"
    assert event["path"] == "/notes.txt"
    assert json.loads(event["details"]) == {"bytes": len("hello audit")}


def test_authenticated_chunked_upload_audits_final_path_not_session_hash(
    root, tmp_dir, users_yaml, tmp_path
):
    db_path = tmp_path / "audit.db"
    settings = Settings(
        root_dir=root,
        tmp_dir=tmp_dir,
        users_config=users_yaml,
        require_auth=True,
        trusted_auth_proxies=["testclient"],
        audit_db=db_path,
    )
    headers = {"X-Forwarded-User": "alice"}
    with TestClient(create_app(settings)) as client:
        init = client.post(
            "/_upload/init",
            json={"filename": "final.txt", "total_chunks": 2, "dir": "/"},
            headers=headers,
        )
        assert init.status_code == 200
        session_id = init.json()["session_id"]
        assert (
            client.put(
                f"/_upload/{session_id}/0",
                content=b"hello ",
                headers=headers,
            ).status_code
            == 204
        )
        assert (
            client.put(
                f"/_upload/{session_id}/1",
                content=b"world",
                headers=headers,
            ).status_code
            == 204
        )
        response = client.post(f"/_upload/{session_id}/complete", headers=headers)

    assert response.status_code == 200
    events = audit_store.list_events(db_path, username="alice", limit=10)
    assert len(events) == 1
    event = events[0]
    assert event["method"] == "upload"
    assert event["path"] == "/final.txt"
    assert not event["path"].startswith("/_upload/")
    assert session_id not in event["path"]
    assert json.loads(event["details"]) == {"bytes": 11, "chunks": 2}


def test_authenticated_delete_is_semantically_audited(
    root, tmp_dir, users_yaml, tmp_path
):
    db_path = tmp_path / "audit.db"
    (root / "old.txt").write_text("delete me")
    settings = Settings(
        root_dir=root,
        tmp_dir=tmp_dir,
        users_config=users_yaml,
        require_auth=True,
        trusted_auth_proxies=["testclient"],
        audit_db=db_path,
    )
    with TestClient(create_app(settings)) as client:
        response = client.delete("/old.txt", headers={"X-Forwarded-User": "alice"})

    assert response.status_code == 200
    event = audit_store.list_events(db_path, username="alice")[0]
    assert event["method"] == "delete"
    assert event["path"] == "/old.txt"
    details = json.loads(event["details"])
    assert details["count"] == 1
    assert details["transaction_id"]


def test_authenticated_bulk_delete_is_semantically_audited(
    root, tmp_dir, users_yaml, tmp_path
):
    db_path = tmp_path / "audit.db"
    (root / "a.txt").write_text("a")
    (root / "b.txt").write_text("b")
    settings = Settings(
        root_dir=root,
        tmp_dir=tmp_dir,
        users_config=users_yaml,
        require_auth=True,
        trusted_auth_proxies=["testclient"],
        audit_db=db_path,
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/_bulk/delete",
            json={"paths": ["/a.txt", "/b.txt"]},
            headers={"X-Forwarded-User": "alice"},
        )

    assert response.status_code == 200
    event = audit_store.list_events(db_path, username="alice")[0]
    assert event["method"] == "bulk_delete"
    assert event["path"] == "/_bulk/delete"
    details = json.loads(event["details"])
    assert details["count"] == 2
    assert details["paths"] == ["/a.txt", "/b.txt"]
    assert details["transaction_id"]


def test_semantic_operations_are_written_to_application_log(
    root, tmp_dir, users_yaml, tmp_path, caplog
):
    (root / "old.txt").write_text("delete me")
    settings = Settings(
        root_dir=root,
        tmp_dir=tmp_dir,
        users_config=users_yaml,
        require_auth=True,
        trusted_auth_proxies=["testclient"],
        audit_db=tmp_path / "audit.db",
    )
    caplog.set_level(logging.INFO, logger="xwing.app")
    with TestClient(create_app(settings)) as client:
        response = client.delete("/old.txt", headers={"X-Forwarded-User": "alice"})

    assert response.status_code == 200
    assert "file operation user=alice operation=delete path=/old.txt" in caplog.text
    assert "status=200" in caplog.text
