import json
import time
from pathlib import Path

import pytest

from xwing.app import create_app
from xwing.config import Settings, DEFAULT_SESSION_TTL_SECONDS
from xwing.upload import _CHUNK_LOCKS, _SESSION_LOCKS, _chunk_lock, _cleanup_stale_async
from fastapi.testclient import TestClient


class TestUploadInit:
    def test_valid_init(self, client, root):
        r = client.post(
            "/_upload/init",
            json={"filename": "hello.txt", "total_chunks": 2, "dir": "/"},
        )
        assert r.status_code == 200
        assert "session_id" in r.json()

    def test_invalid_json_rejected(self, client):
        r = client.post(
            "/_upload/init",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 400

    def test_non_object_json_rejected(self, client):
        r = client.post("/_upload/init", json=["not", "an", "object"])
        assert r.status_code == 400

    def test_non_string_filename_rejected(self, client):
        r = client.post(
            "/_upload/init", json={"filename": 123, "total_chunks": 1, "dir": "/"}
        )
        assert r.status_code == 400

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

    def test_total_chunks_non_integer_rejected(self, client):
        r = client.post(
            "/_upload/init",
            json={"filename": "x.txt", "total_chunks": "nope", "dir": "/"},
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

    def test_dest_non_string_rejected(self, client):
        r = client.post(
            "/_upload/init",
            json={"filename": "x.txt", "total_chunks": 1, "dir": 123},
        )
        assert r.status_code == 400

    def test_encoded_dest_dir_is_decoded(self, client, root):
        (root / "hash#dir?").mkdir()
        r = client.post(
            "/_upload/init",
            json={"filename": "x.txt", "total_chunks": 1, "dir": "/hash%23dir%3F/"},
        )
        assert r.status_code == 200

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

    def test_os_metadata_files_are_ignored(self, client, root, tmp_dir):
        for name in (".DS_Store", "Thumbs.db", "desktop.ini", "._notes.txt"):
            r = client.post(
                "/_upload/init",
                json={"filename": name, "total_chunks": 1, "dir": "/"},
            )
            assert r.status_code == 200
            assert r.json() == {"ignored": True}
            assert not (root / name).exists()
        assert list(tmp_dir.iterdir()) == []


class TestUploadLifecycle:
    def test_chunk_lock_is_scoped_to_session_and_index(self):
        same = _chunk_lock("a" * 32, 2)
        assert _chunk_lock("a" * 32, 2) is same
        assert _chunk_lock("a" * 32, 3) is not same
        assert _chunk_lock("b" * 32, 2) is not same
        _CHUNK_LOCKS.clear()

    def _init(self, client, filename="out.txt", total_chunks=2):
        r = client.post(
            "/_upload/init",
            json={"filename": filename, "total_chunks": total_chunks, "dir": "/"},
        )
        assert r.status_code == 200
        return r.json()["session_id"]

    def _init_direct(self, client, filename="out.txt", total_chunks=2, chunk_size=3):
        r = client.post(
            "/_upload/init",
            json={
                "filename": filename,
                "total_chunks": total_chunks,
                "chunk_size": chunk_size,
                "dir": "/",
            },
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

    def test_direct_chunk_upload_writes_final_offsets(self, client, root, tmp_dir):
        sid = self._init_direct(client, "direct.txt", total_chunks=3, chunk_size=3)
        assert client.put(f"/_upload/{sid}/2", content=b"cc").status_code == 204
        assert client.put(f"/_upload/{sid}/0", content=b"aaa").status_code == 204
        assert client.put(f"/_upload/{sid}/1", content=b"bbb").status_code == 204

        session = json.loads((tmp_dir / sid / "session.json").read_text())
        temp_file = Path(session["temp_file"])
        assert temp_file.exists()
        assert not (tmp_dir / sid / "0.part").exists()

        r = client.post(f"/_upload/{sid}/complete")
        assert r.status_code == 200
        assert (root / "direct.txt").read_bytes() == b"aaabbbcc"
        assert not temp_file.exists()

    def test_direct_chunk_retry_truncates_old_tail_on_complete(self, client, root):
        sid = self._init_direct(
            client, "retry-direct.txt", total_chunks=2, chunk_size=3
        )
        assert client.put(f"/_upload/{sid}/0", content=b"aaa").status_code == 204
        assert client.put(f"/_upload/{sid}/1", content=b"bbb").status_code == 204
        assert client.put(f"/_upload/{sid}/1", content=b"c").status_code == 204

        r = client.post(f"/_upload/{sid}/complete")
        assert r.status_code == 200
        assert (root / "retry-direct.txt").read_bytes() == b"aaac"

    def test_direct_non_final_chunk_must_match_chunk_size(self, client):
        sid = self._init_direct(client, "bad-direct.txt", total_chunks=2, chunk_size=3)
        r = client.put(f"/_upload/{sid}/0", content=b"aa")
        assert r.status_code == 400
        assert "Non-final chunk" in r.json()["detail"]

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

    def test_complete_failure_preserves_existing_destination(
        self, client, root, tmp_dir
    ):
        (root / "existing.txt").write_text("keep me")
        sid = self._init(client, "existing.txt", total_chunks=1)
        client.put(f"/_upload/{sid}/0", content=b"new data")
        (tmp_dir / sid / "0.part").unlink()
        r = client.post(f"/_upload/{sid}/complete")
        assert r.status_code == 500
        assert (root / "existing.txt").read_text() == "keep me"
        assert sid not in _SESSION_LOCKS

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

    def test_chunks_cannot_exceed_total_upload_bytes(self, root, tmp_dir, users_yaml):
        s = Settings(
            root_dir=root,
            tmp_dir=tmp_dir,
            max_upload_bytes=10,
            max_chunk_bytes=10,
            users_config=users_yaml,
        )
        with TestClient(create_app(s)) as c:
            r = c.post(
                "/_upload/init",
                json={"filename": "x.txt", "total_chunks": 2, "dir": "/"},
            )
            sid = r.json()["session_id"]
            assert c.put(f"/_upload/{sid}/0", content=b"123456").status_code == 204
            r = c.put(f"/_upload/{sid}/1", content=b"abcdef")
        assert r.status_code == 413

    def test_retrying_chunk_replaces_metadata_without_double_counting(
        self, client, root, tmp_dir
    ):
        sid = self._init(client, "retry.txt", total_chunks=2)
        assert client.put(f"/_upload/{sid}/0", content=b"aa").status_code == 204
        assert client.put(f"/_upload/{sid}/1", content=b"bbb").status_code == 204
        assert client.put(f"/_upload/{sid}/0", content=b"c").status_code == 204

        session = json.loads((tmp_dir / sid / "session.json").read_text())
        assert session["total_bytes"] == 4
        assert session["chunk_bytes"] == {"0": 1, "1": 3}
        assert sorted(session["received"]) == [0, 1]

        r = client.post(f"/_upload/{sid}/complete")
        assert r.status_code == 200
        assert (root / "retry.txt").read_bytes() == b"cbbb"

    def test_invalid_session_id_rejected_before_filesystem_lookup(self, client):
        r = client.put("/_upload/not-a-session/0", content=b"data")
        assert r.status_code == 404

    def test_chunk_requires_current_write_permission(self, root, tmp_dir, tmp_path):
        users_yaml = tmp_path / "users.yaml"
        users_yaml.write_text("users:\n  alice: rw\n")
        s = Settings(
            root_dir=root,
            tmp_dir=tmp_dir,
            require_auth=True,
            users_config=users_yaml,
            trusted_auth_proxies=["testclient"],
        )
        with TestClient(create_app(s)) as c:
            r = c.post(
                "/_upload/init",
                json={"filename": "x.txt", "total_chunks": 1, "dir": "/"},
                headers={"X-Forwarded-User": "alice"},
            )
            sid = r.json()["session_id"]
            users_yaml.write_text("users:\n  alice: r\n")
            r = c.put(
                f"/_upload/{sid}/0",
                content=b"data",
                headers={"X-Forwarded-User": "alice"},
            )
        assert r.status_code == 403


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
            root_dir=root,
            tmp_dir=tmp_dir,
            require_auth=True,
            users_config=users_yaml,
            trusted_auth_proxies=["testclient"],
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
        users_yaml.write_text(
            "users:\n  alice:\n    read: true\n    write: true\n    delete: true\n"
        )
        s = Settings(
            root_dir=root,
            tmp_dir=tmp_dir,
            require_auth=True,
            users_config=users_yaml,
            trusted_auth_proxies=["testclient"],
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
        users_yaml.write_text(
            "users:\n  alice:\n    read: true\n    write: true\n    delete: true\n"
        )
        s = Settings(
            root_dir=root,
            tmp_dir=tmp_dir,
            require_auth=True,
            users_config=users_yaml,
            trusted_auth_proxies=["testclient"],
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
