import os
from pathlib import Path
from urllib.parse import quote

_IGNORED_SYSTEM_NAMES = {
    ".ds_store",
    "thumbs.db",
    "desktop.ini",
    "ehthumbs.db",
    "__macosx",
    ".spotlight-v100",
    ".temporaryitems",
    ".trashes",
    ".fseventsd",
}


def is_within_root(root: Path, path: Path) -> bool:
    """True if path resolves under root."""
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def safe_path(root: Path, rel: str) -> Path:
    """Resolve a user-supplied relative path under root, rejecting traversal."""
    # Strip leading slashes so Path doesn't treat it as absolute
    cleaned = rel.lstrip("/")
    resolved = (root / cleaned).resolve()
    if not is_within_root(root, resolved):
        raise PermissionError(f"Path escapes root: {rel!r}")
    return resolved


def is_ignored_system_file(path: Path | str) -> bool:
    """True for OS metadata files that should not be stored or shown."""
    name = Path(path).name
    if not name:
        return False
    lowered = name.lower()
    return lowered in _IGNORED_SYSTEM_NAMES or name.startswith("._")


def list_dir(path: Path) -> list[dict]:
    """Return sorted directory entries as dicts suitable for templates.

    Raises:
        PermissionError: If directory cannot be accessed
        FileNotFoundError: If directory doesn't exist
    """
    try:
        entries = []
        # DirEntry caches metadata supplied by the directory scan. Keeping the
        # complete snapshot in this synchronous function also makes it safe to
        # run the whole operation in a worker thread from the request handler.
        with os.scandir(path) as scan:
            children = list(scan)
        children.sort(key=lambda entry: (not entry.is_dir(), entry.name.lower()))
        for child in children:
            if is_ignored_system_file(child.name):
                continue
            try:
                stat = child.stat()
                is_dir = child.is_dir()
                child_path = Path(child.path)
                entries.append(
                    {
                        "name": child.name,
                        "url_name": quote(child.name, safe=""),
                        "is_dir": is_dir,
                        "size": stat.st_size,
                        "size_human": "" if is_dir else human_size(stat.st_size),
                        "mtime": stat.st_mtime,
                        "editable": (not is_dir) and is_editable(child_path),
                    }
                )
            except PermissionError:
                # Skip files we can't access - still include directory itself
                entries.append(
                    {
                        "name": child.name,
                        "url_name": quote(child.name, safe=""),
                        "is_dir": child.is_dir(),
                        "size": 0,
                        "size_human": "",
                        "mtime": 0,
                        "editable": False,
                    }
                )
        return entries
    except PermissionError:
        raise


_EDITABLE_EXTS = {
    ".txt",
    ".md",
    ".rst",
    ".csv",
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".html",
    ".htm",
    ".css",
    ".scss",
    ".less",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".conf",
    ".cfg",
    ".sh",
    ".bash",
    ".zsh",
    ".fish",
    ".sql",
    ".xml",
    ".dockerfile",
    ".nginx",
    ".gitignore",
    ".gitattributes",
    ".editorconfig",
    ".log",
}


# Files at or under this size open fully editable in the browser editor.
# Larger files open as a read-only preview of the first EDITOR_PREVIEW_BYTES
# so a huge file can't OOM the server worker or freeze the browser tab.
EDITOR_FULL_EDIT_MAX = 32 * 1024 * 1024  # 32 MB
EDITOR_PREVIEW_BYTES = 1024 * 1024  # 1 MB


def is_editable(path: Path) -> bool:
    """True if the file should be opened in the browser editor."""
    if path.name == ".env" or path.name.startswith(".env."):
        return False
    if path.suffix.lower() in _EDITABLE_EXTS:
        return True
    return not path.suffix


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} PB"
