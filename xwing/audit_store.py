"""Persistent audit storage for authenticated X-wing activity."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    with sqlite3.connect(path) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                username TEXT NOT NULL,
                method TEXT NOT NULL,
                path TEXT NOT NULL,
                details TEXT,
                status_code INTEGER NOT NULL,
                duration_ms REAL NOT NULL
            )
            """
        )
    try:
        path.chmod(0o600)
    except OSError:
        pass


def record_event(
    *,
    db_path: Path,
    username: str,
    method: str,
    path: str,
    details: str | None,
    status_code: int,
    duration_ms: float,
) -> None:
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO audit_events (occurred_at, username, method, path, details, status_code, duration_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                username,
                method,
                path,
                details,
                status_code,
                duration_ms,
            ),
        )


_NOISE_METHODS = ("GET", "HEAD", "OPTIONS", "PROPFIND")


def _append_activity_filters(
    clauses: list[str],
    values: list[object],
    *,
    username: str | None = None,
    since: str | None = None,
    scope: str | None = None,
) -> None:
    clauses.append("method NOT IN (?, ?, ?, ?)")
    values.extend(_NOISE_METHODS)
    if scope == "file":
        clauses.append("method NOT LIKE 'admin_%'")
    elif scope == "admin":
        clauses.append("method LIKE 'admin_%'")
    if username:
        clauses.append("username = ?")
        values.append(username)
    if since:
        clauses.append("occurred_at >= ?")
        values.append(since)


def list_events(
    db_path: Path,
    *,
    username: str | None = None,
    since: str | None = None,
    scope: str | None = None,
    limit: int = 100,
) -> list[dict]:
    clauses: list[str] = []
    values: list[object] = []
    _append_activity_filters(
        clauses, values, username=username, since=since, scope=scope
    )
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT occurred_at, username, method, path, details, status_code, duration_ms "
            f"FROM audit_events{where} ORDER BY id DESC LIMIT ?",
            (*values, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def summarize_events(
    db_path: Path,
    *,
    since: str | None = None,
    scope: str | None = None,
) -> dict:
    clauses: list[str] = []
    values: list[object] = []
    _append_activity_filters(clauses, values, since=since, scope=scope)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with sqlite3.connect(db_path) as db:
        row = db.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT username) FROM audit_events{where}",
            values,
        ).fetchone()
        users = db.execute(
            "SELECT username, COUNT(*) AS event_count "
            f"FROM audit_events{where} GROUP BY username ORDER BY event_count DESC, username",
            values,
        ).fetchall()
    return {
        "event_count": int(row[0] if row else 0),
        "active_users": int(row[1] if row else 0),
        "by_user": [{"username": user, "event_count": count} for user, count in users],
    }


def purge_events(db_path: Path, older_than_days: int) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
    with sqlite3.connect(db_path) as db:
        cursor = db.execute("DELETE FROM audit_events WHERE occurred_at < ?", (cutoff,))
        return cursor.rowcount


async def record_event_async(
    *,
    db_path: Path,
    username: str,
    method: str,
    path: str,
    details: str | None,
    status_code: int,
    duration_ms: float,
) -> None:
    await asyncio.to_thread(
        record_event,
        db_path=db_path,
        username=username,
        method=method,
        path=path,
        details=details,
        status_code=status_code,
        duration_ms=duration_ms,
    )
