from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from paperagent.schemas.common import stable_json_hash
from paperagent.tools.contracts import ToolResult


class ToolResultStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def externalize(self, result: ToolResult, max_inline_chars: int) -> ToolResult:
        if result.status != "success":
            return result
        encoded = json.dumps(result.content, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) <= max_inline_chars:
            return result
        assert result.content_hash is not None
        relative = Path(result.content_hash[:2]) / f"{result.content_hash}.json"
        target = (self.root / relative).resolve()
        if self.root not in target.parents:
            raise ValueError("tool result path escapes result store")
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            descriptor, temporary = tempfile.mkstemp(prefix=".paperagent-", dir=target.parent)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise
        preview = encoded[:max_inline_chars]
        return result.model_copy(
            update={
                "content": {"preview": preview, "content_hash": result.content_hash},
                "truncated": True,
                "full_result_ref": relative.as_posix(),
            }
        )

    def hydrate(self, result: ToolResult) -> ToolResult:
        """Restore an externalized result for deterministic in-process consumers.

        Externalization is a transport/context optimization.  Workflow nodes must not
        interpret the preview descriptor as the tool's semantic result.
        """
        if not result.truncated:
            return result
        if not result.full_result_ref:
            raise ValueError("truncated tool result has no full result reference")
        relative = Path(result.full_result_ref)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("tool result reference escapes result store")
        target = (self.root / relative).resolve()
        if target != self.root and self.root not in target.parents:
            raise ValueError("tool result reference escapes result store")
        content = json.loads(target.read_text(encoding="utf-8"))
        if result.content_hash is not None and stable_json_hash(content) != result.content_hash:
            raise ValueError("externalized tool result failed integrity verification")
        return result.model_copy(update={"content": content})
