import asyncio
import json
import shutil
import time
import uuid
from pathlib import Path

import anyio
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .auth import get_user, require_perm
from .config import Settings
from .files import safe_path

_SESSION_FILE = "session.json"


def _session_path(tmp_dir: Path, session_id: str) -> Path:  # type: ignore[union-attr]
    return tmp_dir / session_id / _SESSION_FILE


def _session_path_sync(tmp_dir: Path, session_id: str) -> Path:  # type: ignore[union-attr]
    """Synchronous version for tests."""
    return tmp_dir / session_id / _SESSION_FILE


async def _load_session(tmp_dir: Path, session_id: str) -> dict | None:  # type: ignore[union-attr]
    """Load session from disk, returns None if not found or expired."""
    session_file = _session_path(tmp_dir, session_id)
    try:
        async with await anyio.open_file(session_file, "r") as f:
            content = await f.read()
            return json.loads(content)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


async def _save_session(tmp_dir: Path, session: dict) -> None:  # type: ignore[union-attr]
    """Save session to disk."""
    session_id = session["session_id"]
    session_file = _session_path(tmp_dir, session_id)
    session_file.parent.mkdir(parents=True, exist_ok=True)
    async with await anyio.open_file(session_file, "w") as f:
        await f.write(json.dumps(session))


async def _delete_session(tmp_dir: Path, session_id: str) -> None:  # type: ignore[union-attr]
    """Delete session from disk."""
    session_dir = tmp_dir / session_id
    shutil.rmtree(session_dir, ignore_errors=True)


def create_upload_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/_upload")

    @router.post("/init")
    async def upload_init(request: Request):
        user = get_user(request, settings)
        require_perm(user, "write", settings)

        body = await request.json()

        # Strip all path components — only the bare filename is allowed
        raw_name: str = body.get("filename", "upload")
        filename = Path(raw_name).name
        if not filename or filename in (".", ".."):
            raise HTTPException(status_code=400, detail="Invalid filename")

        # .env files and variants contain sensitive data — reject before accepting any data
        if filename == ".env" or filename.startswith(".env."):
            raise HTTPException(
                status_code=400, detail="Uploading .env files is not allowed"
            )

        total_chunks: int = int(body.get("total_chunks", 1))
        if total_chunks < 1 or total_chunks > settings.max_chunks:
            raise HTTPException(
                status_code=400, detail=f"total_chunks must be 1–{settings.max_chunks}"
            )

        rel_dir: str = body.get("dir", "")
        dest_dir = safe_path(settings.root_dir, rel_dir)
        if not dest_dir.exists() or not dest_dir.is_dir():
            raise HTTPException(
                status_code=404, detail="Destination directory not found"
            )

        session_id = uuid.uuid4().hex
        session_tmp = settings.tmp_dir / session_id  # type: ignore[operator]
        session_tmp.mkdir(parents=True, exist_ok=True)

        session = {
            "session_id": session_id,
            "dest_dir": str(dest_dir),
            "filename": filename,
            "total_chunks": total_chunks,
            "received": [],  # Stored as list for JSON serialization
            "created_at": time.monotonic(),
            "user": user,
        }
        await _save_session(settings.tmp_dir, session)  # type: ignore[union-attr]
        return JSONResponse({"session_id": session_id})

    @router.put("/{session_id}/{chunk_index}")
    async def upload_chunk(session_id: str, chunk_index: int, request: Request):
        user = get_user(request, settings)
        session = await _load_session(settings.tmp_dir, session_id)  # type: ignore[union-attr]
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        session_user = session.get("user")
        if session_user and session_user != user:
            raise HTTPException(status_code=403, detail="Not session owner")

        if chunk_index < 0 or chunk_index >= session["total_chunks"]:
            raise HTTPException(status_code=400, detail="Invalid chunk index")

        chunk_path = settings.tmp_dir / session_id / f"{chunk_index}.part"  # type: ignore[union-attr]
        received_bytes = 0
        try:
            async with await anyio.open_file(chunk_path, "wb") as f:
                async for chunk in request.stream():
                    received_bytes += len(chunk)
                    if received_bytes > settings.max_chunk_bytes:
                        raise HTTPException(
                            status_code=413, detail="Chunk exceeds 100MB limit"
                        )
                    if received_bytes > settings.max_upload_bytes:
                        raise HTTPException(
                            status_code=413, detail="Chunk exceeds total upload limit"
                        )
                    await f.write(chunk)
        except HTTPException:
            try:
                chunk_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise

        # Update received chunks in session and refresh TTL on activity
        session["received"].append(chunk_index)
        session["created_at"] = time.monotonic()
        await _save_session(settings.tmp_dir, session)  # type: ignore[union-attr]
        return Response(status_code=204)

    @router.post("/{session_id}/complete")
    async def upload_complete(session_id: str, request: Request):
        user = get_user(request, settings)
        require_perm(user, "write", settings)
        session = await _load_session(settings.tmp_dir, session_id)  # type: ignore[union-attr]
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        session_user = session.get("user")
        if session_user and session_user != user:
            raise HTTPException(status_code=403, detail="Not session owner")

        total = session["total_chunks"]
        received = set(session["received"])
        if len(received) != total or set(range(total)) != received:
            missing = sorted(set(range(total)) - received)
            raise HTTPException(status_code=400, detail=f"Missing chunks: {missing}")

        dest_dir = Path(session["dest_dir"])
        dest_file = dest_dir / session["filename"]
        tmp_dir = settings.tmp_dir / session_id  # type: ignore[union-attr]

        try:
            async with await anyio.open_file(dest_file, "wb") as out:
                for i in range(total):
                    chunk_path = tmp_dir / f"{i}.part"
                    async with await anyio.open_file(chunk_path, "rb") as inp:
                        while True:
                            data = await inp.read(settings.chunk_read_size)
                            if not data:
                                break
                            await out.write(data)
        except OSError as e:
            # Disk full or I/O error during write
            try:
                dest_file.unlink(missing_ok=True)
            except Exception:
                pass
            raise HTTPException(status_code=500, detail=f"Write failed: {e}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            await _delete_session(settings.tmp_dir, session_id)  # type: ignore[union-attr]

        return JSONResponse({"path": str(dest_file.relative_to(settings.root_dir))})

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
            async with await anyio.open_file(session_file, "r") as f:
                session = json.loads(await f.read())
            if now - session.get("created_at", 0) > ttl:
                shutil.rmtree(session_dir, ignore_errors=True)
        except (json.JSONDecodeError, OSError):
            continue


# Backward compatibility alias for tests
_cleanup_once = _cleanup_stale_async
