import asyncio
import json
import time
from pathlib import Path

import pytest

from xwing.app import create_app
from xwing.config import Settings, DEFAULT_SESSION_TTL_SECONDS
from xwing.upload import (
    _cleanup_stale_async,
    _load_session,
    _save_session,
    _delete_session,
)
from fastapi.testclient import TestClient


class TestUploadInit:
    def test_valid_init(self, client, root):
        r = client.post(
            "/_upload/init",
            json={"filename": "hello.txt", "total_chunks": 2, "dir": "/"},
        )
        assert r.status_code == 200
        assert "session_id" in r.json()

    def test_filename_traversal_stripped(self, client, root, tmp_dir):
        r = client.post(
            "/_upload/init",
            json={"filename": "../../evil.txt", "total_chunks": 1, "dir": "/"},
        )
        assert r.status_code == 200
        sid = r.json()["session_id"]
        # Stored filename should be just the basename - read directly from disk
        session_file = tmp_dir / sid / "session.json"
        session = json.loads(session_file.read_text())
        assert session["filename"] == "evil.txt"

    def test_invalid_filename_empty(self, client):
        r = client.post(
            "/_upload/init", json={"filename": "/", "total_chunks": 1, "dir": "/"}
        )
        assert r.status_code == 400

    def test_total_chunks_zero_rejected(self, client):
        r = client.post(
            "/_upload/init", json={"filename": "x.txt", "total_chunks": 0, "dir": "/"}
        )
        assert r.status_code == 400

    def test_total_chunks_negative_rejected(self, client):
        r = client.post(
            "/_upload/init", json={"filename": "x.txt", "total_chunks": -1, "dir": "/"}
        )
        assert r.status_code == 400

    def test_total_chunks_over_max_rejected(self, client):
        r = client.post(
            "/_upload/init",
            json={"filename": "x.txt", "total_chunks": 10_001, "dir": "/"},
        )
        assert r.status_code == 400

    def test_dest_not_found(self, client):
        r = client.post(
            "/_upload/init",
            json={"filename": "x.txt", "total_chunks": 1, "dir": "/nonexistent"},
        )
        assert r.status_code == 404

    def test_dest_is_file_rejected(self, client, root):
        (root / "file.txt").write_text("existing")
        r = client.post(
            "/_upload/init",
            json={"filename": "x.txt", "total_chunks": 1, "dir": "/file.txt"},
        )
        assert r.status_code == 404

    def test_env_file_rejected(self, client):
        r = client.post(
            "/_upload/init",
            json={"filename": ".env", "total_chunks": 1, "dir": "/"},
        )
        assert r.status_code == 400
        assert "env" in r.json()["detail"].lower()

    def test_env_variant_rejected(self, client):
        for name in (".env.local", ".env.production", ".env."):
            r = client.post(
                "/_upload/init",
                json={"filename": name, "total_chunks": 1, "dir": "/"},
            )
            assert r.status_code == 400, f"{name} should be rejected"


class TestUploadLifecycle:
    def _init(self, client, filename="out.txt", total_chunks=2):
        r = client.post(
            "/_upload/init",
            json={"filename": filename, "total_chunks": total_chunks, "dir": "/"},
        )
        assert r.status_code == 200
        return r.json()["session_id"]

    def test_full_upload_single_chunk(self, client, root):
        sid = self._init(client, "single.txt", total_chunks=1)
        client.put(f"/_upload/{sid}/0", content=b"hello world")
        r = client.post(f"/_upload/{sid}/complete")
        assert r.status_code == 200
        assert (root / "single.txt").read_bytes() == b"hello world"

    def test_full_upload_multiple_chunks(self, client, root):
        sid = self._init(client, "multi.txt", total_chunks=3)
        client.put(f"/_upload/{sid}/0", content=b"aaa")
        client.put(f"/_upload/{sid}/1", content=b"bbb")
        client.put(f"/_upload/{sid}/2", content=b"ccc")
        r = client.post(f"/_upload/{sid}/complete")
        assert r.status_code == 200
        assert (root / "multi.txt").read_bytes() == b"aaabbbccc"

    def test_complete_with_missing_chunk_fails(self, client, root):
        sid = self._init(client, "partial.txt", total_chunks=2)
        client.put(f"/_upload/{sid}/0", content=b"only first")
        r = client.post(f"/_upload/{sid}/complete")
        assert r.status_code == 400
        assert "Missing chunks" in r.json()["detail"]

    def test_session_cleaned_up_after_complete(self, client, root, tmp_dir):
        sid = self._init(client, "cleanup.txt", total_chunks=1)
        client.put(f"/_upload/{sid}/0", content=b"data")
        r = client.post(f"/_upload/{sid}/complete")
        assert r.status_code == 200
        # Session file should be deleted
        session_file = tmp_dir / sid / "session.json"
        assert not session_file.exists()

    def test_invalid_chunk_index_rejected(self, client, root):
        sid = self._init(client, "x.txt", total_chunks=2)
        r = client.put(f"/_upload/{sid}/5", content=b"bad")
        assert r.status_code == 400

    def test_unknown_session_returns_404(self, client):
        r = client.put("/_upload/deadbeef/0", content=b"data")
        assert r.status_code == 404

    def test_chunk_exceeds_max_upload_bytes(self, root, tmp_dir, users_yaml):
        s = Settings(
            root_dir=root, tmp_dir=tmp_dir, max_upload_bytes=10, users_config=users_yaml
        )
        with TestClient(create_app(s)) as c:
            r = c.post(
                "/_upload/init",
                json={"filename": "x.txt", "total_chunks": 1, "dir": "/"},
            )
            sid = r.json()["session_id"]
            r = c.put(f"/_upload/{sid}/0", content=b"x" * 100)
        assert r.status_code == 413


class TestUploadAuth:
    def test_init_blocked_when_require_auth(self, root, tmp_dir):
        s = Settings(root_dir=root, tmp_dir=tmp_dir, require_auth=True)
        with TestClient(create_app(s)) as c:
            r = c.post(
                "/_upload/init",
                json={"filename": "x.txt", "total_chunks": 1, "dir": "/"},
            )
        assert r.status_code == 403

    def test_init_allowed_with_user_header(self, root, tmp_dir, users_yaml):
        s = Settings(
            root_dir=root, tmp_dir=tmp_dir, require_auth=True, users_config=users_yaml
        )
        with TestClient(create_app(s)) as c:
            r = c.post(
                "/_upload/init",
                json={"filename": "x.txt", "total_chunks": 1, "dir": "/"},
                headers={"X-Forwarded-User": "alice"},
            )
        assert r.status_code == 200

    def test_chunk_blocked_when_require_auth(self, root, tmp_dir):
        s = Settings(root_dir=root, tmp_dir=tmp_dir, require_auth=True)
        with TestClient(create_app(s)) as c:
            r = c.put("/_upload/fakesession/0", content=b"data")
        assert r.status_code == 403

    def test_complete_blocked_when_require_auth(self, root, tmp_dir):
        s = Settings(root_dir=root, tmp_dir=tmp_dir, require_auth=True)
        with TestClient(create_app(s)) as c:
            r = c.post("/_upload/fakesession/complete")
        assert r.status_code == 403


class TestUploadSessionIsolation:
    def test_alice_cannot_write_to_bobs_session(self, root, tmp_dir, tmp_path):
        users_yaml = tmp_path / "users.yaml"
        users_yaml.write_text("users:\n  alice:\n    read: true\n    write: true\n    delete: true\n")
        s = Settings(
            root_dir=root, tmp_dir=tmp_dir, require_auth=True, users_config=users_yaml
        )
        with TestClient(create_app(s)) as c:
            r = c.post(
                "/_upload/init",
                json={"filename": "x.txt", "total_chunks": 1, "dir": "/"},
                headers={"X-Forwarded-User": "alice"},
            )
            sid = r.json()["session_id"]
            r = c.put(
                f"/_upload/{sid}/0",
                content=b"attacker",
                headers={"X-Forwarded-User": "bob"},
            )
            assert r.status_code == 403

    def test_alice_cannot_complete_bobs_session(self, root, tmp_dir, tmp_path):
        users_yaml = tmp_path / "users.yaml"
        users_yaml.write_text("users:\n  alice:\n    read: true\n    write: true\n    delete: true\n")
        s = Settings(
            root_dir=root, tmp_dir=tmp_dir, require_auth=True, users_config=users_yaml
        )
        with TestClient(create_app(s)) as c:
            r = c.post(
                "/_upload/init",
                json={"filename": "x.txt", "total_chunks": 1, "dir": "/"},
                headers={"X-Forwarded-User": "alice"},
            )
            sid = r.json()["session_id"]
            c.put(
                f"/_upload/{sid}/0",
                content=b"data",
                headers={"X-Forwarded-User": "alice"},
            )
            r = c.post(
                f"/_upload/{sid}/complete",
                headers={"X-Forwarded-User": "bob"},
            )
            assert r.status_code == 403


class TestCleanupStale:
    @pytest.mark.asyncio
    async def test_stale_session_removed(self, settings, tmp_dir):
        sid = "stalesession"
        session_dir = tmp_dir / sid
        session_dir.mkdir()
        session_file = session_dir / "session.json"
        session_file.write_text(
            json.dumps(
                {
                    "session_id": sid,
                    "dest_dir": str(settings.root_dir),
                    "filename": "x.txt",
                    "total_chunks": 1,
                    "received": [0],
                    "created_at": time.monotonic() - DEFAULT_SESSION_TTL_SECONDS - 1,
                    "user": None,
                }
            )
        )
        await _cleanup_stale_async(settings)
        assert not session_dir.exists()

    @pytest.mark.asyncio
    async def test_fresh_session_kept(self, settings, tmp_dir):
        sid = "freshsession"
        session_dir = tmp_dir / sid
        session_dir.mkdir()
        session_file = session_dir / "session.json"
        session_file.write_text(
            json.dumps(
                {
                    "session_id": sid,
                    "dest_dir": str(settings.root_dir),
                    "filename": "x.txt",
                    "total_chunks": 1,
                    "received": [0],
                    "created_at": time.monotonic(),
                    "user": None,
                }
            )
        )
        await _cleanup_stale_async(settings)
        assert session_dir.exists()


