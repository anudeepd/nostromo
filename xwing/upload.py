import asyncio
import shutil
import time
import uuid
from pathlib import Path

import anyio
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .config import Settings
from .files import safe_path

# session_id → {dest_dir, filename, total_chunks, received, created_at}
_sessions: dict[str, dict] = {}
_SESSION_TTL = 3600  # seconds
_MAX_CHUNKS = 10_000


def create_upload_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/_upload")

    def _require_auth(request: Request) -> str | None:
        """Authenticate request; return user header value or raise 403 if required."""
        user = request.headers.get(settings.user_header)
        if user is None and settings.require_auth:
            raise HTTPException(status_code=403, detail="Authentication required")
        return user

    @router.post("/init")
    async def upload_init(request: Request):
        user = _require_auth(request)
        body = await request.json()

        # Strip all path components — only the bare filename is allowed
        raw_name: str = body.get("filename", "upload")
        filename = Path(raw_name).name
        if not filename or filename in (".", ".."):
            raise HTTPException(status_code=400, detail="Invalid filename")

        # .env files and variants contain secrets — reject before accepting any data
        if filename == ".env" or filename.startswith(".env."):
            raise HTTPException(
                status_code=400, detail="Uploading .env files is not allowed"
            )

        total_chunks: int = int(body.get("total_chunks", 1))
        if total_chunks < 1 or total_chunks > _MAX_CHUNKS:
            raise HTTPException(
                status_code=400, detail=f"total_chunks must be 1–{_MAX_CHUNKS}"
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

        _sessions[session_id] = {
            "dest_dir": str(dest_dir),
            "filename": filename,
            "total_chunks": total_chunks,
            "received": set(),
            "created_at": time.monotonic(),
            "user": user,
        }
        return JSONResponse({"session_id": session_id})

    @router.put("/{session_id}/{chunk_index}")
    async def upload_chunk(session_id: str, chunk_index: int, request: Request):
        user = _require_auth(request)
        session = _sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        if session.get("user") is not None and session.get("user") != user:
            raise HTTPException(status_code=403, detail="Not session owner")

        if chunk_index < 0 or chunk_index >= session["total_chunks"]:
            raise HTTPException(status_code=400, detail="Invalid chunk index")

        chunk_path = settings.tmp_dir / session_id / f"{chunk_index}.part"  # type: ignore[operator]
        received_bytes = 0
        async with await anyio.open_file(chunk_path, "wb") as f:
            async for chunk in request.stream():
                received_bytes += len(chunk)
                if received_bytes > settings.max_upload_bytes:
                    raise HTTPException(status_code=413, detail="Chunk too large")
                await f.write(chunk)

        session["received"].add(chunk_index)
        return Response(status_code=204)

    @router.post("/{session_id}/complete")
    async def upload_complete(session_id: str, request: Request):
        user = _require_auth(request)
        session = _sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        if session.get("user") is not None and session.get("user") != user:
            raise HTTPException(status_code=403, detail="Not session owner")

        total = session["total_chunks"]
        received = session["received"]
        if len(received) != total or set(range(total)) != received:
            missing = sorted(set(range(total)) - received)
            raise HTTPException(status_code=400, detail=f"Missing chunks: {missing}")

        dest_dir = Path(session["dest_dir"])
        dest_file = dest_dir / session["filename"]
        tmp_dir = settings.tmp_dir / session_id  # type: ignore[operator]

        try:
            async with await anyio.open_file(dest_file, "wb") as out:
                for i in range(total):
                    chunk_path = tmp_dir / f"{i}.part"
                    async with await anyio.open_file(chunk_path, "rb") as inp:
                        while True:
                            data = await inp.read(256 * 1024)
                            if not data:
                                break
                            await out.write(data)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            _sessions.pop(session_id, None)

        return JSONResponse({"path": str(dest_file.relative_to(settings.root_dir))})

    return router


async def cleanup_stale_sessions(settings: Settings) -> None:
    """Background task: remove sessions older than SESSION_TTL."""
    while True:
        await asyncio.sleep(300)
        _cleanup_once(settings)


def _cleanup_once(settings: Settings) -> None:
    """Remove stale upload sessions. Extracted for testability."""
    now = time.monotonic()
    stale = [
        sid
        for sid, s in list(_sessions.items())
        if now - s["created_at"] > _SESSION_TTL
    ]
    for sid in stale:
        shutil.rmtree(settings.tmp_dir / sid, ignore_errors=True)  # type: ignore[operator]
        _sessions.pop(sid, None)  # type: ignore[reportUnboundVariable]
