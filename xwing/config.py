import hashlib
import tempfile
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, model_validator


class Settings(BaseModel):
    root_dir: Path
    listen_host: str = "127.0.0.1"
    listen_port: int = 8989
    tmp_dir: Optional[Path] = None
    max_upload_bytes: int = 10 * 1024 * 1024 * 1024  # 10 GB
    require_auth: bool = False
    user_header: str = "X-Forwarded-User"

    @model_validator(mode="after")
    def set_tmp_dir(self) -> "Settings":
        if self.tmp_dir is None:
            root_hash = hashlib.md5(str(self.root_dir.resolve()).encode()).hexdigest()[
                :8
            ]
            self.tmp_dir = (
                Path(tempfile.gettempdir())
                / f"xwing-{self.root_dir.name}-{root_hash}"
            )
        return self
