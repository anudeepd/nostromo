"""Persistent metadata for X-wing's recoverable trash."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

INDEX_NAME = ".index.json"


def _index_path(trash_dir: Path) -> Path:
    return trash_dir / INDEX_NAME


def load_transactions(trash_dir: Path, root_dir: Path) -> dict[str, dict[str, Any]]:
    """Load valid trash transactions, discarding malformed or unsafe rows."""
    path = _index_path(trash_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, list):
        return {}

    transactions: dict[str, dict[str, Any]] = {}
    root = root_dir.resolve()
    for record in raw:
        if not isinstance(record, dict):
            continue
        transaction_id = record.get("transaction_id")
        items = record.get("items")
        if (
            not isinstance(transaction_id, str)
            or not transaction_id
            or not isinstance(items, list)
        ):
            continue
        parsed_items: list[dict[str, Any]] = []
        valid = True
        for item in items:
            if not isinstance(item, dict):
                valid = False
                break
            original = item.get("original")
            trash_name = item.get("trash_name")
            kind = item.get("kind")
            if (
                not isinstance(original, str)
                or not original.startswith("/")
                or not isinstance(trash_name, str)
                or Path(trash_name).name != trash_name
                or kind not in {"file", "directory"}
            ):
                valid = False
                break
            original_path = (root / original.lstrip("/")).resolve()
            try:
                original_path.relative_to(root)
            except ValueError:
                valid = False
                break
            parsed_items.append(
                {
                    "original": original_path,
                    "trash": trash_dir / trash_name,
                    "kind": kind,
                }
            )
        if valid and parsed_items:
            transactions[transaction_id] = {
                "user": str(record.get("user") or "unknown"),
                "created": float(record.get("created") or 0),
                "items": parsed_items,
            }
    return transactions


def save_transactions(
    trash_dir: Path,
    root_dir: Path,
    transactions: dict[str, dict[str, Any]],
) -> None:
    """Atomically persist transaction metadata with private file permissions."""
    trash_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        trash_dir.chmod(0o700)
    except OSError:
        pass
    root = root_dir.resolve()
    records: list[dict[str, Any]] = []
    for transaction_id, transaction in transactions.items():
        items: list[dict[str, str]] = []
        for item in transaction.get("items", []):
            original = Path(item["original"]).resolve()
            trash = Path(item["trash"])
            try:
                rel_original = "/" + original.relative_to(root).as_posix()
            except ValueError:
                continue
            if trash.name != trash.as_posix().split("/")[-1]:
                continue
            items.append(
                {
                    "original": rel_original,
                    "trash_name": trash.name,
                    "kind": str(item.get("kind", "file")),
                }
            )
        if items:
            records.append(
                {
                    "transaction_id": transaction_id,
                    "user": str(transaction.get("user") or "unknown"),
                    "created": float(transaction.get("created") or 0),
                    "items": items,
                }
            )

    fd, temp_name = tempfile.mkstemp(prefix=".index-", suffix=".tmp", dir=trash_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(records, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, _index_path(trash_dir))
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
