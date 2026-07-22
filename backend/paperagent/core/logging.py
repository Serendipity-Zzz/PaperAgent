from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SENSITIVE_KEY = re.compile(
    r"authorization|api[-_]?key|password|passwd|secret|token|credential", re.IGNORECASE
)
BEARER_TOKEN = re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]+")
KEY_ASSIGNMENT = re.compile(r"(?i)(api[-_]?key|password|secret|token)\s*[:=]\s*[^\s,;]+")
MAX_TEXT_LENGTH = 2_000


def redact(value: Any, *, key: str | None = None) -> Any:
    if key and SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(k): redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        text = BEARER_TOKEN.sub("Bearer [REDACTED]", value)
        text = KEY_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
        if len(text) > MAX_TEXT_LENGTH:
            return f"[LONG_TEXT_REDACTED length={len(text)}]"
        return text
    return value


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.msg)
        if record.args:
            record.args = redact(record.args)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name in ("trace_id", "task_id", "agent", "tool"):
            if hasattr(record, name):
                payload[name] = redact(getattr(record, name), key=name)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(redact(payload), ensure_ascii=False)


def configure_logging(
    *, level: str = "INFO", output_format: str = "human", log_file: Path | None = None
) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    formatter: logging.Formatter
    if output_format == "json":
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    redaction_filter = RedactionFilter()
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(redaction_filter)

    logging.basicConfig(level=level, handlers=handlers, force=True)
