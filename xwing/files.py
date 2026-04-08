from pathlib import Path


def safe_path(root: Path, rel: str) -> Path:
    """Resolve a user-supplied relative path under root, rejecting traversal."""
    # Strip leading slashes so Path doesn't treat it as absolute
    cleaned = rel.lstrip("/")
    resolved = (root / cleaned).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        raise PermissionError(f"Path escapes root: {rel!r}")
    return resolved


def list_dir(path: Path) -> list[dict]:
    """Return sorted directory entries as dicts suitable for templates."""
    entries = []
    for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        stat = child.stat()
        is_dir = child.is_dir()
        entries.append(
            {
                "name": child.name,
                "is_dir": is_dir,
                "size": stat.st_size,
                "size_human": "" if is_dir else human_size(stat.st_size),
                "mtime": stat.st_mtime,
                "editable": (not is_dir)
                and stat.st_size <= _EDITABLE_MAX
                and is_editable(child),
            }
        )
    return entries


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
_EDITABLE_MAX = 2 * 1024 * 1024  # 2 MB


def is_editable(path: Path) -> bool:
    """True if the file should be opened in the browser editor."""
    if path.name == ".env" or path.name.startswith(".env."):
        return False
    if path.stat().st_size > _EDITABLE_MAX:
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
