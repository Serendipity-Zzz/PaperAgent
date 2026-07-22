from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

SAFE_NAME = re.compile(r"[^\w.()\[\] -]+", re.UNICODE)
ALLOWED_CATEGORIES = {"sources", "artifacts", "versions", "workspaces"}


@dataclass(frozen=True)
class StoredFile:
    file_id: str
    original_name: str
    relative_path: str
    sha256: str
    size_bytes: int
    provenance: dict[str, str]


class ProjectFileStore:
    def __init__(self, project_root: Path) -> None:
        self.root = project_root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def write(
        self, *, category: str, name: str, content: bytes, provenance: dict[str, str]
    ) -> StoredFile:
        if category not in ALLOWED_CATEGORIES:
            raise ValueError("Unsupported file category")
        original_name = Path(name).name
        if original_name != name or name in {"", ".", ".."}:
            raise ValueError("Unsafe file name")
        safe_name = SAFE_NAME.sub("_", original_name).strip(" .") or "file"
        digest = hashlib.sha256(content).hexdigest()
        file_id = str(uuid4())
        relative = Path(category) / digest[:2] / f"{file_id}-{safe_name}"
        target = (self.root / relative).resolve()
        if self.root not in target.parents:
            raise ValueError("Path escapes project root")
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".paperagent-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, target)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
        return StoredFile(
            file_id=file_id,
            original_name=original_name,
            relative_path=relative.as_posix(),
            sha256=digest,
            size_bytes=len(content),
            provenance=dict(provenance),
        )

    def resolve(self, relative_path: str) -> Path:
        target = (self.root / relative_path).resolve()
        if self.root not in target.parents:
            raise ValueError("Path escapes project root")
        return target
