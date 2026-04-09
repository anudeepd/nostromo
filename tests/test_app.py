import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xwing.app import create_app
from xwing.config import Settings


HTML = {"Accept": "text/html"}


class TestDirectoryListing:
    def test_root_listing(self, client, root):
        (root / "hello.txt").write_text("hi")
        (root / "subdir").mkdir()
        r = client.get("/", headers=HTML)
        assert r.status_code == 200
        assert "hello.txt" in r.text
        assert "subdir" in r.text

    def test_subdir_listing(self, client, root):
        d = root / "docs"
        d.mkdir()
        (d / "readme.md").write_text("docs")
        r = client.get("/docs/", headers=HTML)
        assert r.status_code == 200
        assert "readme.md" in r.text

    def test_missing_dir_returns_404(self, client):
        r = client.get("/nonexistent/", headers=HTML)
        assert r.status_code == 404


class TestFileDownload:
    def test_download_file(self, client, root):
        (root / "data.txt").write_text("content here")
        r = client.get("/data.txt")
        assert r.status_code == 200
        assert r.text == "content here"

    def test_missing_file_returns_404(self, client):
        r = client.get("/nope.txt")
        assert r.status_code == 404


class TestAuth:
    def test_require_auth_blocks_anonymous(self, root, tmp_dir):
        s = Settings(root_dir=root, tmp_dir=tmp_dir, require_auth=True)
        with TestClient(create_app(s)) as c:
            r = c.get("/")
        assert r.status_code == 403

    def test_require_auth_passes_with_header(self, root, tmp_dir):
        s = Settings(root_dir=root, tmp_dir=tmp_dir, require_auth=True)
        with TestClient(create_app(s)) as c:
            r = c.get("/", headers={**HTML, "X-Forwarded-User": "alice"})
        assert r.status_code == 200

    def test_no_auth_returns_anonymous(self, client, root):
        r = client.get("/", headers=HTML)
        assert r.status_code == 200
        assert "anonymous" in r.text

    def test_custom_user_header(self, root, tmp_dir):
        s = Settings(root_dir=root, tmp_dir=tmp_dir, user_header="X-Remote-User")
        with TestClient(create_app(s)) as c:
            r = c.get("/", headers={**HTML, "X-Remote-User": "bob"})
        assert r.status_code == 200
        assert "bob" in r.text


class TestPut:
    def test_put_creates_file(self, client, root):
        r = client.put("/newfile.txt", content=b"hello")
        assert r.status_code == 204
        assert (root / "newfile.txt").read_bytes() == b"hello"

    def test_put_overwrites_file(self, client, root):
        (root / "existing.txt").write_text("old")
        r = client.put("/existing.txt", content=b"new")
        assert r.status_code == 204
        assert (root / "existing.txt").read_bytes() == b"new"

    def test_put_on_directory_returns_409(self, client, root):
        (root / "adir").mkdir()
        r = client.put("/adir", content=b"data")
        assert r.status_code == 409

    def test_put_exceeds_max_upload_bytes_returns_413(self, root, tmp_dir, users_yaml):
        s = Settings(root_dir=root, tmp_dir=tmp_dir, max_upload_bytes=10, users_config=users_yaml)
        with TestClient(create_app(s)) as c:
            r = c.put("/large.txt", content=b"x" * 100)
        assert r.status_code == 413
        assert not (root / "large.txt").exists()


class TestHead:
    def test_head_on_file_returns_headers(self, client, root):
        (root / "data.txt").write_text("content")
        r = client.head("/data.txt")
        assert r.status_code == 200

    def test_head_on_dir_returns_200(self, client, root):
        (root / "subdir").mkdir()
        r = client.head("/")
        assert r.status_code == 200

    def test_head_does_not_return_body(self, client, root):
        (root / "data.txt").write_text("content")
        r = client.head("/data.txt")
        assert r.content == b""


class TestZip:
    def test_zip_directory(self, client, root):
        (root / "a.txt").write_text("hello")
        (root / "sub").mkdir()
        (root / "sub" / "b.txt").write_text("world")
        r = client.get("/?zip")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        names = zf.namelist()
        assert "a.txt" in names
        assert "sub/b.txt" in names

    def test_zip_nested_files(self, client, root):
        (root / "deep").mkdir()
        (root / "deep" / "nested.txt").write_text("deep content")
        r = client.get("/deep/?zip")
        assert r.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        assert "nested.txt" in zf.namelist()

    def test_zip_empty_directory(self, client, root):
        (root / "empty").mkdir()
        r = client.get("/empty/?zip")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        assert len(zf.namelist()) == 0


class TestCopy:
    def test_copy_file(self, client, root):
        (root / "src.txt").write_text("source")
        r = client.request("COPY", "/src.txt", headers={"Destination": "/dst.txt"})
        assert r.status_code == 201
        assert (root / "src.txt").read_text() == "source"
        assert (root / "dst.txt").read_text() == "source"

    def test_copy_file_overwrite_t(self, client, root):
        (root / "src.txt").write_text("source")
        (root / "dst.txt").write_text("old")
        r = client.request(
            "COPY", "/src.txt", headers={"Destination": "/dst.txt", "Overwrite": "T"}
        )
        assert r.status_code == 201
        assert (root / "dst.txt").read_text() == "source"

    def test_copy_file_overwrite_f_returns_412(self, client, root):
        (root / "src.txt").write_text("source")
        (root / "dst.txt").write_text("old")
        r = client.request(
            "COPY", "/src.txt", headers={"Destination": "/dst.txt", "Overwrite": "F"}
        )
        assert r.status_code == 412

    def test_copy_missing_source_returns_404(self, client):
        r = client.request("COPY", "/ghost.txt", headers={"Destination": "/dst.txt"})
        assert r.status_code == 404


class TestMove:
    def test_move_file(self, client, root):
        (root / "src.txt").write_text("source")
        r = client.request("MOVE", "/src.txt", headers={"Destination": "/dst.txt"})
        assert r.status_code == 201
        assert not (root / "src.txt").exists()
        assert (root / "dst.txt").read_text() == "source"

    def test_move_file_overwrite_f_returns_412(self, client, root):
        (root / "src.txt").write_text("source")
        (root / "dst.txt").write_text("old")
        r = client.request(
            "MOVE", "/src.txt", headers={"Destination": "/dst.txt", "Overwrite": "F"}
        )
        assert r.status_code == 412
        assert (root / "src.txt").read_text() == "source"


class TestEnvFile:
    def test_env_file_not_downloadable(self, client, root):
        (root / ".env").write_text("SECRET=hunter2")
        r = client.get("/.env")
        assert r.status_code == 403

    def test_env_file_variant_not_downloadable(self, client, root):
        (root / ".env.local").write_text("SECRET=local")
        r = client.get("/.env.local")
        assert r.status_code == 403


class TestSymlinkInsideRoot:
    def test_symlink_inside_root_allowed(self, root):
        target = root / "actual.txt"
        target.write_text("hello")
        link = root / "link.txt"
        link.symlink_to(target)
        from xwing.files import safe_path

        result = safe_path(root, "link.txt")
        assert result.resolve() == target.resolve()


class TestDelete:
    def test_delete_file(self, client, root):
        f = root / "todelete.txt"
        f.write_text("bye")
        r = client.delete("/todelete.txt")
        assert r.status_code == 204
        assert not f.exists()

    def test_delete_directory(self, client, root):
        d = root / "rmdir"
        d.mkdir()
        (d / "child.txt").write_text("x")
        r = client.delete("/rmdir/")
        assert r.status_code == 204
        assert not d.exists()

    def test_delete_missing_returns_404(self, client):
        r = client.delete("/ghost.txt")
        assert r.status_code == 404


class TestOptions:
    def test_options_returns_dav_headers(self, client):
        r = client.options("/")
        assert r.status_code == 200
        assert "DAV" in r.headers
        assert "PROPFIND" in r.headers.get("Allow", "")


class TestMkcol:
    def test_mkcol_creates_directory(self, client, root):
        r = client.request("MKCOL", "/newdir")
        assert r.status_code == 201
        assert (root / "newdir").is_dir()

    def test_mkcol_existing_returns_405(self, client, root):
        (root / "exists").mkdir()
        r = client.request("MKCOL", "/exists")
        assert r.status_code == 405

    def test_mkcol_missing_parent_returns_409(self, client, root):
        r = client.request("MKCOL", "/parent/child")
        assert r.status_code == 409


class TestPropfind:
    def test_propfind_root(self, client, root):
        (root / "a.txt").write_text("a")
        r = client.request("PROPFIND", "/", headers={"Depth": "1"})
        assert r.status_code == 207
        assert "a.txt" in r.text

    def test_propfind_depth0(self, client, root):
        r = client.request("PROPFIND", "/", headers={"Depth": "0"})
        assert r.status_code == 207

    def test_propfind_infinity_rejected(self, client):
        r = client.request("PROPFIND", "/", headers={"Depth": "infinity"})
        assert r.status_code == 403

    def test_propfind_missing_returns_404(self, client):
        r = client.request("PROPFIND", "/ghost")
        assert r.status_code == 404


class TestLock:
    def test_lock_returns_501(self, client, root):
        (root / "file.txt").write_text("x")
        r = client.request("LOCK", "/file.txt")
        assert r.status_code == 501


class TestPathTraversal:
    def test_traversal_in_url_path_normalized_by_http(self, client):
        # HTTP normalises ../../ in URLs before routing — the path arrives
        # as the normalised form, which simply won't exist under root → 404.
        r = client.get("/../../etc/passwd")
        assert r.status_code == 404

    def test_traversal_in_destination_rejected(self, client, root):
        (root / "src.txt").write_text("src")
        r = client.request(
            "COPY",
            "/src.txt",
            headers={"Destination": "http://localhost/../../etc/passwd"},
        )
        assert r.status_code == 403
