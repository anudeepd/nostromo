import time

import pytest

from xwing import upload as upload_module
from xwing.app import create_app
from xwing.config import Settings
from xwing.upload import _cleanup_once
from fastapi.testclient import TestClient


class TestUploadInit:
    def test_valid_init(self, client, root):
        r = client.post(
            "/_upload/init",
            json={"filename": "hello.txt", "total_chunks": 2, "dir": "/"},
        )
        assert r.status_code == 200
        assert "session_id" in r.json()

    def test_filename_traversal_stripped(self, client, root):
        r = client.post(
            "/_upload/init",
            json={"filename": "../../evil.txt", "total_chunks": 1, "dir": "/"},
        )
        assert r.status_code == 200
        sid = r.json()["session_id"]
        # Stored filename should be just the basename
        assert upload_module._sessions[sid]["filename"] == "evil.txt"

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

    def test_session_cleaned_up_after_complete(self, client, root):
        sid = self._init(client, "cleanup.txt", total_chunks=1)
        client.put(f"/_upload/{sid}/0", content=b"data")
        client.post(f"/_upload/{sid}/complete")
        assert sid not in upload_module._sessions

    def test_invalid_chunk_index_rejected(self, client, root):
        sid = self._init(client, "x.txt", total_chunks=2)
        r = client.put(f"/_upload/{sid}/5", content=b"bad")
        assert r.status_code == 400

    def test_unknown_session_returns_404(self, client):
        r = client.put("/_upload/deadbeef/0", content=b"data")
        assert r.status_code == 404

    def test_chunk_exceeds_max_upload_bytes(self, root, tmp_dir):
        s = Settings(root_dir=root, tmp_dir=tmp_dir, max_upload_bytes=10)
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

    def test_init_allowed_with_user_header(self, root, tmp_dir):
        s = Settings(root_dir=root, tmp_dir=tmp_dir, require_auth=True)
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
    def test_alice_cannot_write_to_bobs_session(self, root, tmp_dir):
        s = Settings(root_dir=root, tmp_dir=tmp_dir, require_auth=True)
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

    def test_alice_cannot_complete_bobs_session(self, root, tmp_dir):
        s = Settings(root_dir=root, tmp_dir=tmp_dir, require_auth=True)
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


class TestCleanupOnce:
    def test_stale_session_removed(self, settings, tmp_dir):
        sid = "stalesession"
        (tmp_dir / sid).mkdir()
        upload_module._sessions[sid] = {
            "dest_dir": str(settings.root_dir),
            "filename": "x.txt",
            "total_chunks": 1,
            "received": set(),
            "created_at": time.monotonic() - upload_module._SESSION_TTL - 1,
            "user": None,
        }
        _cleanup_once(settings)
        assert sid not in upload_module._sessions

    def test_fresh_session_kept(self, settings):
        sid = "freshsession"
        upload_module._sessions[sid] = {
            "dest_dir": str(settings.root_dir),
            "filename": "x.txt",
            "total_chunks": 1,
            "received": set(),
            "created_at": time.monotonic(),
            "user": None,
        }
        _cleanup_once(settings)
        assert sid in upload_module._sessions
