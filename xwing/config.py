import hashlib
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Security note: yaml.load() with the default Loader can construct arbitrary
# Python objects from YAML tags (e.g. !!python/object/apply), which is a remote
# code execution risk if config files are user-supplied or network-accessible.
# We always pass Loader=yaml.SafeLoader, which only constructs basic Python types
# (str, int, float, bool, list, dict, None) and raises on any other tag.
import yaml
from pydantic import BaseModel, PrivateAttr, model_validator

logger = logging.getLogger(__name__)

# Chunked upload defaults
DEFAULT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024 * 1024  # 10 GB
DEFAULT_MAX_CHUNK_SIZE = 100 * 1024 * 1024  # 100 MB per chunk
DEFAULT_MAX_CHUNKS = 10_000
DEFAULT_SESSION_TTL_SECONDS = 3600  # 1 hour
DEFAULT_CHUNK_READ_SIZE = 256 * 1024  # 256 KB


@dataclass
class UserPerms:
    read: bool = True
    write: bool = False
    delete: bool = False


_DEFAULT_PERMS = UserPerms(read=True, write=False, delete=False)


class UserConfig:
    """Per-user permission table loaded from a YAML config file.

    Compact format (recommended):
        users:
          alice: rwd     # read + write + delete
          bob: rw        # read + write, no delete
          "*": r         # fallback for any unlisted user

    Verbose format:
        users:
          alice:
            read: true
            write: true
            delete: true

    Verbose format field defaults (when a key is omitted):
        read   — defaults to true  (omitting it grants read access)
        write  — defaults to false (omitting it denies write access)
        delete — defaults to false (omitting it denies delete access)

    Compact format: only the characters 'r', 'w', 'd' are accepted.
    Any other character raises ValueError at load time.
    Unlisted users and the '*' wildcard fall back to read-only (_DEFAULT_PERMS).
    """

    def __init__(self, path: Path) -> None:
        try:
            raw = yaml.load(path.read_text(), Loader=yaml.SafeLoader) or {}
        except FileNotFoundError:
            raise ValueError(f"Users config file not found: {path}") from None
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid YAML in users config {path}: {exc}") from exc

        if not isinstance(raw, dict) or "users" not in raw:
            raise ValueError(
                f"Users config {path} has no 'users' key. "
                "Check your config file structure."
            )

        entries = raw.get("users", {}) or {}
        self._perms: dict[str, UserPerms] = {
            k.lower(): self._parse(k, v) for k, v in entries.items()
        }

    @staticmethod
    def _parse(username: str, v: str | dict) -> UserPerms:
        if isinstance(v, str):
            invalid = set(v) - {"r", "w", "d"}
            if invalid:
                raise ValueError(
                    f"Invalid permission string {v!r} for user {username!r}: "
                    f"only 'r', 'w', 'd' are valid characters"
                )
            return UserPerms(read="r" in v, write="w" in v, delete="d" in v)
        perms = {}
        for field, default in (("read", True), ("write", False), ("delete", False)):
            val = v.get(field, default)
            if not isinstance(val, bool):
                raise ValueError(
                    f"Field {field!r} for user {username!r} must be true or false, got {val!r}"
                )
            perms[field] = val
        return UserPerms(**perms)

    def get(self, user: str) -> UserPerms:
        key = user.lower()
        return self._perms.get(key) or self._perms.get("*") or _DEFAULT_PERMS


class Settings(BaseModel):
    root_dir: Path
    listen_host: str = "127.0.0.1"
    listen_port: int = 8989
    tmp_dir: Optional[Path] = None
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    max_chunk_bytes: int = DEFAULT_MAX_CHUNK_SIZE
    max_chunks: int = DEFAULT_MAX_CHUNKS
    session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS
    chunk_read_size: int = DEFAULT_CHUNK_READ_SIZE
    require_auth: bool = False
    user_header: str = "X-Forwarded-User"
    users_config: Optional[Path] = None

    _user_config: Optional[UserConfig] = PrivateAttr(default=None)
    _config_mtime: float = PrivateAttr(default=0.0)

    @model_validator(mode="after")
    def _init(self) -> "Settings":
        if self.users_config is not None:
            self._user_config = UserConfig(self.users_config)
            self._config_mtime = self.users_config.stat().st_mtime
        if self.tmp_dir is None:
            salt = os.urandom(4).hex()
            root_str = str(self.root_dir.resolve())
            root_hash = hashlib.sha256((salt + root_str).encode()).hexdigest()[:12]
            self.tmp_dir = (
                Path(tempfile.gettempdir()) / f"xwing-{self.root_dir.name}-{root_hash}"
            )
        return self

    def perms_for(self, user: str) -> UserPerms:
        if self.users_config is not None:
            try:
                mtime = self.users_config.stat().st_mtime
                if mtime != self._config_mtime:
                    logger.info("Users config changed, reloading %s", self.users_config)
                    self._user_config = UserConfig(self.users_config)
                    self._config_mtime = mtime
            except OSError as e:
                logger.warning("Could not stat users config %s: %s — using cached permissions", self.users_config, e)
            if self._user_config is not None:
                return self._user_config.get(user)
        return _DEFAULT_PERMS
