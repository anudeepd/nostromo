import asyncio
import json
import logging
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import unquote

import anyio
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .auth import get_user, require_perm
from . import audit_store
from .config import Settings
from .files import is_ignored_system_file, is_within_root, safe_path

logger = logging.getLogger(__name__)

_SESSION_FILE = "session.json"
_SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SESSION_LOCKS: dict[str, asyncio.Lock] = {}
_CHUNK_LOCKS: dict[tuple[str, int], asyncio.Lock] = {}
_SESSION_CACHE: dict[str, dict] = {}


def _session_lock(session_id: str) -> asyncio.Lock:
    lock = _SESSION_LOCKS.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _SESSION_LOCKS[session_id] = lock
    return lock


def _chunk_lock(session_id: str, chunk_index: int) -> asyncio.Lock:
    key = (session_id, chunk_index)
    lock = _CHUNK_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _CHUNK_LOCKS[key] = lock
    return lock


def _validate_session_id(session_id: str) -> None:
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise HTTPException(status_code=404, detail="Session not found")


def _session_path(tmp_dir: Path, session_id: str) -> Path:  # type: ignore[union-attr]
    return tmp_dir / session_id / _SESSION_FILE


def _session_path_sync(tmp_dir: Path, session_id: str) -> Path:  # type: ignore[union-attr]
    """Synchronous version for tests."""
    return tmp_dir / session_id / _SESSION_FILE


async def _load_session(tmp_dir: Path, session_id: str) -> dict | None:  # type: ignore[union-attr]
    """Load session from disk, returns None if not found or expired."""
    cached = _SESSION_CACHE.get(session_id)
    if cached is not None:
        return cached

    session_file = _session_path(tmp_dir, session_id)
    try:
        async with await anyio.open_file(session_file, "r") as f:
            content = await f.read()
            session = json.loads(content)
            _SESSION_CACHE[session_id] = session
            return session
    except (FileNotFoundError, json.JSONDecodeError):
        return None


async def _save_session(tmp_dir: Path, session: dict) -> None:  # type: ignore[union-attr]
    """Save session to disk."""
    session_id = session["session_id"]
    session_file = _session_path(tmp_dir, session_id)
    session_file.parent.mkdir(parents=True, exist_ok=True)
    async with await anyio.open_file(session_file, "w") as f:
        await f.write(json.dumps(session))
    _SESSION_CACHE[session_id] = session


async def _delete_session(tmp_dir: Path, session_id: str) -> None:  # type: ignore[union-attr]
    """Delete session from disk."""
    _SESSION_CACHE.pop(session_id, None)
    session_dir = tmp_dir / session_id
    shutil.rmtree(session_dir, ignore_errors=True)


def _delete_direct_temp(session: dict | None, root: Path) -> None:
    if not session:
        return
    temp_file = session.get("temp_file")
    if not temp_file:
        return
    try:
        path = Path(temp_file)
        if is_within_root(root, path):
            path.unlink(missing_ok=True)
    except Exception:
        pass


def _drop_session_lock(session_id: str) -> None:
    _SESSION_LOCKS.pop(session_id, None)
    for key in [key for key in _CHUNK_LOCKS if key[0] == session_id]:
        _CHUNK_LOCKS.pop(key, None)
    _SESSION_CACHE.pop(session_id, None)


def _to_audit_path(root: Path, fspath: Path) -> str:
    rel = fspath.relative_to(root)
    if rel == Path("."):
        return "/"
    return "/" + rel.as_posix()


def _validate_session_owner_and_chunk(
    session: dict | None,
    *,
    user: str,
    chunk_index: int | None = None,
) -> None:
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    session_user = session.get("user")
    if session_user and session_user != user:
        raise HTTPException(status_code=403, detail="Not session owner")
    if chunk_index is not None and (
        chunk_index < 0 or chunk_index >= session["total_chunks"]
    ):
        raise HTTPException(status_code=400, detail="Invalid chunk index")


def _session_chunk_bytes(
    session: dict, tmp_dir: Path, session_id: str
) -> dict[str, int]:
    chunk_bytes = session.get("chunk_bytes")
    if isinstance(chunk_bytes, dict):
        return {str(k): int(v) for k, v in chunk_bytes.items()}

    session_dir = tmp_dir / session_id
    sizes = {}
    for index in session.get("received", []):
        chunk_path = session_dir / f"{index}.part"
        try:
            sizes[str(index)] = chunk_path.stat().st_size
        except OSError:
            sizes[str(index)] = 0
    return sizes


async def _record_upload_audit(
    *,
    settings: Settings,
    user: str,
    dest_file: Path,
    total_bytes: int,
    total_chunks: int,
    status_code: int,
    started: float,
) -> None:
    duration_ms = round((time.monotonic() - started) * 1000, 2)
    details = json.dumps(
        {"bytes": total_bytes, "chunks": total_chunks},
        ensure_ascii=False,
    )
    path = _to_audit_path(settings.root_dir, dest_file)
    logger.info(
        "file operation user=%s operation=upload path=%s status=%s duration_ms=%s details=%s",
        user,
        path,
        status_code,
        duration_ms,
        details,
    )
    if not settings.audit_db or user == "anonymous":
        return
    try:
        await audit_store.record_event_async(
            db_path=settings.audit_db,
            username=user,
            method="upload",
            path=path,
            details=details,
            status_code=status_code,
            duration_ms=duration_ms,
        )
    except Exception:
        logger.exception("Failed to record upload audit event")


def create_upload_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/_upload")

    @router.post("/init")
    async def upload_init(request: Request):
        user = get_user(request, settings)
        require_perm(user, "write", settings)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON") from None
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="JSON body must be an object")

        # Strip all path components — only the bare filename is allowed
        raw_name = body.get("filename", "upload")
        if not isinstance(raw_name, str):
            raise HTTPException(status_code=400, detail="Invalid filename")
        filename = Path(raw_name).name
        if not filename or filename in (".", ".."):
            raise HTTPException(status_code=400, detail="Invalid filename")
        if is_ignored_system_file(filename):
            return JSONResponse({"ignored": True})

        # .env files and variants contain sensitive data — reject before accepting any data
        if filename == ".env" or filename.startswith(".env."):
            raise HTTPException(
                status_code=400, detail="Uploading .env files is not allowed"
            )

        try:
            total_chunks = int(body.get("total_chunks", 1))
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400, detail="total_chunks must be an integer"
            ) from None
        if total_chunks < 1 or total_chunks > settings.max_chunks:
            raise HTTPException(
                status_code=400, detail=f"total_chunks must be 1–{settings.max_chunks}"
            )

        raw_chunk_size = body.get("chunk_size")
        chunk_size = None
        if raw_chunk_size is not None:
            try:
                chunk_size = int(raw_chunk_size)
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=400, detail="chunk_size must be an integer"
                ) from None
            if chunk_size < 1 or chunk_size > settings.max_chunk_bytes:
                raise HTTPException(
                    status_code=400,
                    detail=f"chunk_size must be 1–{settings.max_chunk_bytes}",
                )

        raw_dir = body.get("dir", "")
        if not isinstance(raw_dir, str):
            raise HTTPException(status_code=400, detail="dir must be a string")
        rel_dir = unquote(raw_dir)
        dest_dir = safe_path(settings.root_dir, rel_dir)
        if not dest_dir.exists() or not dest_dir.is_dir():
            raise HTTPException(
                status_code=404, detail="Destination directory not found"
            )

        session_id = uuid.uuid4().hex
        session_tmp = settings.tmp_dir / session_id  # type: ignore[operator]
        session_tmp.mkdir(parents=True, exist_ok=True)
        temp_file = None
        if chunk_size is not None:
            temp_handle = tempfile.NamedTemporaryFile(
                prefix=f".{filename}.",
                suffix=".upload.tmp",
                dir=dest_dir,
                delete=False,
            )
            temp_file = temp_handle.name
            temp_handle.close()

        session = {
            "session_id": session_id,
            "dest_dir": str(dest_dir),
            "filename": filename,
            "total_chunks": total_chunks,
            "total_bytes": 0,
            "received": [],  # Stored as list for JSON serialization
            "chunk_bytes": {},
            "created_at": time.monotonic(),
            "user": user,
        }
        if chunk_size is not None:
            session["chunk_size"] = chunk_size
            session["temp_file"] = temp_file
        await _save_session(settings.tmp_dir, session)  # type: ignore[union-attr]
        return JSONResponse({"session_id": session_id})

    @router.put("/{session_id}/{chunk_index}")
    async def upload_chunk(session_id: str, chunk_index: int, request: Request):
        user = get_user(request, settings)
        require_perm(user, "write", settings)
        _validate_session_id(session_id)

        # State machine: validate -> stream -> validate size -> commit metadata.
        # One lock spans the whole attempt for this chunk so concurrent retries
        # cannot overwrite the same direct-file offset or race metadata commits.
        async with _chunk_lock(session_id, chunk_index):
            return await _write_chunk(session_id, chunk_index, request, user)

    async def _write_chunk(
        session_id: str, chunk_index: int, request: Request, user: str
    ) -> Response:
        session_dir = settings.tmp_dir / session_id  # type: ignore[operator]
        chunk_path = session_dir / f"{chunk_index}.part"
        pending_path = session_dir / f".{chunk_index}.{uuid.uuid4().hex}.part.tmp"
        received_bytes = 0
        direct_temp_file = None
        direct_chunk_size = None
        async with _session_lock(session_id):
            session = await _load_session(settings.tmp_dir, session_id)  # type: ignore[union-attr]
            _validate_session_owner_and_chunk(
                session, user=user, chunk_index=chunk_index
            )
            assert session is not None
            if "chunk_size" in session and "temp_file" in session:
                direct_chunk_size = int(session["chunk_size"])
                direct_temp_file = Path(session["temp_file"])
                if not is_within_root(settings.root_dir, direct_temp_file):
                    raise HTTPException(status_code=403, detail="Forbidden temp file")

        try:
            if direct_temp_file is not None and direct_chunk_size is not None:
                offset = chunk_index * direct_chunk_size
                async with await anyio.open_file(direct_temp_file, "r+b") as f:
                    await f.seek(offset)
                    async for chunk in request.stream():
                        received_bytes += len(chunk)
                        if received_bytes > settings.max_chunk_bytes:
                            raise HTTPException(
                                status_code=413, detail="Chunk exceeds 100MB limit"
                            )
                        await f.write(chunk)
            else:
                async with await anyio.open_file(pending_path, "wb") as f:
                    async for chunk in request.stream():
                        received_bytes += len(chunk)
                        if received_bytes > settings.max_chunk_bytes:
                            raise HTTPException(
                                status_code=413, detail="Chunk exceeds 100MB limit"
                            )
                        await f.write(chunk)
        except HTTPException:
            if direct_temp_file is None:
                try:
                    pending_path.unlink(missing_ok=True)
                except Exception:
                    pass
            raise
        except Exception:
            if direct_temp_file is None:
                try:
                    pending_path.unlink(missing_ok=True)
                except Exception:
                    pass
            raise

        if (
            direct_chunk_size is not None
            and received_bytes != direct_chunk_size
            and session is not None
            and chunk_index < session["total_chunks"] - 1
        ):
            raise HTTPException(
                status_code=400, detail="Non-final chunk has wrong size"
            )

        async with _session_lock(session_id):
            session = await _load_session(settings.tmp_dir, session_id)  # type: ignore[union-attr]
            try:
                _validate_session_owner_and_chunk(
                    session, user=user, chunk_index=chunk_index
                )
                assert session is not None
                chunk_bytes = _session_chunk_bytes(
                    session,
                    settings.tmp_dir,
                    session_id,  # type: ignore[arg-type]
                )
                old_chunk_bytes = chunk_bytes.get(str(chunk_index), 0)
                prior_total_bytes = (
                    int(session.get("total_bytes", sum(chunk_bytes.values())))
                    - old_chunk_bytes
                )

                if prior_total_bytes + received_bytes > settings.max_upload_bytes:
                    raise HTTPException(
                        status_code=413, detail="Upload exceeds total size limit"
                    )

                if direct_temp_file is None:
                    await anyio.to_thread.run_sync(pending_path.replace, chunk_path)  # type: ignore[reportAttributeAccessIssue]
                if chunk_index not in session["received"]:
                    session["received"].append(chunk_index)
                chunk_bytes[str(chunk_index)] = received_bytes
                session["chunk_bytes"] = chunk_bytes
                session["total_bytes"] = prior_total_bytes + received_bytes
                session["created_at"] = time.monotonic()
                await _save_session(settings.tmp_dir, session)  # type: ignore[union-attr]
            finally:
                if direct_temp_file is None:
                    try:
                        pending_path.unlink(missing_ok=True)
                    except Exception:
                        pass
        return Response(status_code=204)

    @router.post("/{session_id}/complete")
    async def upload_complete(session_id: str, request: Request):
        started = time.monotonic()
        user = get_user(request, settings)
        require_perm(user, "write", settings)
        _validate_session_id(session_id)
        drop_lock = False
        try:
            async with _session_lock(session_id):
                session = await _load_session(settings.tmp_dir, session_id)  # type: ignore[union-attr]
                _validate_session_owner_and_chunk(session, user=user)
                assert session is not None

                total = session["total_chunks"]
                received = set(session["received"])
                if len(received) != total or set(range(total)) != received:
                    missing = sorted(set(range(total)) - received)
                    raise HTTPException(
                        status_code=400, detail=f"Missing chunks: {missing}"
                    )

                dest_dir = Path(session["dest_dir"])
                if not is_within_root(settings.root_dir, dest_dir):
                    raise HTTPException(status_code=403, detail="Forbidden destination")
                dest_file = dest_dir / session["filename"]
                tmp_dir = settings.tmp_dir / session_id  # type: ignore[union-attr]
                temp_file = None

                try:
                    if "temp_file" in session:
                        temp_file = Path(session["temp_file"])
                        if not is_within_root(settings.root_dir, temp_file):
                            raise HTTPException(
                                status_code=403, detail="Forbidden temp file"
                            )
                        async with await anyio.open_file(temp_file, "r+b") as out:
                            await out.truncate(int(session.get("total_bytes", 0)))
                        await anyio.to_thread.run_sync(temp_file.replace, dest_file)  # type: ignore[reportAttributeAccessIssue]
                    else:
                        temp_handle = tempfile.NamedTemporaryFile(
                            prefix=f".{dest_file.name}.",
                            suffix=".tmp",
                            dir=dest_dir,
                            delete=False,
                        )
                        temp_file = Path(temp_handle.name)
                        temp_handle.close()
                        async with await anyio.open_file(temp_file, "wb") as out:
                            for i in range(total):
                                chunk_path = tmp_dir / f"{i}.part"
                                async with await anyio.open_file(
                                    chunk_path, "rb"
                                ) as inp:
                                    while True:
                                        data = await inp.read(settings.chunk_read_size)
                                        if not data:
                                            break
                                        await out.write(data)
                        await anyio.to_thread.run_sync(temp_file.replace, dest_file)  # type: ignore[reportAttributeAccessIssue]
                except OSError as e:
                    if temp_file is not None:
                        try:
                            temp_file.unlink(missing_ok=True)
                        except Exception:
                            pass
                    raise HTTPException(status_code=500, detail=f"Write failed: {e}")
                finally:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    await _delete_session(settings.tmp_dir, session_id)  # type: ignore[union-attr]
                    drop_lock = True

                response = JSONResponse(
                    {"path": str(dest_file.relative_to(settings.root_dir))}
                )
                await _record_upload_audit(
                    settings=settings,
                    user=user,
                    dest_file=dest_file,
                    total_bytes=int(session.get("total_bytes", 0)),
                    total_chunks=total,
                    status_code=response.status_code,
                    started=started,
                )
        finally:
            if drop_lock:
                _drop_session_lock(session_id)
        return response

    return router


async def cleanup_stale_sessions(settings: Settings) -> None:
    """Background task: remove sessions older than session_ttl_seconds."""
    while True:
        await asyncio.sleep(300)
        await _cleanup_stale_async(settings)  # type: ignore[union-attr]


async def _cleanup_stale_async(settings: Settings) -> None:  # type: ignore[union-attr]
    """Remove stale upload sessions by scanning filesystem."""
    now = time.monotonic()
    if not settings.tmp_dir or not settings.tmp_dir.exists():
        return

    ttl = settings.session_ttl_seconds

    for session_dir in settings.tmp_dir.iterdir():
        if not session_dir.is_dir():
            continue
        session_file = session_dir / _SESSION_FILE
        if not session_file.exists():
            continue
        try:
            session = json.loads(session_file.read_text())
            if now - session.get("created_at", 0) > ttl:
                _SESSION_CACHE.pop(session.get("session_id", session_dir.name), None)
                _delete_direct_temp(session, settings.root_dir)
                shutil.rmtree(session_dir, ignore_errors=True)
        except (json.JSONDecodeError, OSError):
            continue


# Backward compatibility alias for tests
_cleanup_once = _cleanup_stale_async
