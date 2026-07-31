import asyncio
import io
import json
import logging
import os
import secrets
import shutil
import tempfile
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import anyio
import yaml
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .auth import get_user, require_perm
from . import audit_store, trash_store
from .config import Settings, UserConfig
from .files import (
    human_size,
    is_editable,
    is_ignored_system_file,
    is_within_root,
    list_dir,
    safe_path,
)
from .upload import cleanup_stale_sessions, create_upload_router
from .webdav import (
    copy_response,
    lock_response,
    mkcol_response,
    move_response,
    propfind_response,
    unlock_response,
)

logger = logging.getLogger(__name__)


def _configure_log_file_from_env() -> None:
    log_file = os.getenv("XWING_LOG_FILE")
    if not log_file:
        return
    path = Path(log_file).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(), logging.FileHandler(path, encoding="utf-8")],
    )


TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
APP_CSP = (
    "default-src 'self'; "
    "form-action 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self' data:"
)
APP_SHELL_CACHE_CONTROL = "no-cache, must-revalidate"
DIRECTORY_MEDIA_TYPE = "application/vnd.xwing.directory+json"
ACTIVE_USER_WINDOW_MINUTES = 5


class BreadcrumbPayload(BaseModel):
    name: str
    path: str


class UserPayload(BaseModel):
    name: str
    authenticated: bool


class PermissionsPayload(BaseModel):
    read: bool
    write: bool
    delete: bool


class AdminPermissionsPayload(BaseModel):
    read: bool = True
    write: bool = False
    delete: bool = False


class AdminUserPayload(BaseModel):
    username: str
    original_username: str | None = None
    permissions: AdminPermissionsPayload


class FilePayload(BaseModel):
    name: str
    path: str
    kind: str
    size: int | None
    modified: str | None
    editable: bool


class UploadPayload(BaseModel):
    chunkSize: int
    parallelDefault: int = 4


class XwingBootstrapV1(BaseModel):
    version: int = 1
    path: str
    breadcrumbs: list[BreadcrumbPayload]
    user: UserPayload
    admin: bool = False
    permissions: PermissionsPayload
    files: list[FilePayload]
    upload: UploadPayload


def _model_dict(model: BaseModel) -> dict:
    dump = getattr(model, "model_dump", None)
    return dump(mode="json") if dump else model.dict()


def timestamped_selection_zip_name(now: datetime | None = None) -> str:
    dt = now or datetime.now(timezone.utc)
    return f"xwing-selection-{dt.astimezone(timezone.utc):%Y%m%d-%H%M%S}.zip"


def build_app_csp(style_nonce: str | None = None) -> str:
    style_src = "style-src 'self'"
    if style_nonce:
        style_src += f" 'nonce-{style_nonce}'"
    return (
        "default-src 'self'; "
        "form-action 'self'; "
        "script-src 'self'; "
        f"{style_src}; "
        "img-src 'self' data:; "
        "font-src 'self' data:"
    )


def create_app_reload() -> FastAPI:
    """Factory for uvicorn reload mode — reads settings from environment variables."""
    _configure_log_file_from_env()
    root = os.environ.get("XWING_ROOT")
    if not root:
        raise RuntimeError(
            "XWING_ROOT environment variable is required for reload mode"
        )

    kwargs: dict = {"root_dir": Path(root)}
    if os.environ.get("XWING_REQUIRE_AUTH") == "True":
        kwargs["require_auth"] = True
    kwargs["listen_host"] = os.environ.get("XWING_LISTEN_HOST", "127.0.0.1")
    kwargs["listen_port"] = int(os.environ.get("XWING_LISTEN_PORT", "8989"))
    if os.environ.get("XWING_MAX_UPLOAD_GB"):
        kwargs["max_upload_bytes"] = int(
            float(os.environ["XWING_MAX_UPLOAD_GB"]) * 1024**3
        )
    if os.environ.get("XWING_MAX_CHUNK_MB"):
        kwargs["max_chunk_bytes"] = int(os.environ["XWING_MAX_CHUNK_MB"]) * 1024**2
    if os.environ.get("XWING_MAX_CHUNKS"):
        kwargs["max_chunks"] = int(os.environ["XWING_MAX_CHUNKS"])
    if os.environ.get("XWING_SESSION_TTL_MINUTES"):
        kwargs["session_ttl_seconds"] = (
            int(os.environ["XWING_SESSION_TTL_MINUTES"]) * 60
        )
    if os.environ.get("XWING_USER_HEADER"):
        kwargs["user_header"] = os.environ["XWING_USER_HEADER"]
    if os.environ.get("XWING_TRUSTED_AUTH_PROXIES"):
        kwargs["trusted_auth_proxies"] = [
            p.strip()
            for p in os.environ["XWING_TRUSTED_AUTH_PROXIES"].split(",")
            if p.strip()
        ]
    if os.environ.get("XWING_USERS_CONFIG"):
        kwargs["users_config"] = Path(os.environ["XWING_USERS_CONFIG"])
    if os.environ.get("XWING_LDAP_CONFIG"):
        kwargs["ldap_config"] = Path(os.environ["XWING_LDAP_CONFIG"])
    if os.environ.get("XWING_AUDIT_DB"):
        kwargs["audit_db"] = Path(os.environ["XWING_AUDIT_DB"])
    settings = Settings(**kwargs)
    return create_app(settings)


def create_app(settings: Settings) -> FastAPI:
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
    directory_zip_semaphore = anyio.Semaphore(1)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.audit_db:
            audit_store.init_db(settings.audit_db)
        delete_transactions.update(
            trash_store.load_transactions(
                settings.root_dir / ".xwing-trash",
                settings.root_dir,
            )
        )
        task = asyncio.create_task(cleanup_stale_sessions(settings))
        yield
        task.cancel()

    app = FastAPI(lifespan=lifespan)
    # Trash metadata survives restarts so administrators can inspect and restore
    # deleted items beyond the short browser undo window.
    delete_transactions: dict[str, dict] = {}

    def _skip_generic_audit(request: Request) -> bool:
        if request.url.path.startswith("/_auth/"):
            return True
        if request.url.path.startswith("/_upload/"):
            return True
        if request.url.path.startswith("/static/"):
            return True
        if request.url.path.startswith("/api/admin/"):
            return True
        if request.url.path == "/admin":
            return True
        if request.url.path == "/_bulk/delete":
            return True
        if request.url.path.startswith("/api/restore/"):
            return True
        return request.method.upper() in {
            "GET",
            "HEAD",
            "OPTIONS",
            "PROPFIND",
            "PUT",
            "DELETE",
            "MKCOL",
            "COPY",
            "MOVE",
        }

    async def _record_semantic_audit(
        *,
        user: str,
        operation: str,
        path: str,
        details: str | None,
        status_code: int,
        started: float,
    ) -> None:
        duration_ms = round((time.monotonic() - started) * 1000, 2)
        logger.info(
            "file operation user=%s operation=%s path=%s status=%s duration_ms=%s details=%s",
            user,
            operation,
            path,
            status_code,
            duration_ms,
            details or "",
        )
        if not settings.audit_db or user == "anonymous":
            return
        try:
            await audit_store.record_event_async(
                db_path=settings.audit_db,
                username=user,
                method=operation,
                path=path,
                details=details,
                status_code=status_code,
                duration_ms=duration_ms,
            )
        except Exception:
            logger.exception("Failed to record audit event")

    async def _audit_details(request: Request) -> str | None:
        """Capture bounded textual input; uploads remain metadata-only."""
        content_length = request.headers.get("content-length")
        if not content_length:
            return None
        try:
            if int(content_length) > 32_000:
                return None
        except ValueError:
            return None
        content_type = request.headers.get("content-type", "").lower()
        if not ("application/json" in content_type or content_type.startswith("text/")):
            return None
        body = await request.body()
        if not body or len(body) > 32_000:
            return None
        if "application/json" in content_type:
            try:
                data = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            if isinstance(data, dict):
                for key in list(data):
                    if "password" in key.lower() or "token" in key.lower():
                        data[key] = "[redacted]"
            return json.dumps(data, ensure_ascii=False)[:16_000]
        return body.decode("utf-8", errors="replace")[:16_000]

    @app.middleware("http")
    async def authenticated_activity_audit(request: Request, call_next):
        started = time.monotonic()
        details = (
            await _audit_details(request)
            if (settings.audit_db and not _skip_generic_audit(request))
            else None
        )
        response = await call_next(request)
        if not settings.audit_db or _skip_generic_audit(request):
            return response
        try:
            user = get_user(request, settings)
        except HTTPException:
            user = "anonymous"
        if user != "anonymous":
            try:
                await audit_store.record_event_async(
                    db_path=settings.audit_db,
                    username=user,
                    method=request.method,
                    path=request.url.path,
                    details=details,
                    status_code=response.status_code,
                    duration_ms=round((time.monotonic() - started) * 1000, 2),
                )
            except Exception:
                logger.exception("Failed to record audit event")
        return response

    @app.middleware("http")
    async def add_app_security_headers(request: Request, call_next):
        request.state.csp_style_nonce = (
            secrets.token_urlsafe(16) if "edit" in request.query_params else None
        )
        response = await call_next(request)
        if not request.url.path.startswith("/_auth/"):
            response.headers.setdefault(
                "Content-Security-Policy",
                build_app_csp(request.state.csp_style_nonce),
            )
        # Xwing's JS and CSS filenames are stable across releases, so they
        # must be revalidated before a normal browser reload can use them.
        if request.url.path.startswith("/static/") or response.headers.get(
            "content-type", ""
        ).startswith("text/html"):
            response.headers.setdefault("Cache-Control", APP_SHELL_CACHE_CONTROL)
        return response

    if settings.users_config:
        logger.info("Permissions loaded from %s", settings.users_config)
    else:
        logger.error(
            "No --users-config provided — all users are read-only. "
            "Pass --users-config <file> to grant write or delete access."
        )

    # LDAPGate middleware — enabled via Settings, XWING_LDAP_CONFIG, or --ldap-config
    # CLI flag. The env fallback keeps direct ASGI factory deployments simple.
    _ldap_config_path = settings.ldap_config or (
        Path(env_path) if (env_path := os.getenv("XWING_LDAP_CONFIG")) else None
    )
    ldap_idle_timeout = 0
    if _ldap_config_path:
        try:
            from ldapgate.config import load_config  # type: ignore[import]
            from ldapgate.middleware import add_ldap_auth  # type: ignore[import]
        except ImportError as e:
            raise RuntimeError(
                "ldapgate is not installed but XWING_LDAP_CONFIG is set. "
                "Install it with: pip install 'xwing[ldap]' or pip install ldapgate"
            ) from e
        ldap_config = load_config(str(_ldap_config_path))
        _ensure_ldapgate_static_paths(ldap_config)
        _ensure_ldapgate_cookie_name(ldap_config)
        _sync_ldapgate_trusted_proxies(ldap_config, settings)
        ldap_idle_timeout = int(
            getattr(getattr(ldap_config, "proxy", None), "idle_timeout", 0) or 0
        )
        _login_template = TEMPLATES_DIR / "login.html"
        add_ldap_auth(
            app,
            ldap_config,
            template_path=str(_login_template) if _login_template.exists() else None,
        )

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["human_size"] = human_size

    app.include_router(create_upload_router(settings))
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def resolve(request: Request) -> Path:
        rel = request.path_params.get("path", "")
        try:
            return safe_path(settings.root_dir, rel)
        except PermissionError:
            raise HTTPException(status_code=403, detail="Forbidden")

    def dest_from_header(request: Request, root: Path) -> Path:
        raw = request.headers.get("destination", "")
        parsed = urlparse(raw)
        if parsed.scheme:
            path_part = unquote(parsed.path) if parsed.path else None
        elif raw.startswith("//"):
            path_part = None
        else:
            path_part = unquote(raw)
        if not path_part:
            raise HTTPException(
                status_code=400, detail="Missing or invalid Destination header"
            )
        try:
            return safe_path(root, path_part)
        except PermissionError:
            raise HTTPException(status_code=403, detail="Forbidden destination")

    def _to_rel_path(fspath: Path) -> str:
        rel = fspath.relative_to(settings.root_dir)
        if rel == Path("."):
            return "/"
        return "/" + rel.as_posix()

    def _to_url_path(fspath: Path, *, trailing_slash: bool = False) -> str:
        rel = fspath.relative_to(settings.root_dir)
        if rel == Path("."):
            return "/"
        encoded = "/" + "/".join(quote(part, safe="") for part in rel.parts)
        if trailing_slash and not encoded.endswith("/"):
            encoded += "/"
        return encoded

    def _is_sensitive_path(fspath: Path) -> bool:
        try:
            rel = fspath.resolve().relative_to(settings.root_dir.resolve())
        except ValueError:
            rel = Path(fspath.name)
        return any(part == ".env" or part.startswith(".env.") for part in rel.parts)

    def _is_ignored_system_path(fspath: Path) -> bool:
        try:
            rel = fspath.resolve().relative_to(settings.root_dir.resolve())
        except ValueError:
            rel = Path(fspath.name)
        return any(is_ignored_system_file(part) for part in rel.parts)

    def _reject_sensitive_path(fspath: Path) -> None:
        if _is_sensitive_path(fspath):
            raise HTTPException(status_code=403, detail="Forbidden: sensitive file")

    def _is_root_path(fspath: Path) -> bool:
        return fspath.resolve() == settings.root_dir.resolve()

    def _trash_dir() -> Path:
        return settings.root_dir / ".xwing-trash"

    def _is_trash_path(fspath: Path) -> bool:
        try:
            fspath.resolve().relative_to(_trash_dir().resolve())
            return True
        except ValueError:
            return fspath.resolve() == _trash_dir().resolve()

    def _is_internal_path(fspath: Path) -> bool:
        resolved = fspath.resolve()
        internal_paths = [
            settings.tmp_dir.resolve(),
            *(
                path.resolve()
                for path in (
                    settings.users_config,
                    settings.ldap_config,
                    settings.audit_db,
                )
                if path is not None
            ),
        ]
        if any(resolved == path for path in internal_paths):
            return True
        try:
            resolved.relative_to(settings.tmp_dir.resolve())
            return True
        except ValueError:
            return _is_trash_path(fspath)

    def _visible_entries(fspath: Path) -> list[dict]:
        entries = []
        for entry in list_dir(fspath):
            child = (fspath / entry["name"]).resolve()
            if _is_internal_path(child):
                continue
            entries.append(entry)
        return entries

    def _top_level_paths(paths: list[Path]) -> list[Path]:
        selected = {path.resolve() for path in paths}
        result: list[Path] = []
        for path in sorted(paths, key=lambda p: len(p.parts)):
            resolved = path.resolve()
            if any(parent in selected for parent in resolved.parents):
                continue
            result.append(path)
        return result

    def _trash_name(fspath: Path, transaction_id: str, index: int) -> str:
        safe_name = fspath.name.replace("/", "_") or "item"
        return f"{int(time.time())}-{transaction_id[:8]}-{index}-{safe_name}"

    def _restore_candidate(original_path: Path, kind: str) -> Path:
        if not original_path.exists():
            return original_path
        stem = original_path.stem
        suffix = original_path.suffix
        parent = original_path.parent
        if kind == "directory" or not suffix:
            candidate = parent / f"{original_path.name} (restored)"
        else:
            candidate = parent / f"{stem} (restored){suffix}"
        if not candidate.exists():
            return candidate
        for index in range(1, 10_000):
            if kind == "directory" or not suffix:
                candidate = parent / f"{original_path.name} (restored-{index})"
            else:
                candidate = parent / f"{stem} (restored-{index}){suffix}"
            if not candidate.exists():
                return candidate
        raise HTTPException(status_code=409, detail="Could not find restore target")

    async def _soft_delete_paths(paths: list[Path], user: str) -> dict:
        txid = uuid.uuid4().hex
        trash_dir = _trash_dir()
        await anyio.to_thread.run_sync(
            lambda: trash_dir.mkdir(parents=True, exist_ok=True)
        )
        paths_to_delete = _top_level_paths(paths)
        for fspath in paths_to_delete:
            if not fspath.exists():
                raise HTTPException(status_code=404, detail="Selected path not found")
            if _is_root_path(fspath):
                raise HTTPException(status_code=403, detail="Cannot delete root")
            _reject_sensitive_path(fspath)
            if _is_internal_path(fspath):
                raise HTTPException(
                    status_code=403, detail="Cannot delete internal paths"
                )
        items = []
        try:
            for index, fspath in enumerate(paths_to_delete):
                trash_path = trash_dir / _trash_name(fspath, txid, index)
                await anyio.to_thread.run_sync(
                    shutil.move, str(fspath), str(trash_path)
                )
                items.append(
                    {
                        "original": fspath,
                        "trash": trash_path,
                        "kind": "directory" if trash_path.is_dir() else "file",
                    }
                )
            delete_transactions[txid] = {
                "user": user,
                "created": time.time(),
                "items": items,
            }
            await anyio.to_thread.run_sync(
                trash_store.save_transactions,
                trash_dir,
                settings.root_dir,
                delete_transactions,
            )
        except BaseException:
            delete_transactions.pop(txid, None)
            for item in reversed(items):
                trash_path = item["trash"]
                original_path = item["original"]
                if trash_path.exists() and not original_path.exists():
                    await anyio.to_thread.run_sync(
                        shutil.move, str(trash_path), str(original_path)
                    )
            raise
        return {"transaction_id": txid, "count": len(items), "items": items}

    # ── Method handlers ───────────────────────────────────────────────────────

    async def _handle_put(fspath: Path, request: Request, user: str) -> Response:
        started = time.monotonic()
        require_perm(user, "write", settings)
        if _is_ignored_system_path(fspath):
            return Response(status_code=204)
        _reject_sensitive_path(fspath)

        if fspath.is_dir():
            raise HTTPException(status_code=409, detail="Is a directory")

        # Check Content-Length early if provided
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > settings.max_upload_bytes:
                    raise HTTPException(
                        status_code=413, detail="Upload exceeds size limit"
                    )
            except ValueError:
                pass  # Invalid header, fall through to stream-based check

        fspath.parent.mkdir(parents=True, exist_ok=True)
        temp_handle = tempfile.NamedTemporaryFile(
            prefix=f".{fspath.name}.",
            suffix=".tmp",
            dir=fspath.parent,
            delete=False,
        )
        temp_file = Path(temp_handle.name)
        temp_handle.close()
        received_bytes = 0
        try:
            async with await anyio.open_file(temp_file, "wb") as f:
                async for chunk in request.stream():
                    received_bytes += len(chunk)
                    if received_bytes > settings.max_upload_bytes:
                        raise HTTPException(status_code=413, detail="Upload too large")
                    await f.write(chunk)
            await anyio.to_thread.run_sync(temp_file.replace, fspath)  # type: ignore[reportAttributeAccessIssue]
        except HTTPException:
            try:
                await anyio.to_thread.run_sync(temp_file.unlink)  # type: ignore[reportAttributeAccessIssue]
            except FileNotFoundError:
                pass
            raise
        except OSError as e:
            # Disk full or I/O error
            try:
                await anyio.to_thread.run_sync(temp_file.unlink)  # type: ignore[reportAttributeAccessIssue]
            except Exception:
                pass
            raise HTTPException(status_code=500, detail=f"Write failed: {e}")
        response = Response(status_code=204)
        await _record_semantic_audit(
            user=user,
            operation="upload",
            path=_to_rel_path(fspath),
            details=json.dumps({"bytes": received_bytes}, ensure_ascii=False),
            status_code=response.status_code,
            started=started,
        )
        return response

    async def _handle_delete(fspath: Path, user: str) -> Response:
        started = time.monotonic()
        require_perm(user, "delete", settings)
        if _is_root_path(fspath):
            raise HTTPException(status_code=403, detail="Cannot delete root")
        if _is_ignored_system_path(fspath):
            if fspath.exists():
                if fspath.is_dir():
                    await anyio.to_thread.run_sync(shutil.rmtree, fspath)  # type: ignore[reportAttributeAccessIssue]
                else:
                    await anyio.to_thread.run_sync(fspath.unlink)  # type: ignore[reportAttributeAccessIssue]
            return Response(status_code=204)
        _reject_sensitive_path(fspath)

        if not fspath.exists():
            raise HTTPException(status_code=404)
        rel_path = _to_rel_path(fspath)
        deleted = await _soft_delete_paths([fspath], user)
        response = JSONResponse(
            {
                "ok": True,
                "transaction_id": deleted["transaction_id"],
                "count": deleted["count"],
            }
        )
        await _record_semantic_audit(
            user=user,
            operation="delete",
            path=rel_path,
            details=json.dumps(
                {
                    "count": deleted["count"],
                    "transaction_id": deleted["transaction_id"],
                },
                ensure_ascii=False,
            ),
            status_code=response.status_code,
            started=started,
        )
        return response

    async def _handle_get(fspath: Path, request: Request, user: str) -> Response:
        if not fspath.exists():
            raise HTTPException(status_code=404)

        if _is_ignored_system_path(fspath):
            raise HTTPException(status_code=404)

        _reject_sensitive_path(fspath)

        if fspath.is_dir():
            return await _handle_get_dir(fspath, request, user)

        if "edit" in request.query_params and is_editable(fspath):
            return await _handle_edit(fspath, request, user)

        response = FileResponse(fspath)
        if request.method.upper() == "GET":
            await _record_semantic_audit(
                user=user,
                operation="download",
                path=_to_rel_path(fspath),
                details=None,
                status_code=response.status_code,
                started=time.monotonic(),
            )
        return response

    async def _handle_get_dir(fspath: Path, request: Request, user: str) -> Response:
        rel_path = _to_rel_path(fspath)
        url_path = _to_url_path(fspath, trailing_slash=True)

        if "zip" in request.query_params:
            return await _zip_response(fspath, settings.root_dir)

        accept = request.headers.get("accept", "")
        accepted_types = {
            item.split(";", 1)[0].strip().lower()
            for item in accept.split(",")
            if item.strip()
        }
        wants_directory_json = DIRECTORY_MEDIA_TYPE in accepted_types
        if (
            request.method == "GET"
            and not wants_directory_json
            and "text/html" not in accepted_types
            and "*/*" not in accepted_types
        ):
            return propfind_response(request, fspath, settings.root_dir)

        try:
            entries = await anyio.to_thread.run_sync(_visible_entries, fspath)
        except PermissionError:
            raise HTTPException(status_code=403, detail="Permission denied")

        parts = [p for p in rel_path.strip("/").split("/") if p]
        breadcrumbs = [{"name": "Home", "path": "/", "url_path": "/"}]
        cumulative_parts = []
        for part in parts:
            cumulative_parts.append(part)
            encoded = "/" + "/".join(quote(p, safe="") for p in cumulative_parts) + "/"
            breadcrumbs.append({"name": part, "path": encoded, "url_path": encoded})

        perms = settings.perms_for(user)
        normalized_path = "/" + rel_path.strip("/") if rel_path.strip("/") else "/"
        payload = XwingBootstrapV1(
            path=normalized_path,
            breadcrumbs=[
                BreadcrumbPayload(name=crumb["name"], path=crumb["url_path"])
                for crumb in breadcrumbs
            ],
            user=UserPayload(name=user, authenticated=user != "anonymous"),
            admin=settings.is_admin_user(user),
            permissions=PermissionsPayload(
                read=perms.read,
                write=perms.write,
                delete=perms.delete,
            ),
            files=[
                FilePayload(
                    name=entry["name"],
                    path=(
                        url_path + entry["url_name"] + ("/" if entry["is_dir"] else "")
                    ),
                    kind="directory" if entry["is_dir"] else "file",
                    size=None if entry["is_dir"] else entry["size"],
                    modified=datetime.fromtimestamp(
                        entry["mtime"], tz=timezone.utc
                    ).isoformat()
                    if entry["mtime"]
                    else None,
                    editable=entry["editable"],
                )
                for entry in entries
            ],
            upload=UploadPayload(chunkSize=settings.max_chunk_bytes),
        )
        payload_dict = _model_dict(payload)

        if wants_directory_json:
            response = JSONResponse(payload_dict, media_type=DIRECTORY_MEDIA_TYPE)
            response.headers["Vary"] = "Accept"
            return response

        response = templates.TemplateResponse(
            request,
            "index.html",
            {
                "entries": entries,
                "entry_count": len(entries),
                "current_path": rel_path if rel_path.endswith("/") else rel_path + "/",
                "current_url_path": url_path,
                "breadcrumbs": breadcrumbs,
                "user": user,
                "perms": perms,
                "max_chunk_bytes": settings.max_chunk_bytes,
                "auth_idle_timeout": ldap_idle_timeout,
                "bootstrap": payload_dict,
            },
        )
        response.headers["Vary"] = "Accept"
        return response

    async def _handle_edit(fspath: Path, request: Request, user: str) -> Response:
        rel_path = _to_rel_path(fspath)
        url_path = _to_url_path(fspath)
        content = await anyio.Path(fspath).read_text(encoding="utf-8", errors="replace")
        dir_path = rel_path.rsplit("/", 1)[0] + "/"
        if dir_path == "//":
            dir_path = "/"
        dir_url_path = _to_url_path(fspath.parent, trailing_slash=True)
        perms = settings.perms_for(user)
        editor_bootstrap = {
            "path": url_path,
            "directory": dir_url_path,
            "filename": fspath.name,
            "displayPath": rel_path,
            "extension": fspath.suffix.lstrip(".").lower(),
            "content": content,
            "user": {"name": user, "authenticated": user != "anonymous"},
            "canWrite": perms.write,
            "cspNonce": request.state.csp_style_nonce or "",
            "authIdleTimeout": ldap_idle_timeout,
        }
        return templates.TemplateResponse(
            request,
            "editor.html",
            {
                "file_path": rel_path,
                "file_url_path": url_path,
                "dir_path": dir_path,
                "dir_url_path": dir_url_path,
                "filename": fspath.name,
                "content": content,
                "ext": fspath.suffix.lstrip(".").lower(),
                "user": user,
                "perms": perms,
                "csp_style_nonce": request.state.csp_style_nonce,
                "auth_idle_timeout": ldap_idle_timeout,
                "editor_bootstrap": editor_bootstrap,
            },
        )

    async def _zip_response(fspath: Path, root: Path) -> Response:
        zip_name = (fspath.name or "archive") + ".zip"

        def _collect_files() -> list[Path]:
            files: list[Path] = []
            total_bytes = 0
            visited_entries = 0
            for child in fspath.rglob("*"):
                visited_entries += 1
                if visited_entries > settings.max_chunks:
                    raise HTTPException(
                        status_code=413,
                        detail="Archive exceeds entry count limit",
                    )
                if (
                    child.is_file()
                    and is_within_root(root, child)
                    and not _is_sensitive_path(child)
                    and not _is_internal_path(child)
                    and not _is_ignored_system_path(child)
                ):
                    total_bytes += child.stat().st_size
                    if total_bytes > settings.max_upload_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail="Archive exceeds total size limit",
                        )
                    files.append(child)
            return sorted(files)

        await directory_zip_semaphore.acquire()
        try:
            files = await anyio.to_thread.run_sync(_collect_files)  # type: ignore[reportAttributeAccessIssue]
            archive = tempfile.TemporaryFile(dir=settings.tmp_dir)

            def _build() -> None:
                total_bytes = 0
                with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
                    for child in files:
                        info = zipfile.ZipInfo.from_file(
                            child, child.relative_to(fspath).as_posix()
                        )
                        info.compress_type = zipfile.ZIP_DEFLATED
                        with child.open("rb") as source, zf.open(info, "w") as target:
                            while chunk := source.read(settings.chunk_read_size):
                                total_bytes += len(chunk)
                                if total_bytes > settings.max_upload_bytes:
                                    raise HTTPException(
                                        status_code=413,
                                        detail="Archive exceeds total size limit",
                                    )
                                target.write(chunk)
                archive.seek(0)

            await anyio.to_thread.run_sync(_build)  # type: ignore[reportAttributeAccessIssue]
        except BaseException:
            if "archive" in locals():
                archive.close()
            directory_zip_semaphore.release()
            raise

        async def _stream():
            try:
                while chunk := await anyio.to_thread.run_sync(archive.read, 64 * 1024):  # type: ignore[reportAttributeAccessIssue]
                    yield chunk
            finally:
                archive.close()
                directory_zip_semaphore.release()

        return StreamingResponse(
            _stream(),
            media_type="application/zip",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(zip_name)}"
            },
        )

    async def _bulk_body(request: Request) -> dict:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON") from None
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="JSON body must be an object")
        return body

    def _resolve_bulk_paths(raw_paths: object) -> list[Path]:
        if not isinstance(raw_paths, list):
            raise HTTPException(status_code=400, detail="paths must be a list")
        if not raw_paths:
            raise HTTPException(status_code=400, detail="paths must not be empty")
        if len(raw_paths) > 500:
            raise HTTPException(status_code=400, detail="Too many paths")

        paths: list[Path] = []
        seen: set[Path] = set()
        for raw in raw_paths:
            if not isinstance(raw, str):
                raise HTTPException(
                    status_code=400, detail="paths entries must be strings"
                )
            try:
                fspath = safe_path(settings.root_dir, unquote(raw))
            except PermissionError:
                raise HTTPException(status_code=403, detail="Forbidden path") from None
            if _is_root_path(fspath):
                raise HTTPException(status_code=403, detail="Cannot select root")
            _reject_sensitive_path(fspath)
            if _is_ignored_system_path(fspath):
                continue
            if fspath not in seen:
                seen.add(fspath)
                paths.append(fspath)
        if not paths:
            raise HTTPException(status_code=400, detail="No selectable paths")
        return paths

    def _zip_selected(paths: list[Path], base_path: Path) -> bytes:
        buf = io.BytesIO()
        written: set[str] = set()

        def arcname(path: Path) -> Path:
            try:
                return path.relative_to(base_path)
            except ValueError:
                return path.relative_to(settings.root_dir)

        def add_file(zf: zipfile.ZipFile, file_path: Path) -> None:
            if (
                not file_path.is_file()
                or not is_within_root(settings.root_dir, file_path)
                or _is_sensitive_path(file_path)
                or _is_internal_path(file_path)
                or _is_ignored_system_path(file_path)
            ):
                return
            name = arcname(file_path).as_posix()
            if name in written:
                return
            written.add(name)
            zf.write(file_path, name)

        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(paths, key=lambda p: p.as_posix()):
                if not path.exists():
                    raise FileNotFoundError(path)
                if path.is_dir():
                    for child in sorted(path.rglob("*")):
                        add_file(zf, child)
                else:
                    add_file(zf, path)
        return buf.getvalue()

    @app.post("/_bulk/zip", include_in_schema=False)
    async def bulk_zip(request: Request):
        user = get_user(request, settings)
        require_perm(user, "read", settings)
        body = await _bulk_body(request)
        paths = _resolve_bulk_paths(body.get("paths"))
        base_raw = body.get("base", "/")
        if not isinstance(base_raw, str):
            raise HTTPException(status_code=400, detail="base must be a string")
        try:
            base_path = safe_path(settings.root_dir, unquote(base_raw))
        except PermissionError:
            raise HTTPException(status_code=403, detail="Forbidden base") from None
        if not base_path.exists() or not base_path.is_dir():
            raise HTTPException(status_code=404, detail="Base directory not found")

        try:
            zip_bytes = await anyio.to_thread.run_sync(_zip_selected, paths, base_path)  # type: ignore[reportAttributeAccessIssue]
        except FileNotFoundError:
            raise HTTPException(
                status_code=404, detail="Selected path not found"
            ) from None
        return Response(
            zip_bytes,
            media_type="application/zip",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(timestamped_selection_zip_name())}"
            },
        )

    @app.post("/_bulk/delete", include_in_schema=False)
    async def bulk_delete(request: Request):
        started = time.monotonic()
        user = get_user(request, settings)
        require_perm(user, "delete", settings)
        body = await _bulk_body(request)
        paths = _resolve_bulk_paths(body.get("paths"))
        rel_paths = [_to_rel_path(fspath) for fspath in _top_level_paths(paths)]
        deleted = await _soft_delete_paths(paths, user)
        response = JSONResponse(
            {
                "ok": True,
                "transaction_id": deleted["transaction_id"],
                "count": deleted["count"],
                "deleted": deleted["count"],
            }
        )
        await _record_semantic_audit(
            user=user,
            operation="bulk_delete",
            path="/_bulk/delete",
            details=json.dumps(
                {
                    "count": deleted["count"],
                    "paths": rel_paths,
                    "transaction_id": deleted["transaction_id"],
                },
                ensure_ascii=False,
            ),
            status_code=response.status_code,
            started=started,
        )
        return response

    @app.post("/api/restore/{transaction_id}", include_in_schema=False)
    async def restore_delete(transaction_id: str, request: Request):
        started = time.monotonic()
        user = get_user(request, settings)
        require_perm(user, "write", settings)
        transaction = delete_transactions.get(transaction_id)
        if not transaction:
            raise HTTPException(status_code=404, detail="Delete transaction not found")
        if transaction["user"] != user:
            raise HTTPException(
                status_code=403, detail="Cannot restore another user's delete"
            )

        restored = 0
        restored_paths = []
        for item in transaction["items"]:
            trash_path: Path = item["trash"]
            original_path: Path = item["original"]
            if not trash_path.exists():
                continue
            target = _restore_candidate(original_path, item["kind"])
            target.parent.mkdir(parents=True, exist_ok=True)
            await anyio.to_thread.run_sync(shutil.move, str(trash_path), str(target))
            restored += 1
            restored_paths.append(_to_rel_path(target))

        delete_transactions.pop(transaction_id, None)
        await anyio.to_thread.run_sync(
            trash_store.save_transactions,
            _trash_dir(),
            settings.root_dir,
            delete_transactions,
        )
        response = JSONResponse(
            {"ok": True, "restored": restored, "paths": restored_paths}
        )
        await _record_semantic_audit(
            user=user,
            operation="restore",
            path=f"/api/restore/{transaction_id}",
            details=json.dumps(
                {"restored": restored, "paths": restored_paths},
                ensure_ascii=False,
            ),
            status_code=response.status_code,
            started=started,
        )
        return response

    def _admin_user(request: Request) -> str:
        if _ldap_config_path is None and not settings.trusted_auth_proxies:
            raise HTTPException(
                status_code=403,
                detail="Admin console requires LDAPGate authentication",
            )
        user = getattr(request.state, "user", None)
        if not user:
            user = get_user(request, settings)
        if not user or str(user).lower() == "anonymous":
            raise HTTPException(
                status_code=403,
                detail="Admin console requires an authenticated LDAPGate user",
            )
        user = str(user).lower()
        if not settings.is_admin_user(user):
            raise HTTPException(status_code=403, detail="Administrator access denied")
        return user

    def _read_users_document() -> tuple[Path, dict, dict]:
        path = settings.users_config
        if path is None:
            raise HTTPException(
                status_code=409,
                detail="User management requires --users-config",
            )
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            raise HTTPException(
                status_code=404, detail="Users config not found"
            ) from None
        except (OSError, yaml.YAMLError) as exc:
            raise HTTPException(
                status_code=500, detail=f"Could not read users config: {exc}"
            ) from None
        if not isinstance(document, dict) or not isinstance(
            document.get("users"), dict
        ):
            raise HTTPException(
                status_code=500, detail="Users config has invalid structure"
            )
        return path, document, document["users"]

    def _normalize_username(value: str) -> str:
        username = value.strip().lower()
        if (
            not username
            or username == "*"
            or len(username) > 128
            or any(ord(char) < 32 or ord(char) == 127 for char in username)
            or "/" in username
        ):
            raise HTTPException(status_code=400, detail="Invalid username")
        return username

    def _permissions_dict(value: str | dict) -> dict[str, bool]:
        try:
            perms = UserConfig._parse("user", value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {
            "read": perms.read,
            "write": perms.write,
            "delete": perms.delete,
        }

    def _users_payload(entries: dict) -> dict:
        users = []
        default = None
        for username, value in sorted(
            entries.items(), key=lambda pair: str(pair[0]).lower()
        ):
            if str(username) == "*":
                default = _permissions_dict(value)
                continue
            users.append(
                {
                    "username": str(username).lower(),
                    "permissions": _permissions_dict(value),
                }
            )
        return {"users": users, "default": default}

    def _write_yaml_document(path: Path, document: dict) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(
                yaml.safe_dump(document, sort_keys=False),
                encoding="utf-8",
            )
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass

    def _prepare_ldap_user_document(entries: dict) -> tuple[Path, dict] | None:
        if _ldap_config_path is None:
            return None
        path, document = _read_ldap_document()
        section = document.get("ldap")
        if not isinstance(section, dict):
            raise HTTPException(
                status_code=500, detail="LDAPGate config has invalid ldap section"
            )
        usernames = sorted(
            {
                *(
                    str(username).strip().lower()
                    for username in entries
                    if str(username) != "*"
                ),
                *(user.strip().lower() for user in settings.admin_users),
            }
        )
        if (
            not usernames
            and bool(section.get("require_authorization_rule", True))
            and not section.get("group_dn")
        ):
            raise HTTPException(
                status_code=409,
                detail="At least one LDAP user must remain while LDAP authorization is enabled",
            )
        section["allowed_users"] = usernames
        _validate_ldap_document(path, document)
        return path, document

    def _save_user_documents(
        users_path: Path,
        users_document: dict,
        ldap_update: tuple[Path, dict] | None,
    ) -> None:
        try:
            original_users = users_path.read_bytes()
            _write_yaml_document(users_path, users_document)
            try:
                if ldap_update is not None:
                    _write_yaml_document(*ldap_update)
            except Exception:
                users_path.write_bytes(original_users)
                raise
            settings._user_config = UserConfig(users_path)
            settings._config_mtime = users_path.stat().st_mtime
        except HTTPException:
            raise
        except (OSError, yaml.YAMLError, ValueError) as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Could not save users and LDAP config: {exc}",
            ) from None

    async def _record_admin_event(
        user: str,
        operation: str,
        path: str,
        details: dict | None = None,
    ) -> None:
        await _record_semantic_audit(
            user=user,
            operation=operation,
            path=path,
            details=json.dumps(details, ensure_ascii=False) if details else None,
            status_code=200,
            started=time.monotonic(),
        )

    @app.get("/admin", include_in_schema=False)
    async def admin_page(request: Request):
        user = _admin_user(request)
        return templates.TemplateResponse(
            request,
            "admin.html",
            {
                "user": user,
                "ldap_configured": _ldap_config_path is not None,
                "auth_idle_timeout": ldap_idle_timeout,
            },
        )

    @app.get("/api/admin/users", include_in_schema=False)
    async def admin_users(request: Request):
        _admin_user(request)
        _, _, entries = _read_users_document()
        return _users_payload(entries)

    @app.post("/api/admin/users", include_in_schema=False)
    async def admin_upsert_user(payload: AdminUserPayload, request: Request):
        actor = _admin_user(request)
        path, document, entries = _read_users_document()
        username = _normalize_username(payload.username)
        original_username = (
            _normalize_username(payload.original_username)
            if payload.original_username
            else username
        )
        for existing in list(entries):
            if str(existing).lower() in {username, original_username}:
                del entries[existing]
        entries[username] = payload.permissions.model_dump()
        ldap_update = _prepare_ldap_user_document(entries)
        _save_user_documents(path, document, ldap_update)
        await _record_admin_event(
            actor,
            "admin_user_upsert",
            "/api/admin/users",
            {
                "username": username,
                "permissions": payload.permissions.model_dump(),
                "ldap_synced": ldap_update is not None,
            },
        )
        return {
            "ok": True,
            "restart_required": ldap_update is not None,
            **_users_payload(entries),
        }

    @app.delete("/api/admin/users/{username}", include_in_schema=False)
    async def admin_delete_user(username: str, request: Request):
        actor = _admin_user(request)
        path, document, entries = _read_users_document()
        username = _normalize_username(username)
        matching = next((key for key in entries if str(key).lower() == username), None)
        if matching is None:
            raise HTTPException(status_code=404, detail="User not found")
        del entries[matching]
        ldap_update = _prepare_ldap_user_document(entries)
        _save_user_documents(path, document, ldap_update)
        await _record_admin_event(
            actor,
            "admin_user_delete",
            f"/api/admin/users/{username}",
            {"username": username, "ldap_synced": ldap_update is not None},
        )
        return {
            "ok": True,
            "restart_required": ldap_update is not None,
            **_users_payload(entries),
        }

    def _ldap_file() -> Path:
        if _ldap_config_path is None:
            raise HTTPException(
                status_code=404, detail="LDAPGate config is not configured"
            )
        if not _ldap_config_path.exists():
            raise HTTPException(status_code=404, detail="LDAPGate config not found")
        return _ldap_config_path

    def _read_ldap_document() -> tuple[Path, dict]:
        path = _ldap_file()
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise HTTPException(
                status_code=500, detail=f"Could not read LDAPGate config: {exc}"
            ) from None
        if not isinstance(document, dict):
            raise HTTPException(
                status_code=500, detail="LDAPGate config has invalid structure"
            )
        return path, document

    def _validate_ldap_document(path: Path, document: dict) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(
                yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
            )
            temporary.chmod(0o600)
            try:
                from ldapgate.config import load_config  # type: ignore[import]
            except ImportError:
                raise HTTPException(
                    status_code=503,
                    detail="ldapgate is required to validate LDAPGate config",
                ) from None
            load_config(temporary)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid LDAPGate configuration: {exc}",
            ) from None
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass

    def _path_size(path: Path) -> int:
        try:
            if path.is_file() and not path.is_symlink():
                return path.stat().st_size
            if path.is_dir() and not path.is_symlink():
                return sum(
                    child.stat().st_size
                    for child in path.rglob("*")
                    if child.is_file() and not child.is_symlink()
                )
        except OSError:
            return 0
        return 0

    def _trash_payload() -> dict:
        transactions = []
        for transaction_id, transaction in sorted(
            delete_transactions.items(),
            key=lambda pair: pair[1].get("created", 0),
            reverse=True,
        ):
            items = []
            for item in transaction["items"]:
                trash_path: Path = item["trash"]
                if not trash_path.exists():
                    continue
                items.append(
                    {
                        "path": _to_rel_path(item["original"]),
                        "kind": item["kind"],
                        "size": _path_size(trash_path),
                    }
                )
            if items:
                transactions.append(
                    {
                        "transaction_id": transaction_id,
                        "user": transaction["user"],
                        "created": datetime.fromtimestamp(
                            transaction["created"], tz=timezone.utc
                        ).isoformat(),
                        "items": items,
                        "size": sum(item["size"] for item in items),
                    }
                )
        return {
            "transactions": transactions,
            "count": sum(len(item["items"]) for item in transactions),
            "size": sum(item["size"] for item in transactions),
        }

    @app.get("/api/admin/trash", include_in_schema=False)
    async def admin_trash(request: Request):
        _admin_user(request)
        return await anyio.to_thread.run_sync(_trash_payload)

    @app.post("/api/admin/trash/{transaction_id}/restore", include_in_schema=False)
    async def admin_restore_trash(transaction_id: str, request: Request):
        actor = _admin_user(request)
        transaction = delete_transactions.get(transaction_id)
        if not transaction:
            raise HTTPException(status_code=404, detail="Trash transaction not found")
        restored_paths = []
        for item in transaction["items"]:
            trash_path: Path = item["trash"]
            original_path: Path = item["original"]
            if not trash_path.exists():
                continue
            target = _restore_candidate(original_path, item["kind"])
            target.parent.mkdir(parents=True, exist_ok=True)
            await anyio.to_thread.run_sync(shutil.move, str(trash_path), str(target))
            restored_paths.append(_to_rel_path(target))
        delete_transactions.pop(transaction_id, None)
        await anyio.to_thread.run_sync(
            trash_store.save_transactions,
            _trash_dir(),
            settings.root_dir,
            delete_transactions,
        )
        await _record_admin_event(
            actor,
            "admin_trash_restore",
            f"/api/admin/trash/{transaction_id}/restore",
            {"restored": len(restored_paths), "paths": restored_paths},
        )
        return {"ok": True, "restored": len(restored_paths), "paths": restored_paths}

    @app.delete("/api/admin/trash/{transaction_id}", include_in_schema=False)
    async def admin_delete_trash(transaction_id: str, request: Request):
        actor = _admin_user(request)
        transaction = delete_transactions.get(transaction_id)
        if not transaction:
            raise HTTPException(status_code=404, detail="Trash transaction not found")
        deleted = 0
        for item in transaction["items"]:
            trash_path: Path = item["trash"]
            if not trash_path.exists():
                continue
            if trash_path.is_dir():
                await anyio.to_thread.run_sync(shutil.rmtree, trash_path)
            else:
                await anyio.to_thread.run_sync(trash_path.unlink)
            deleted += 1
        delete_transactions.pop(transaction_id, None)
        await anyio.to_thread.run_sync(
            trash_store.save_transactions,
            _trash_dir(),
            settings.root_dir,
            delete_transactions,
        )
        await _record_admin_event(
            actor,
            "admin_trash_delete",
            f"/api/admin/trash/{transaction_id}",
            {"deleted": deleted},
        )
        return {"ok": True, "deleted": deleted}

    def _storage_metrics() -> dict[str, int]:
        files = 0
        bytes_used = 0
        for path in settings.root_dir.rglob("*"):
            if (
                path.is_file()
                and not path.is_symlink()
                and not _is_internal_path(path)
                and not _is_ignored_system_path(path)
                and not _is_sensitive_path(path)
            ):
                try:
                    bytes_used += path.stat().st_size
                    files += 1
                except OSError:
                    continue
        return {"files": files, "bytes": bytes_used}

    async def _admin_metrics() -> dict:
        _, _, entries = _read_users_document()
        since = datetime.fromtimestamp(
            time.time() - ACTIVE_USER_WINDOW_MINUTES * 60, tz=timezone.utc
        ).isoformat()
        if settings.audit_db and settings.audit_db.exists():
            summary = await anyio.to_thread.run_sync(
                lambda: audit_store.summarize_events(settings.audit_db, since=since)
            )
        else:
            summary = {"event_count": 0, "active_users": 0, "by_user": []}
        storage = await anyio.to_thread.run_sync(_storage_metrics)
        trash = await anyio.to_thread.run_sync(_trash_payload)
        return {
            "configured_users": len([key for key in entries if str(key) != "*"]),
            "active_users": summary["active_users"],
            "activity_events": summary["event_count"],
            "active_window_minutes": ACTIVE_USER_WINDOW_MINUTES,
            "storage": storage,
            "trash": {"items": trash["count"], "bytes": trash["size"]},
        }

    @app.get("/api/admin/metrics", include_in_schema=False)
    async def admin_metrics(request: Request):
        _admin_user(request)
        return await _admin_metrics()

    @app.get("/api/admin/activity", include_in_schema=False)
    async def admin_activity(request: Request):
        _admin_user(request)
        username = request.query_params.get("username") or None
        since = request.query_params.get("since") or None
        scope = request.query_params.get("scope", "file")
        if scope not in {"all", "file", "admin"}:
            raise HTTPException(
                status_code=400, detail="scope must be all, file, or admin"
            )
        try:
            limit = min(max(int(request.query_params.get("limit", "200")), 1), 1000)
        except ValueError:
            raise HTTPException(
                status_code=400, detail="limit must be an integer"
            ) from None
        if not settings.audit_db or not settings.audit_db.exists():
            return {
                "events": [],
                "summary": {"event_count": 0, "active_users": 0, "by_user": []},
            }
        events = await anyio.to_thread.run_sync(
            lambda: audit_store.list_events(
                settings.audit_db,
                username=username,
                since=since,
                scope=scope,
                limit=limit,
            )
        )
        summary = await anyio.to_thread.run_sync(
            lambda: audit_store.summarize_events(
                settings.audit_db,
                since=since,
                scope=scope,
            )
        )
        return {"events": events, "summary": summary}

    @app.delete("/api/admin/activity", include_in_schema=False)
    async def admin_purge_activity(request: Request):
        actor = _admin_user(request)
        try:
            older_than_days = int(request.query_params.get("older_than_days", "90"))
        except ValueError:
            raise HTTPException(
                status_code=400, detail="older_than_days must be an integer"
            ) from None
        if not 1 <= older_than_days <= 36500:
            raise HTTPException(
                status_code=400,
                detail="older_than_days must be between 1 and 36500",
            )
        deleted = 0
        if settings.audit_db and settings.audit_db.exists():
            deleted = await anyio.to_thread.run_sync(
                audit_store.purge_events,
                settings.audit_db,
                older_than_days,
            )
        await _record_admin_event(
            actor,
            "admin_audit_purge",
            "/api/admin/activity",
            {"older_than_days": older_than_days, "deleted": deleted},
        )
        return {"ok": True, "deleted": deleted, "older_than_days": older_than_days}

    # ── Catch-all route ───────────────────────────────────────────────────────

    @app.api_route(
        "/{path:path}",
        methods=[
            "GET",
            "HEAD",
            "PUT",
            "DELETE",
            "OPTIONS",
            "PROPFIND",
            "MKCOL",
            "COPY",
            "MOVE",
            "LOCK",
            "UNLOCK",
        ],
    )
    async def catch_all(request: Request, path: str = ""):
        method = request.method.upper()
        started = time.monotonic()
        user = get_user(request, settings)
        fspath = resolve(request)

        # OPTIONS is exempt from auth: WebDAV clients (e.g. Windows, Finder) send
        # OPTIONS before credentials to discover supported methods (RFC 4918 §9.1).
        # Blocking it would prevent clients from negotiating the connection at all.
        if method != "OPTIONS":
            require_perm(user, "read", settings)

        if method == "OPTIONS":
            return Response(
                status_code=200,
                headers={
                    "Allow": "GET,HEAD,PUT,DELETE,OPTIONS,PROPFIND,MKCOL,COPY,MOVE,LOCK,UNLOCK",
                    "DAV": "1, 2",
                    "MS-Author-Via": "DAV",
                },
            )

        if method == "PROPFIND":
            if not fspath.exists():
                raise HTTPException(status_code=404)
            if _is_ignored_system_path(fspath):
                raise HTTPException(status_code=404)
            _reject_sensitive_path(fspath)
            return propfind_response(request, fspath, settings.root_dir)

        if method == "MKCOL":
            require_perm(user, "write", settings)
            if _is_ignored_system_path(fspath):
                return Response(status_code=201)
            _reject_sensitive_path(fspath)
            response = mkcol_response(fspath)
            await _record_semantic_audit(
                user=user,
                operation="mkdir",
                path=_to_rel_path(fspath),
                details=None,
                status_code=response.status_code,
                started=started,
            )
            return response

        if method == "COPY":
            require_perm(user, "write", settings)
            dest = dest_from_header(request, settings.root_dir)
            if _is_ignored_system_path(fspath) or _is_ignored_system_path(dest):
                return Response(status_code=201)
            _reject_sensitive_path(fspath)
            _reject_sensitive_path(dest)
            overwrite = request.headers.get("overwrite", "T").upper() != "F"
            response = await copy_response(fspath, dest, overwrite)
            await _record_semantic_audit(
                user=user,
                operation="copy",
                path=_to_rel_path(fspath),
                details=json.dumps(
                    {"destination": _to_rel_path(dest), "overwrite": overwrite},
                    ensure_ascii=False,
                ),
                status_code=response.status_code,
                started=started,
            )
            return response

        if method == "MOVE":
            require_perm(user, "delete", settings)
            require_perm(user, "write", settings)
            dest = dest_from_header(request, settings.root_dir)
            if _is_root_path(fspath):
                raise HTTPException(status_code=403, detail="Cannot move root")
            if _is_ignored_system_path(fspath) or _is_ignored_system_path(dest):
                return Response(status_code=204)
            _reject_sensitive_path(fspath)
            _reject_sensitive_path(dest)
            overwrite = request.headers.get("overwrite", "T").upper() != "F"
            source_path = _to_rel_path(fspath)
            response = await move_response(fspath, dest, overwrite)
            await _record_semantic_audit(
                user=user,
                operation="move",
                path=source_path,
                details=json.dumps(
                    {"destination": _to_rel_path(dest), "overwrite": overwrite},
                    ensure_ascii=False,
                ),
                status_code=response.status_code,
                started=started,
            )
            return response

        if method == "LOCK":
            return lock_response(fspath)

        if method == "UNLOCK":
            return unlock_response()

        if method == "PUT":
            return await _handle_put(fspath, request, user)

        if method == "DELETE":
            return await _handle_delete(fspath, user)

        # GET / HEAD
        return await _handle_get(fspath, request, user)

    return app


def _ensure_ldapgate_static_paths(config) -> None:
    """Allow login-page assets to load without triggering Basic auth."""
    proxy_config = getattr(config, "proxy", None)
    if proxy_config is None:
        return
    static_paths = list(getattr(proxy_config, "static_paths", []) or [])
    for path in ("/static", "/favicon.ico"):
        if path not in static_paths:
            static_paths.append(path)
    proxy_config.static_paths = static_paths


def _ensure_ldapgate_cookie_name(config) -> None:
    """Keep xwing auth cookies distinct from sibling localhost apps."""
    proxy_config = getattr(config, "proxy", None)
    if proxy_config is None:
        return
    if (
        getattr(proxy_config, "session_cookie_name", "ldapgate_session")
        == "ldapgate_session"
    ):
        proxy_config.session_cookie_name = "xwing_session"


def _sync_ldapgate_trusted_proxies(config, settings: Settings) -> None:
    """Share xwing's trusted proxy list with embedded ldapgate when unset."""
    proxy_config = getattr(config, "proxy", None)
    if proxy_config is None:
        return
    if getattr(proxy_config, "trusted_proxies", None):
        return
    if settings.trusted_auth_proxies:
        proxy_config.trusted_proxies = list(settings.trusted_auth_proxies)
