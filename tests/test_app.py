import io
import json
import re
import sqlite3
import sys
import types
import yaml
import zipfile
from datetime import datetime, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient

from xwing.app import create_app, timestamped_selection_zip_name
from xwing.config import Settings


HTML = {"Accept": "text/html"}


def bootstrap(response):
    match = re.search(
        r'<script type="application/json" id="xwing-bootstrap">(.*?)</script>',
        response.text,
        re.DOTALL,
    )
    assert match is not None
    return json.loads(match.group(1))


def editor_bootstrap(response):
    match = re.search(
        r'<script type="application/json" id="xwing-editor-bootstrap">(.*?)</script>',
        response.text,
        re.DOTALL,
    )
    assert match is not None
    return json.loads(match.group(1))


class TestDirectoryListing:
    def test_vendor_directory_contract_is_versioned_and_varies_on_accept(
        self, client, root
    ):
        (root / "docs").mkdir()
        (root / "notes.txt").write_text("hello")

        response = client.get(
            "/", headers={"Accept": "application/vnd.xwing.directory+json"}
        )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith(
            "application/vnd.xwing.directory+json"
        )
        assert response.headers["vary"] == "Accept"
        data = response.json()
        assert data["version"] == 1
        assert data["path"] == "/"
        assert [item["kind"] for item in data["files"]] == ["directory", "file"]
        assert data["files"][0]["size"] is None
        assert data["files"][1]["size"] == 5

    def test_generic_json_accept_retains_webdav_compatibility(self, client, root):
        (root / "notes.txt").write_text("hello")
        response = client.get("/", headers={"Accept": "application/json"})
        assert response.status_code == 207
        assert "multistatus" in response.text

    def test_root_listing(self, client, root):
        (root / "hello.txt").write_text("hi")
        (root / "subdir").mkdir()
        r = client.get("/", headers=HTML)
        assert r.status_code == 200
        assert "hello.txt" in r.text
        assert "subdir" in r.text

    def test_listing_has_sort_controls_and_user_scope(self, root, tmp_dir, users_yaml):
        (root / "hello.txt").write_text("hi")
        s = Settings(
            root_dir=root,
            tmp_dir=tmp_dir,
            require_auth=True,
            users_config=users_yaml,
            trusted_auth_proxies=["testclient"],
        )
        with TestClient(create_app(s)) as c:
            r = c.get("/", headers={**HTML, "X-Forwarded-User": "alice"})
        assert r.status_code == 200
        data = bootstrap(r)
        assert data["user"] == {"name": "alice", "authenticated": True}
        assert data["files"][0]["name"] == "hello.txt"
        source = (Path(__file__).parents[1] / "xwing/frontend/src/app.tsx").read_text()
        sort_source = (
            Path(__file__).parents[1] / "xwing/frontend/src/sort.ts"
        ).read_text()
        assert 'type SortKey = "name" | "size" | "modified"' in sort_source
        assert 'SORT_STORAGE_VERSION = "v2"' in source

    def test_listing_has_accessible_icon_controls_and_skip_link(self, client, root):
        (root / "notes.txt").write_text("hi")
        (root / "docs").mkdir()

        r = client.get("/", headers=HTML)

        assert r.status_code == 200
        assert 'id="xwing-root"' in r.text
        source = (Path(__file__).parents[1] / "xwing/frontend/src/app.tsx").read_text()
        assert 'className="skip-link" href="#file-list"' in source
        assert "aria-label={`Download ${file.name}`}" in source
        assert "aria-label={`Delete ${file.name}`}" in source

    def test_listing_has_selection_and_item_counts(self, client, root):
        (root / "notes.txt").write_text("hi")

        r = client.get("/", headers=HTML)

        assert r.status_code == 200
        assert len(bootstrap(r)["files"]) == 1
        source = (Path(__file__).parents[1] / "xwing/frontend/src/app.tsx").read_text()
        assert "selection-pill" in source
        assert "selected.size" in source
        assert 'className="toast-stack" aria-live="polite"' in source

    def test_trash_directory_is_hidden_from_listing(self, client, root):
        (root / ".xwing-trash").mkdir()
        (root / ".xwing-trash" / "deleted.txt").write_text("secret")
        (root / "visible.txt").write_text("hi")

        r = client.get("/", headers=HTML)

        assert r.status_code == 200
        assert "visible.txt" in r.text
        assert ".xwing-trash" not in r.text

    def test_os_metadata_files_are_hidden_from_listing(self, client, root):
        (root / ".DS_Store").write_bytes(b"junk")
        (root / "Thumbs.db").write_bytes(b"junk")
        (root / "desktop.ini").write_text("junk")
        (root / "visible.txt").write_text("hi")

        r = client.get("/", headers=HTML)

        assert r.status_code == 200
        assert "visible.txt" in r.text
        assert ".DS_Store" not in r.text
        assert "Thumbs.db" not in r.text
        assert "desktop.ini" not in r.text

    def test_empty_state_is_permission_aware_for_writable_user(self, client):
        r = client.get("/", headers=HTML)

        assert r.status_code == 200
        data = bootstrap(r)
        assert data["files"] == []
        assert data["permissions"]["write"] is True
        assert (
            "This folder is empty"
            in (Path(__file__).parents[1] / "xwing/frontend/src/app.tsx").read_text()
        )

    def test_empty_state_omits_ctas_for_read_only_user(self, root, tmp_dir, tmp_path):
        users_yaml = tmp_path / "users.yaml"
        users_yaml.write_text("users:\n  alice: r\n")
        s = Settings(
            root_dir=root,
            tmp_dir=tmp_dir,
            users_config=users_yaml,
            trusted_auth_proxies=["testclient"],
        )

        with TestClient(create_app(s)) as c:
            r = c.get("/", headers={**HTML, "X-Forwarded-User": "alice"})

        assert r.status_code == 200
        data = bootstrap(r)
        assert data["files"] == []
        assert data["permissions"]["write"] is False
        source = (Path(__file__).parents[1] / "xwing/frontend/src/app.tsx").read_text()
        assert "You have read-only access here." in source

    def test_subdir_listing(self, client, root):
        d = root / "docs"
        d.mkdir()
        (d / "readme.md").write_text("docs")
        r = client.get("/docs/", headers=HTML)
        assert r.status_code == 200
        assert "readme.md" in r.text

    def test_listing_url_encodes_special_filename_segments(self, client, root):
        (root / "hash#file?.txt").write_text("special")
        r = client.get("/", headers=HTML)
        assert r.status_code == 200
        assert bootstrap(r)["files"][0]["path"] == "/hash%23file%3F.txt"

    def test_listing_url_encodes_current_directory(self, client, root):
        d = root / "hash#dir?"
        d.mkdir()
        (d / "child#file.txt").write_text("special")
        r = client.get("/hash%23dir%3F/", headers=HTML)
        assert r.status_code == 200
        data = bootstrap(r)
        assert data["path"] == "/hash#dir?"
        assert data["files"][0]["path"] == "/hash%23dir%3F/child%23file.txt"

    def test_missing_dir_returns_404(self, client):
        r = client.get("/nonexistent/", headers=HTML)
        assert r.status_code == 404


class TestFileDownload:
    def test_download_file(self, client, root):
        (root / "data.txt").write_text("content here")
        r = client.get("/data.txt")
        assert r.status_code == 200
        assert r.text == "content here"

    def test_download_special_filename(self, client, root):
        (root / "hash#file?.txt").write_text("special")
        r = client.get("/hash%23file%3F.txt")
        assert r.status_code == 200
        assert r.text == "special"

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
        s = Settings(
            root_dir=root,
            tmp_dir=tmp_dir,
            require_auth=True,
            trusted_auth_proxies=["testclient"],
        )
        with TestClient(create_app(s)) as c:
            r = c.get("/", headers={**HTML, "X-Forwarded-User": "alice"})
        assert r.status_code == 200

    def test_authenticated_listing_uses_post_logout_form(self, root, tmp_dir):
        s = Settings(
            root_dir=root,
            tmp_dir=tmp_dir,
            require_auth=True,
            trusted_auth_proxies=["testclient"],
        )
        with TestClient(create_app(s)) as c:
            r = c.get("/", headers={**HTML, "X-Forwarded-User": "alice"})
        assert r.status_code == 200
        assert bootstrap(r)["user"] == {"name": "alice", "authenticated": True}
        source = (Path(__file__).parents[1] / "xwing/frontend/src/app.tsx").read_text()
        assert 'id="logout-form" method="post" action="/_auth/logout"' in source
        assert "authOverlay" in source
        assert 'href="/_auth/logout"' not in r.text

    def test_authenticated_editor_uses_post_logout_form(self, root, tmp_dir):
        (root / "notes.txt").write_text("hello")
        s = Settings(
            root_dir=root,
            tmp_dir=tmp_dir,
            require_auth=True,
            trusted_auth_proxies=["testclient"],
        )
        with TestClient(create_app(s)) as c:
            r = c.get("/notes.txt?edit", headers={**HTML, "X-Forwarded-User": "alice"})
        assert r.status_code == 200
        assert 'id="xwing-editor-bootstrap"' in r.text
        source = (
            Path(__file__).parents[1] / "xwing/frontend/src/editor.tsx"
        ).read_text()
        assert 'id="logout-form" method="post" action="/_auth/logout"' in source
        assert "confirmLeave" in source
        assert 'href="/_auth/logout"' not in r.text

    def test_listing_csp_uses_external_assets_only(self, client):
        r = client.get("/", headers=HTML)
        assert r.status_code == 200
        csp = r.headers["content-security-policy"]
        assert "script-src 'self'" in csp
        assert "font-src 'self'" in csp
        assert "'unsafe-inline'" not in csp

    def test_app_html_and_unversioned_assets_are_revalidated(self, client):
        listing = client.get("/", headers=HTML)
        asset = client.get("/static/assets/app.js")

        assert listing.headers["cache-control"] == "no-cache, must-revalidate"
        assert asset.headers["cache-control"] == "no-cache, must-revalidate"

    def test_editor_csp_uses_nonce_for_runtime_styles(self, client, root):
        (root / "notes.txt").write_text("hello")
        r = client.get("/notes.txt?edit")
        assert r.status_code == 200
        csp = r.headers["content-security-policy"]
        assert "style-src 'self' 'nonce-" in csp
        assert "'unsafe-inline'" not in csp
        assert "data-csp-style-nonce=" in r.text

    def test_editor_save_status_is_live_region(self, client, root):
        (root / "notes.txt").write_text("hello")
        r = client.get("/notes.txt?edit", headers=HTML)
        assert r.status_code == 200
        assert 'id="xwing-editor-bootstrap"' in r.text
        assert (
            "setStatus"
            in (Path(__file__).parents[1] / "xwing/frontend/src/editor.tsx").read_text()
        )

    def test_login_template_avoids_inline_style_attributes(self):
        template = (
            Path(__file__).parents[1] / "xwing" / "templates" / "login.html"
        ).read_text()
        assert (
            '<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">'
            in template
        )
        assert '<style nonce="{{ csrf_nonce }}">' in template
        assert (
            '<input type="hidden" name="csrf_token" value="{{ csrf_token }}">'
            in template
        )
        assert "xwing-card-in" in template
        assert "xwing-error-up" in template
        assert "xwing:login:username" in template
        assert "Signing in" in template
        assert "security-lock" in template
        assert "max-width: 380px;" in template
        assert "min-height: 40px;" in template
        assert "line-height: 1.25rem;" in template
        assert ".submit-label { min-width: 4.75rem; }" in template
        assert "appearance: none;" in template
        assert "-webkit-appearance: none;" in template
        assert ".password-field" in template
        assert "padding-right: 2.75rem;" in template
        assert 'class="password-toggle"' in template
        assert "password.type = visible ? 'text' : 'password';" in template
        assert 'input[type="password"]::-ms-reveal' in template
        assert 'input[type="password"]::-moz-reveal' in template
        assert 'style="' not in template

    def test_login_template_keeps_ldapgate_shape_with_brand_safe_deltas(self):
        template = (
            Path(__file__).parents[1] / "xwing" / "templates" / "login.html"
        ).read_text()
        assert "Secured by" in template
        assert "LDAPGate" in template
        assert "font-display: swap" in template
        assert "letter-spacing: 0;" in template
        assert "rgb(155 135 245 / .18)" in template
        assert "@media (prefers-reduced-motion: reduce)" in template
        assert "font-display: block" not in template
        assert "letter-spacing: -0.025em" not in template
        assert "#3b82f6" not in template

    def test_read_only_listing_warns_and_disables_write_controls(
        self, root, tmp_dir, tmp_path
    ):
        users_yaml = tmp_path / "users.yaml"
        users_yaml.write_text("users:\n  alice: r\n")
        s = Settings(
            root_dir=root,
            tmp_dir=tmp_dir,
            users_config=users_yaml,
            trusted_auth_proxies=["testclient"],
        )
        with TestClient(create_app(s)) as c:
            r = c.get("/", headers={**HTML, "X-Forwarded-User": "alice"})
        assert r.status_code == 200
        data = bootstrap(r)
        assert data["permissions"] == {"read": True, "write": False, "delete": False}
        source = (Path(__file__).parents[1] / "xwing/frontend/src/app.tsx").read_text()
        assert "Read-only access. Uploads and folder creation are disabled." in source
        assert "disabled={!directory.permissions.write}" in source

    def test_listing_has_bulk_selection_controls(self, client, root):
        (root / "hello.txt").write_text("hi")
        r = client.get("/", headers=HTML)
        assert r.status_code == 200
        assert bootstrap(r)["files"][0]["name"] == "hello.txt"
        source = (Path(__file__).parents[1] / "xwing/frontend/src/app.tsx").read_text()
        assert '"Select all"' in source
        assert '"Deselect all"' in source
        assert 'aria-label="Download selected as zip"' in source
        assert 'aria-label="Delete selected"' in source

    def test_require_auth_rejects_untrusted_header(self, root, tmp_dir):
        s = Settings(root_dir=root, tmp_dir=tmp_dir, require_auth=True)
        with TestClient(create_app(s)) as c:
            r = c.get("/", headers={**HTML, "X-Forwarded-User": "alice"})
        assert r.status_code == 403

    def test_no_auth_returns_anonymous(self, client, root):
        r = client.get("/", headers=HTML)
        assert r.status_code == 200
        assert "anonymous" in r.text

    def test_custom_user_header(self, root, tmp_dir):
        s = Settings(
            root_dir=root,
            tmp_dir=tmp_dir,
            user_header="X-Remote-User",
            trusted_auth_proxies=["testclient"],
        )
        with TestClient(create_app(s)) as c:
            r = c.get("/", headers={**HTML, "X-Remote-User": "bob"})
        assert r.status_code == 200
        assert "bob" in r.text

    def test_untrusted_header_ignored_when_auth_optional(self, root, tmp_dir):
        s = Settings(root_dir=root, tmp_dir=tmp_dir)
        with TestClient(create_app(s)) as c:
            r = c.get("/", headers={**HTML, "X-Forwarded-User": "alice"})
        assert r.status_code == 200
        assert "anonymous" in r.text

    def test_ldap_config_adds_ldapgate_middleware(self, root, tmp_dir, monkeypatch):
        calls = {}

        ldapgate_pkg = types.ModuleType("ldapgate")
        config_mod = types.ModuleType("ldapgate.config")
        middleware_mod = types.ModuleType("ldapgate.middleware")
        proxy_config = types.SimpleNamespace(static_paths=["/assets"], idle_timeout=900)
        loaded_config = types.SimpleNamespace(proxy=proxy_config)

        def load_config(path):
            calls["config_path"] = path
            return loaded_config

        def add_ldap_auth(app, config, template_path=None):
            calls["app"] = app
            calls["config"] = config
            calls["template_path"] = template_path

        config_mod.load_config = load_config
        middleware_mod.add_ldap_auth = add_ldap_auth
        monkeypatch.setitem(sys.modules, "ldapgate", ldapgate_pkg)
        monkeypatch.setitem(sys.modules, "ldapgate.config", config_mod)
        monkeypatch.setitem(sys.modules, "ldapgate.middleware", middleware_mod)

        ldap_yaml = tmp_dir / "ldapgate.yaml"
        ldap_yaml.write_text("ldap: {}\nproxy: {}\n")
        settings = Settings(root_dir=root, tmp_dir=tmp_dir, ldap_config=ldap_yaml)

        app = create_app(settings)

        assert calls["app"] is app
        assert calls["config_path"] == str(ldap_yaml)
        assert calls["config"] is loaded_config
        assert calls["template_path"].endswith("templates/login.html")
        assert calls["config"].proxy.static_paths == [
            "/assets",
            "/static",
            "/favicon.ico",
        ]
        assert calls["config"].proxy.session_cookie_name == "xwing_session"

    def test_ldap_config_inherits_xwing_trusted_proxies_when_unset(
        self, root, tmp_dir, monkeypatch
    ):
        calls = {}

        ldapgate_pkg = types.ModuleType("ldapgate")
        config_mod = types.ModuleType("ldapgate.config")
        middleware_mod = types.ModuleType("ldapgate.middleware")
        proxy_config = types.SimpleNamespace(static_paths=[], trusted_proxies=[])
        loaded_config = types.SimpleNamespace(proxy=proxy_config)

        def load_config(path):
            return loaded_config

        def add_ldap_auth(app, config, template_path=None):
            calls["config"] = config

        config_mod.load_config = load_config
        middleware_mod.add_ldap_auth = add_ldap_auth
        monkeypatch.setitem(sys.modules, "ldapgate", ldapgate_pkg)
        monkeypatch.setitem(sys.modules, "ldapgate.config", config_mod)
        monkeypatch.setitem(sys.modules, "ldapgate.middleware", middleware_mod)

        ldap_yaml = tmp_dir / "ldapgate.yaml"
        ldap_yaml.write_text("ldap: {}\nproxy: {}\n")
        settings = Settings(
            root_dir=root,
            tmp_dir=tmp_dir,
            ldap_config=ldap_yaml,
            trusted_auth_proxies=["127.0.0.1", "10.0.0.0/8"],
        )

        create_app(settings)

        assert calls["config"].proxy.trusted_proxies == ["127.0.0.1", "10.0.0.0/8"]

    def test_read_only_editor_script_disables_codemirror_editing(self):
        script = (
            Path(__file__).parents[1] / "xwing" / "frontend" / "src" / "editor.js"
        ).read_text()
        assert "EditorView.editable.of(false)" in script
        assert "EditorState.readOnly.of(true)" in script

    def test_editor_escape_key_navigates_to_back_link(self):
        base = Path(__file__).parents[1] / "xwing"
        template = (base / "templates" / "editor.html").read_text()
        script = (base / "frontend" / "src" / "editor.tsx").read_text()

        assert 'id="xwing-editor-root"' in template
        assert 'event.key === "Escape"' in script
        assert "requestLeave(boot.directory)" in script

    def test_editor_confirms_before_discarding_unsaved_changes(self):
        script = (
            Path(__file__).parents[1] / "xwing" / "frontend" / "src" / "editor.js"
        ).read_text()
        assert "createDialogController" in script
        assert "Discard unsaved changes?" in script
        assert "Discard changes" in script
        assert 'backLink?.addEventListener("click"' in script
        assert 'logoutForm?.addEventListener("submit"' in script
        assert "event.stopImmediatePropagation()" in script
        assert "allowNavigation" in script

    def test_upload_script_shows_waiting_and_finalizing_statuses(self):
        script = (
            Path(__file__).parents[1] / "xwing" / "frontend" / "src" / "app.js"
        ).read_text()
        assert "Preparing upload..." in script
        assert "Finalizing..." in script
        assert 'status.setAttribute("role", "status")' in script
        assert 'status.setAttribute("aria-live", "polite")' in script
        assert 'status.setAttribute("aria-atomic", "true")' in script
        assert "xwing.sort." in script
        assert "localStorage" in script
        assert 'existing.dir === "asc"' in script
        assert "currentSort.filter" in script
        assert "UPLOAD_RETRY_DELAYS_MS" in script
        assert "RETRYABLE_UPLOAD_STATUSES" in script
        assert "withUploadRetries" in script
        assert "502" in script

    def test_frontend_auth_challenges_redirect_to_ldap_login(self):
        base = Path(__file__).parents[1] / "xwing"
        app_script = (base / "frontend" / "src" / "app.tsx").read_text()
        editor_script = (base / "frontend" / "src" / "editor.tsx").read_text()
        app_bundle = (base / "static" / "assets" / "app.js").read_text()
        editor_bundle = (base / "static" / "assets" / "editor.js").read_text()

        for script in (app_script, editor_script):
            assert "/_auth/login?redirect=" in script
            assert "authentication required" in script
            assert "Session expired" in script
            assert "Signing out" in script

        for script in (app_bundle, editor_bundle):
            assert "/_auth/login?redirect=" in script
            assert "authentication required" in script
            assert "Session expired" in script
            assert "Signing out" in script
            assert "Ending your session" in script

        assert "dirty && !allowLeave.current" in editor_script

    def test_ldap_idle_timeout_is_rendered_for_frontend_timer(
        self, root, tmp_dir, monkeypatch
    ):
        ldapgate_pkg = types.ModuleType("ldapgate")
        config_mod = types.ModuleType("ldapgate.config")
        middleware_mod = types.ModuleType("ldapgate.middleware")
        proxy_config = types.SimpleNamespace(static_paths=[], idle_timeout=900)
        loaded_config = types.SimpleNamespace(proxy=proxy_config)

        config_mod.load_config = lambda path: loaded_config
        middleware_mod.add_ldap_auth = lambda app, config, template_path=None: None
        monkeypatch.setitem(sys.modules, "ldapgate", ldapgate_pkg)
        monkeypatch.setitem(sys.modules, "ldapgate.config", config_mod)
        monkeypatch.setitem(sys.modules, "ldapgate.middleware", middleware_mod)

        ldap_yaml = tmp_dir / "ldapgate.yaml"
        ldap_yaml.write_text("ldap: {}\nproxy: {}\n")
        settings = Settings(root_dir=root, tmp_dir=tmp_dir, ldap_config=ldap_yaml)

        with TestClient(create_app(settings)) as c:
            directory = c.get("/", headers=HTML)

        assert directory.status_code == 200
        assert 'data-auth-idle-timeout="900"' in directory.text

    def test_frontend_logout_submit_is_delayed_for_overlay(self):
        base = Path(__file__).parents[1] / "xwing"
        scripts = (
            (base / "frontend" / "src" / "app.tsx").read_text(),
            (base / "frontend" / "src" / "editor.tsx").read_text(),
            (base / "static" / "assets" / "app.js").read_text(),
            (base / "static" / "assets" / "editor.js").read_text(),
        )

        for script in scripts:
            assert "logout-form" in script
            assert "Signing out" in script
            assert "Ending your session" in script
            assert "setTimeout" in script
            assert ".submit()" in script

    def test_pages_use_bundled_frontend_assets(self, client, root):
        (root / "notes.txt").write_text("hello")
        listing = client.get("/", headers=HTML)
        editor = client.get("/notes.txt?edit")

        assert listing.status_code == 200
        assert editor.status_code == 200
        assert "/static/assets/style.css" in listing.text
        assert "/static/assets/app.js" in listing.text
        assert "/static/assets/style.css" in editor.text
        assert "/static/assets/editor.js" in editor.text
        assert "/static/app.js" not in listing.text
        assert "/static/editor.js" not in editor.text

    def test_frontend_prevents_document_overscroll_bounce(self):
        stylesheet = (
            Path(__file__).parents[1] / "xwing" / "frontend" / "src" / "style.css"
        ).read_text()
        assert (
            "html, body {\n  height: 100%;\n  overscroll-behavior: none;\n}"
            in stylesheet
        )


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

    def test_put_env_file_rejected(self, client, root):
        r = client.put("/.env", content=b"SECRET=hunter2")
        assert r.status_code == 403
        assert not (root / ".env").exists()

    def test_put_env_variant_rejected(self, client, root):
        r = client.put("/.env.local", content=b"SECRET=local")
        assert r.status_code == 403
        assert not (root / ".env.local").exists()

    def test_put_os_metadata_files_is_successful_noop(self, client, root):
        for path in ("/.DS_Store", "/Thumbs.db", "/desktop.ini", "/._notes.txt"):
            r = client.put(path, content=b"junk")
            assert r.status_code == 204
            assert not (root / path.lstrip("/")).exists()

    def test_put_exceeds_max_upload_bytes_returns_413(self, root, tmp_dir, users_yaml):
        s = Settings(
            root_dir=root, tmp_dir=tmp_dir, max_upload_bytes=10, users_config=users_yaml
        )
        with TestClient(create_app(s)) as c:
            r = c.put("/large.txt", content=b"x" * 100)
        assert r.status_code == 413
        assert not (root / "large.txt").exists()

    def test_put_does_not_clobber_sibling_tmp_file(self, client, root):
        (root / "newfile.txt.tmp").write_text("keep me")
        r = client.put("/newfile.txt", content=b"hello")
        assert r.status_code == 204
        assert (root / "newfile.txt").read_bytes() == b"hello"
        assert (root / "newfile.txt.tmp").read_text() == "keep me"

    def test_editor_uses_encoded_save_and_download_paths(self, client, root):
        (root / "hash#file?.txt").write_text("special")
        r = client.get("/hash%23file%3F.txt?edit")
        assert r.status_code == 200
        data = editor_bootstrap(r)
        assert data["path"] == "/hash%23file%3F.txt"
        assert data["directory"] == "/"


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

    def test_zip_rejects_archive_over_total_size_limit(self, root, tmp_dir, users_yaml):
        (root / "large.bin").write_bytes(b"x" * 11)
        settings = Settings(
            root_dir=root,
            tmp_dir=tmp_dir,
            users_config=users_yaml,
            max_upload_bytes=10,
        )
        with TestClient(create_app(settings)) as client:
            r = client.get("/?zip")
        assert r.status_code == 413
        assert r.json()["detail"] == "Archive exceeds total size limit"

    def test_zip_rejects_archive_over_entry_count_limit(
        self, root, tmp_dir, users_yaml
    ):
        (root / "a.txt").write_text("a")
        (root / "b.txt").write_text("b")
        settings = Settings(
            root_dir=root,
            tmp_dir=tmp_dir,
            users_config=users_yaml,
            max_chunks=1,
        )
        with TestClient(create_app(settings)) as client:
            r = client.get("/?zip")
        assert r.status_code == 413
        assert r.json()["detail"] == "Archive exceeds entry count limit"

    def test_zip_skips_symlink_outside_root(self, client, root, tmp_path):
        outside = tmp_path.parent / f"{tmp_path.name}-outside-secret.txt"
        outside.write_text("secret")
        (root / "leak.txt").symlink_to(outside)
        r = client.get("/?zip")
        assert r.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        assert "leak.txt" not in zf.namelist()

    def test_zip_skips_env_files_and_directories(self, client, root):
        (root / "safe.txt").write_text("safe")
        (root / ".env").write_text("SECRET=hunter2")
        (root / ".env.d").mkdir()
        (root / ".env.d" / "secret.txt").write_text("nested")
        r = client.get("/?zip")
        assert r.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        assert "safe.txt" in zf.namelist()
        assert ".env" not in zf.namelist()
        assert ".env.d/secret.txt" not in zf.namelist()

    def test_zip_skips_xwing_trash(self, client, root):
        (root / "safe.txt").write_text("safe")
        (root / ".xwing-trash").mkdir()
        (root / ".xwing-trash" / "deleted.txt").write_text("trash")
        r = client.get("/?zip")
        assert r.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        assert "safe.txt" in zf.namelist()
        assert ".xwing-trash/deleted.txt" not in zf.namelist()

    def test_zip_skips_os_metadata_files(self, client, root):
        (root / "safe.txt").write_text("safe")
        (root / ".DS_Store").write_bytes(b"junk")
        (root / "Thumbs.db").write_bytes(b"junk")
        r = client.get("/?zip")
        assert r.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        assert "safe.txt" in zf.namelist()
        assert ".DS_Store" not in zf.namelist()
        assert "Thumbs.db" not in zf.namelist()

    def test_bulk_zip_selected_files_and_folders(self, client, root):
        (root / "a.txt").write_text("hello")
        (root / "sub").mkdir()
        (root / "sub" / "b.txt").write_text("world")
        (root / "skip.txt").write_text("skip")
        r = client.post(
            "/_bulk/zip",
            json={"base": "/", "paths": ["/a.txt", "/sub/"]},
        )
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        names = zf.namelist()
        assert "a.txt" in names
        assert "sub/b.txt" in names
        assert "skip.txt" not in names

    def test_bulk_zip_uses_timestamped_download_name(self, client, root):
        (root / "a.txt").write_text("hello")
        r = client.post(
            "/_bulk/zip",
            json={"base": "/", "paths": ["/a.txt"]},
        )
        assert r.status_code == 200
        disposition = r.headers["content-disposition"]
        assert disposition.startswith("attachment; filename*=UTF-8''xwing-selection-")
        assert disposition.endswith(".zip")

    def test_timestamped_selection_zip_name_uses_utc_compact_timestamp(self):
        name = timestamped_selection_zip_name(
            datetime(2026, 6, 17, 1, 2, 3, tzinfo=timezone.utc)
        )
        assert name == "xwing-selection-20260617-010203.zip"

    def test_bulk_zip_rejects_sensitive_paths(self, client, root):
        (root / ".env").write_text("SECRET=hunter2")
        r = client.post("/_bulk/zip", json={"base": "/", "paths": ["/.env"]})
        assert r.status_code == 403

    def test_bulk_zip_rejects_traversal(self, client):
        r = client.post("/_bulk/zip", json={"base": "/", "paths": ["/../secret.txt"]})
        assert r.status_code == 403


class TestRestore:
    def test_single_delete_can_be_restored(self, client, root):
        (root / "notes.txt").write_text("hello")
        deleted = client.delete("/notes.txt")
        txid = deleted.json()["transaction_id"]
        assert not (root / "notes.txt").exists()

        restored = client.post(f"/api/restore/{txid}")

        assert restored.status_code == 200
        assert restored.json()["restored"] == 1
        assert (root / "notes.txt").read_text() == "hello"

    def test_restore_nonexistent_transaction_returns_404(self, client):
        r = client.post("/api/restore/nonexistent-id")
        assert r.status_code == 404

    def test_restore_directory(self, client, root):
        (root / "docs").mkdir()
        (root / "docs" / "readme.md").write_text("docs")
        deleted = client.delete("/docs/")
        txid = deleted.json()["transaction_id"]
        assert not (root / "docs").exists()

        restored = client.post(f"/api/restore/{txid}")

        assert restored.status_code == 200
        assert restored.json()["restored"] == 1
        assert (root / "docs" / "readme.md").read_text() == "docs"

    def test_restore_returns_paths(self, client, root):
        (root / "a.txt").write_text("a")
        deleted = client.delete("/a.txt")
        txid = deleted.json()["transaction_id"]

        restored = client.post(f"/api/restore/{txid}")

        data = restored.json()
        assert data["ok"] is True
        assert data["paths"] == ["/a.txt"]


class TestBulkDelete:
    def test_bulk_delete_selected_files_and_folders(self, client, root):
        (root / "a.txt").write_text("hello")
        (root / "sub").mkdir()
        (root / "sub" / "b.txt").write_text("world")
        (root / "keep.txt").write_text("keep")
        r = client.post("/_bulk/delete", json={"paths": ["/a.txt", "/sub/"]})
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["deleted"] == 2
        assert data["count"] == 2
        assert data["transaction_id"]
        assert not (root / "a.txt").exists()
        assert not (root / "sub").exists()
        assert (root / ".xwing-trash").is_dir()
        assert (root / "keep.txt").exists()

    def test_bulk_operations_are_audited_with_selected_paths(
        self, root, tmp_dir, users_yaml, tmp_path
    ):
        (root / "a.txt").write_text("hello")
        (root / "docs").mkdir()
        (root / "docs" / "b.txt").write_text("world")
        audit_db = tmp_path / "audit.db"
        settings = Settings(
            root_dir=root,
            tmp_dir=tmp_dir,
            users_config=users_yaml,
            trusted_auth_proxies=["testclient"],
            admin_users=["admin"],
            audit_db=audit_db,
        )
        headers = {"X-Forwarded-User": "admin"}

        with TestClient(create_app(settings)) as c:
            zipped = c.post(
                "/_bulk/zip",
                headers=headers,
                json={"base": "/", "paths": ["/a.txt", "/docs/"]},
            )
            assert zipped.status_code == 200

            deleted = c.post(
                "/_bulk/delete",
                headers=headers,
                json={"paths": ["/a.txt", "/docs/"]},
            )
            assert deleted.status_code == 200

            activity = c.get(
                "/api/admin/activity?scope=file",
                headers=headers,
            )

        assert activity.status_code == 200
        events = {event["method"]: event for event in activity.json()["events"]}
        assert set(events) == {"bulk_zip", "bulk_delete"}
        assert json.loads(events["bulk_zip"]["details"])["paths"] == [
            "/a.txt",
            "/docs",
        ]
        assert json.loads(events["bulk_delete"]["details"])["paths"] == [
            "/a.txt",
            "/docs",
        ]

    def test_bulk_delete_can_be_restored(self, client, root):
        (root / "a.txt").write_text("hello")
        (root / "sub").mkdir()
        (root / "sub" / "b.txt").write_text("world")
        deleted = client.post("/_bulk/delete", json={"paths": ["/a.txt", "/sub/"]})
        txid = deleted.json()["transaction_id"]

        restored = client.post(f"/api/restore/{txid}")

        assert restored.status_code == 200
        assert restored.json()["restored"] == 2
        assert (root / "a.txt").read_text() == "hello"
        assert (root / "sub" / "b.txt").read_text() == "world"

    def test_restore_conflict_uses_restored_suffix(self, client, root):
        (root / "a.txt").write_text("deleted")
        deleted = client.delete("/a.txt")
        txid = deleted.json()["transaction_id"]
        (root / "a.txt").write_text("replacement")

        restored = client.post(f"/api/restore/{txid}")

        assert restored.status_code == 200
        assert (root / "a.txt").read_text() == "replacement"
        assert (root / "a (restored).txt").read_text() == "deleted"

    def test_bulk_delete_rejects_root(self, client):
        r = client.post("/_bulk/delete", json={"paths": ["/"]})
        assert r.status_code == 403

    def test_bulk_delete_requires_delete_permission(self, root, tmp_dir, tmp_path):
        (root / "a.txt").write_text("hello")
        users_yaml = tmp_path / "users.yaml"
        users_yaml.write_text("users:\n  alice: r\n")
        s = Settings(
            root_dir=root,
            tmp_dir=tmp_dir,
            users_config=users_yaml,
            trusted_auth_proxies=["testclient"],
        )
        with TestClient(create_app(s)) as c:
            r = c.post(
                "/_bulk/delete",
                headers={"X-Forwarded-User": "alice"},
                json={"paths": ["/a.txt"]},
            )
        assert r.status_code == 403
        assert (root / "a.txt").exists()


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

    def test_copy_file_overwrite_failure_preserves_destination(
        self, client, root, monkeypatch
    ):
        (root / "src.txt").write_text("source")
        (root / "dst.txt").write_text("old")

        def fail_copy2(src, dst):
            raise OSError("simulated copy failure")

        import xwing.webdav

        monkeypatch.setattr(xwing.webdav.shutil, "copy2", fail_copy2)
        r = client.request(
            "COPY", "/src.txt", headers={"Destination": "/dst.txt", "Overwrite": "T"}
        )
        assert r.status_code == 500
        assert (root / "dst.txt").read_text() == "old"

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

    def test_copy_to_env_file_rejected(self, client, root):
        (root / "src.txt").write_text("source")
        r = client.request("COPY", "/src.txt", headers={"Destination": "/.env"})
        assert r.status_code == 403
        assert not (root / ".env").exists()

    def test_copy_from_env_file_rejected(self, client, root):
        (root / ".env").write_text("SECRET=hunter2")
        r = client.request("COPY", "/.env", headers={"Destination": "/leak.txt"})
        assert r.status_code == 403
        assert not (root / "leak.txt").exists()

    def test_copy_does_not_clobber_sibling_tmp_file(self, client, root):
        (root / "src.txt").write_text("source")
        (root / "dst.txt.tmp").write_text("keep me")
        r = client.request("COPY", "/src.txt", headers={"Destination": "/dst.txt"})
        assert r.status_code == 201
        assert (root / "dst.txt").read_text() == "source"
        assert (root / "dst.txt.tmp").read_text() == "keep me"

    def test_copy_directory_preserves_external_symlink(self, client, root, tmp_path):
        outside = tmp_path.parent / f"{tmp_path.name}-outside-secret.txt"
        outside.write_text("secret")
        (root / "src").mkdir()
        (root / "src" / "leak.txt").symlink_to(outside)
        r = client.request("COPY", "/src", headers={"Destination": "/dst"})
        assert r.status_code == 201
        assert (root / "dst" / "leak.txt").is_symlink()

    def test_copy_directory_overwrite_failure_restores_destination(
        self, client, root, monkeypatch
    ):
        (root / "src").mkdir()
        (root / "src" / "new.txt").write_text("new")
        (root / "dst").mkdir()
        (root / "dst" / "old.txt").write_text("old")

        original_replace = type(root / "src").replace

        def fail_final_replace(self, dest):
            if (
                self.name.startswith(".dst.")
                and self.name.endswith(".tmp")
                and Path(dest).name == "dst"
            ):
                raise OSError("simulated final replace failure")
            return original_replace(self, dest)

        def fail_move(src, dst):
            raise OSError("simulated fallback move failure")

        import xwing.webdav

        monkeypatch.setattr(type(root / "src"), "replace", fail_final_replace)
        monkeypatch.setattr(xwing.webdav.shutil, "move", fail_move)
        r = client.request(
            "COPY", "/src", headers={"Destination": "/dst", "Overwrite": "T"}
        )
        assert r.status_code == 500
        assert (root / "dst" / "old.txt").read_text() == "old"
        assert not (root / "dst" / "new.txt").exists()


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

    def test_move_file_overwrite_failure_preserves_destination(
        self, client, root, monkeypatch
    ):
        (root / "src.txt").write_text("source")
        (root / "dst.txt").write_text("old")

        def fail_replace(self, dest):
            raise OSError("simulated replace failure")

        def fail_move(src, dst):
            raise OSError("simulated move failure")

        import xwing.webdav

        monkeypatch.setattr(type(root / "src.txt"), "replace", fail_replace)
        monkeypatch.setattr(xwing.webdav.shutil, "move", fail_move)
        r = client.request(
            "MOVE", "/src.txt", headers={"Destination": "/dst.txt", "Overwrite": "T"}
        )
        assert r.status_code == 500
        assert (root / "src.txt").read_text() == "source"
        assert (root / "dst.txt").read_text() == "old"

    def test_move_root_rejected(self, client, root):
        (root / "keep.txt").write_text("keep")
        r = client.request("MOVE", "/", headers={"Destination": "/dst"})
        assert r.status_code == 403
        assert (root / "keep.txt").read_text() == "keep"
        assert not (root / "dst").exists()

    def test_move_directory_overwrite_failure_restores_destination(
        self, client, root, monkeypatch
    ):
        (root / "src").mkdir()
        (root / "src" / "new.txt").write_text("new")
        (root / "dst").mkdir()
        (root / "dst" / "old.txt").write_text("old")

        original_replace = type(root / "src").replace

        def fail_final_replace(self, dest):
            if self.name == "src" and Path(dest).name == "dst":
                raise OSError("simulated final replace failure")
            return original_replace(self, dest)

        def fail_move(src, dst):
            raise OSError("simulated fallback move failure")

        import xwing.webdav

        monkeypatch.setattr(type(root / "src"), "replace", fail_final_replace)
        monkeypatch.setattr(xwing.webdav.shutil, "move", fail_move)
        r = client.request(
            "MOVE", "/src", headers={"Destination": "/dst", "Overwrite": "T"}
        )
        assert r.status_code == 500
        assert (root / "src" / "new.txt").read_text() == "new"
        assert (root / "dst" / "old.txt").read_text() == "old"
        assert not (root / "dst" / "new.txt").exists()

    def test_move_to_env_file_rejected(self, client, root):
        (root / "src.txt").write_text("source")
        r = client.request("MOVE", "/src.txt", headers={"Destination": "/.env"})
        assert r.status_code == 403
        assert (root / "src.txt").read_text() == "source"
        assert not (root / ".env").exists()

    def test_move_from_env_file_rejected(self, client, root):
        (root / ".env").write_text("SECRET=hunter2")
        r = client.request("MOVE", "/.env", headers={"Destination": "/leak.txt"})
        assert r.status_code == 403
        assert (root / ".env").read_text() == "SECRET=hunter2"
        assert not (root / "leak.txt").exists()

    def test_move_requires_write_permission(self, root, tmp_dir, tmp_path):
        users_yaml = tmp_path / "users.yaml"
        users_yaml.write_text("users:\n  alice: rd\n")
        s = Settings(
            root_dir=root,
            tmp_dir=tmp_dir,
            require_auth=True,
            users_config=users_yaml,
            trusted_auth_proxies=["testclient"],
        )
        (root / "src.txt").write_text("source")
        with TestClient(create_app(s)) as c:
            r = c.request(
                "MOVE",
                "/src.txt",
                headers={
                    "Destination": "/dst.txt",
                    "X-Forwarded-User": "alice",
                },
            )
        assert r.status_code == 403
        assert (root / "src.txt").read_text() == "source"
        assert not (root / "dst.txt").exists()


class TestWebDavLocking:
    def test_lock_returns_finder_compatible_response(self, client, root):
        (root / "sample.txt").write_text("hello")
        r = client.request("LOCK", "/sample.txt")
        assert r.status_code == 200
        assert r.headers["dav"] == "1, 2"
        assert r.headers["lock-token"].startswith("<opaquelocktoken:")
        assert "application/xml" in r.headers["content-type"]

        xml = ET.fromstring(r.content)
        ns = {"D": "DAV:"}
        assert (
            xml.find("./D:lockdiscovery/D:activelock/D:lockscope/D:exclusive", ns)
            is not None
        )
        assert (
            xml.find("./D:lockdiscovery/D:activelock/D:locktype/D:write", ns)
            is not None
        )
        token = xml.findtext(
            "./D:lockdiscovery/D:activelock/D:locktoken/D:href", namespaces=ns
        )
        assert token is not None
        assert r.headers["lock-token"] == f"<{token}>"

    def test_unlock_succeeds_after_lock(self, client, root):
        (root / "sample.txt").write_text("hello")
        lock = client.request("LOCK", "/sample.txt")
        token = lock.headers["lock-token"]
        r = client.request("UNLOCK", "/sample.txt", headers={"Lock-Token": token})
        assert r.status_code == 204

    def test_finder_staged_save_sequence_succeeds(self, client, root):
        (root / "test").mkdir()
        lock = client.request("LOCK", "/test/sample.txt")
        assert lock.status_code == 200

        staged_path = "/test/sample.txt.sb-0f9dfb64-wbRrpW/samplte.txt"
        put = client.put(staged_path, content=b"draft")
        assert put.status_code == 204

        propfind = client.request("PROPFIND", staged_path, headers={"Depth": "0"})
        assert propfind.status_code == 207
        assert (
            root / "test" / "sample.txt.sb-0f9dfb64-wbRrpW" / "samplte.txt"
        ).read_bytes() == b"draft"


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
        assert r.status_code == 200
        assert r.json()["transaction_id"]
        assert not f.exists()
        assert (root / ".xwing-trash").is_dir()

    def test_delete_directory(self, client, root):
        d = root / "rmdir"
        d.mkdir()
        (d / "child.txt").write_text("x")
        r = client.delete("/rmdir/")
        assert r.status_code == 200
        assert r.json()["count"] == 1
        assert not d.exists()

    def test_delete_missing_returns_404(self, client):
        r = client.delete("/ghost.txt")
        assert r.status_code == 404

    def test_delete_root_rejected(self, client, root):
        (root / "keep.txt").write_text("keep")
        r = client.delete("/")
        assert r.status_code == 403
        assert (root / "keep.txt").read_text() == "keep"

    def test_delete_env_file_rejected(self, client, root):
        (root / ".env").write_text("SECRET=hunter2")
        r = client.delete("/.env")
        assert r.status_code == 403
        assert (root / ".env").read_text() == "SECRET=hunter2"


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

    def test_mkcol_env_directory_rejected(self, client, root):
        r = client.request("MKCOL", "/.env.d")
        assert r.status_code == 403
        assert not (root / ".env.d").exists()

    def test_mkcol_ignored_system_directory_is_successful_noop(self, client, root):
        r = client.request("MKCOL", "/__MACOSX/")
        assert r.status_code == 201
        assert not (root / "__MACOSX").exists()


class TestPropfind:
    def test_propfind_root(self, client, root):
        (root / "a.txt").write_text("a")
        r = client.request("PROPFIND", "/", headers={"Depth": "1"})
        assert r.status_code == 207
        assert "a.txt" in r.text
        assert "<D:href>/</D:href>" in r.text
        assert "<D:href>/./</D:href>" not in r.text

    def test_propfind_url_encodes_href_segments(self, client, root):
        (root / "hash#file?.txt").write_text("a")
        (root / "hash#dir?").mkdir()
        r = client.request("PROPFIND", "/", headers={"Depth": "1"})
        assert r.status_code == 207
        assert "<D:href>/hash%23file%3F.txt</D:href>" in r.text
        assert "<D:href>/hash%23dir%3F/</D:href>" in r.text

    def test_propfind_env_file_rejected(self, client, root):
        (root / ".env").write_text("SECRET=hunter2")
        r = client.request("PROPFIND", "/.env")
        assert r.status_code == 403

    def test_propfind_ignored_system_file_is_hidden(self, client, root):
        (root / ".DS_Store").write_bytes(b"junk")
        r = client.request("PROPFIND", "/.DS_Store")
        assert r.status_code == 404

    def test_get_nested_env_path_rejected(self, client, root):
        (root / ".env.d").mkdir()
        (root / ".env.d" / "secret.txt").write_text("SECRET=nested")
        r = client.get("/.env.d/secret.txt")
        assert r.status_code == 403

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
    def test_lock_returns_success(self, client, root):
        (root / "file.txt").write_text("x")
        r = client.request("LOCK", "/file.txt")
        assert r.status_code == 200
        assert r.headers["lock-token"].startswith("<opaquelocktoken:")


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


class TestAdminConsole:
    def _settings(self, root, tmp_dir, tmp_path, monkeypatch, *, audit_db=None):
        users = tmp_path / "users.yaml"
        users.write_text("users:\n  admin: rwd\n  alice: rwd\n")
        ldap_yaml = tmp_path / "ldapgate.yaml"
        ldap_yaml.write_text(
            "ldap:\n  bind_password: test-password\nproxy:\n  secret_key: test-secret\n"
        )
        ldapgate_pkg = types.ModuleType("ldapgate")
        config_mod = types.ModuleType("ldapgate.config")
        middleware_mod = types.ModuleType("ldapgate.middleware")
        loaded_config = types.SimpleNamespace(
            proxy=types.SimpleNamespace(
                static_paths=[],
                idle_timeout=0,
                trusted_proxies=[],
                session_cookie_name="ldapgate_session",
            )
        )
        config_mod.load_config = lambda path: loaded_config
        middleware_mod.add_ldap_auth = lambda app, config, template_path=None: None
        monkeypatch.setitem(sys.modules, "ldapgate", ldapgate_pkg)
        monkeypatch.setitem(sys.modules, "ldapgate.config", config_mod)
        monkeypatch.setitem(sys.modules, "ldapgate.middleware", middleware_mod)
        return Settings(
            root_dir=root,
            tmp_dir=tmp_dir,
            require_auth=True,
            users_config=users,
            ldap_config=ldap_yaml,
            admin_users=["admin"],
            trusted_auth_proxies=["testclient"],
            audit_db=audit_db or (tmp_path / "audit.db"),
        ), users

    def test_admin_page_and_user_management(self, root, tmp_dir, tmp_path, monkeypatch):
        settings, users = self._settings(root, tmp_dir, tmp_path, monkeypatch)
        admin_headers = {"X-Forwarded-User": "admin"}
        alice_headers = {"X-Forwarded-User": "alice"}
        with TestClient(create_app(settings)) as client:
            page = client.get("/admin", headers={**admin_headers, **HTML})
            assert page.status_code == 200
            directory = client.get("/", headers={**admin_headers, **HTML})
            assert directory.status_code == 200
            assert bootstrap(directory)["admin"] is True
            assert "admin-bootstrap" in page.text
            assert "/static/assets/admin.js" in page.text

            listed = client.get("/api/admin/users", headers=admin_headers)
            assert listed.status_code == 200
            assert {item["username"] for item in listed.json()["users"]} == {
                "admin",
                "alice",
            }
            assert set(listed.json()["users"][0]["permissions"]) == {
                "read",
                "write",
                "delete",
            }

            created = client.post(
                "/api/admin/users",
                headers=admin_headers,
                json={
                    "username": "Bob",
                    "permissions": {
                        "read": True,
                        "write": False,
                        "delete": False,
                    },
                },
            )
            assert created.status_code == 200
            assert created.json()["restart_required"] is True
            assert "bob" in {item["username"] for item in created.json()["users"]}
            assert yaml.safe_load((tmp_path / "ldapgate.yaml").read_text())["ldap"][
                "allowed_users"
            ] == ["admin", "alice", "bob"]

            forbidden = client.get("/api/admin/users", headers=alice_headers)
            assert forbidden.status_code == 403

            deleted = client.delete("/api/admin/users/bob", headers=admin_headers)
            assert deleted.status_code == 200
            assert deleted.json()["restart_required"] is True
            assert "bob" not in users.read_text()
            assert (
                "bob"
                not in yaml.safe_load((tmp_path / "ldapgate.yaml").read_text())["ldap"][
                    "allowed_users"
                ]
            )

            deleted_admin = client.delete(
                "/api/admin/users/admin", headers=admin_headers
            )
            assert deleted_admin.status_code == 200
            assert "admin:" not in users.read_text()
            assert yaml.safe_load((tmp_path / "ldapgate.yaml").read_text())["ldap"][
                "allowed_users"
            ] == ["admin", "alice"]

            deleted_alice = client.delete(
                "/api/admin/users/alice", headers=admin_headers
            )
            assert deleted_alice.status_code == 200
            assert yaml.safe_load((tmp_path / "ldapgate.yaml").read_text())["ldap"][
                "allowed_users"
            ] == ["admin"]

    def test_admin_requires_ldapgate_and_external_allowlist(
        self, root, tmp_dir, users_yaml, tmp_path
    ):
        settings = Settings(
            root_dir=root,
            tmp_dir=tmp_dir,
            require_auth=True,
            users_config=users_yaml,
            admin_users=["admin"],
            trusted_auth_proxies=[],
            audit_db=tmp_path / "audit.db",
        )
        with TestClient(create_app(settings)) as client:
            response = client.get(
                "/admin",
                headers={"X-Forwarded-User": "admin", **HTML},
            )
        assert response.status_code == 403

    def test_admin_accepts_trusted_ldapgate_proxy_identity(
        self, root, tmp_dir, users_yaml, tmp_path
    ):
        settings = Settings(
            root_dir=root,
            tmp_dir=tmp_dir,
            require_auth=True,
            users_config=users_yaml,
            admin_users=["admin"],
            trusted_auth_proxies=["testclient"],
            audit_db=tmp_path / "audit.db",
        )
        with TestClient(create_app(settings)) as client:
            response = client.get(
                "/admin",
                headers={"X-Forwarded-User": "admin", **HTML},
            )
        assert response.status_code == 200

    def test_admin_metrics_use_current_activity_window_and_purge_history(
        self, root, tmp_dir, tmp_path, monkeypatch
    ):
        settings, _ = self._settings(root, tmp_dir, tmp_path, monkeypatch)
        admin_headers = {"X-Forwarded-User": "admin"}
        alice_headers = {"X-Forwarded-User": "alice"}
        with TestClient(create_app(settings)) as client:
            created = client.put("/recent.txt", content="recent", headers=alice_headers)
            assert created.status_code == 204
            with sqlite3.connect(settings.audit_db) as db:
                db.execute(
                    "UPDATE audit_events SET occurred_at = ? WHERE path = ?",
                    ("2000-01-01T00:00:00+00:00", "/recent.txt"),
                )
                db.commit()
            metrics = client.get("/api/admin/metrics", headers=admin_headers)
            assert metrics.status_code == 200
            assert metrics.json()["active_users"] == 0
            assert "active_users_30d" not in metrics.json()

            purged = client.delete(
                "/api/admin/activity?older_than_days=1",
                headers=admin_headers,
            )
            assert purged.status_code == 200
            assert purged.json()["deleted"] == 1

    def test_admin_can_restore_and_purge_persistent_trash(
        self, root, tmp_dir, tmp_path, monkeypatch
    ):
        settings, _ = self._settings(root, tmp_dir, tmp_path, monkeypatch)
        (root / "restore.txt").write_text("recover")
        admin_headers = {"X-Forwarded-User": "admin"}
        alice_headers = {"X-Forwarded-User": "alice"}
        with TestClient(create_app(settings)) as client:
            deleted = client.delete("/restore.txt", headers=alice_headers)
            assert deleted.status_code == 200
            transaction_id = deleted.json()["transaction_id"]
            assert not (root / "restore.txt").exists()

            trash = client.get("/api/admin/trash", headers=admin_headers)
            assert trash.status_code == 200
            assert trash.json()["transactions"][0]["transaction_id"] == transaction_id

            restored = client.post(
                f"/api/admin/trash/{transaction_id}/restore",
                headers=admin_headers,
            )
            assert restored.status_code == 200
            assert (root / "restore.txt").read_text() == "recover"

            deleted_again = client.delete("/restore.txt", headers=alice_headers)
            transaction_id = deleted_again.json()["transaction_id"]
            purged = client.delete(
                f"/api/admin/trash/{transaction_id}",
                headers=admin_headers,
            )
            assert purged.status_code == 200
            records = json.loads((root / ".xwing-trash" / ".index.json").read_text())
            assert transaction_id not in {
                record["transaction_id"] for record in records
            }

    def test_trash_index_survives_app_restart(
        self, root, tmp_dir, tmp_path, monkeypatch
    ):
        settings, _ = self._settings(root, tmp_dir, tmp_path, monkeypatch)
        (root / "restart.txt").write_text("persist")
        headers = {"X-Forwarded-User": "alice"}
        admin_headers = {"X-Forwarded-User": "admin"}
        with TestClient(create_app(settings)) as client:
            deleted = client.delete("/restart.txt", headers=headers)
            transaction_id = deleted.json()["transaction_id"]

        with TestClient(create_app(settings)) as client:
            trash = client.get("/api/admin/trash", headers=admin_headers)
            assert transaction_id in {
                item["transaction_id"] for item in trash.json()["transactions"]
            }
            purged = client.delete(
                f"/api/admin/trash/{transaction_id}",
                headers=admin_headers,
            )
            assert purged.status_code == 200
