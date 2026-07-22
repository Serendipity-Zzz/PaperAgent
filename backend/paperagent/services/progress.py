from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from paperagent.db.models import EventRecord
from paperagent.services.tasks import TaskService

_SECRET = re.compile(r"\b(?:sk|key|token)-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE)
_HIDDEN_KEYS = {
    "api_key",
    "authorization",
    "chain_of_thought",
    "credential",
    "hidden_reasoning",
    "password",
    "prompt",
    "raw_prompt",
    "secret",
    "system_prompt",
}


def public_payload(value: object, key: str | None = None) -> object:
    """Remove credentials and hidden reasoning before an event reaches UI or SSE."""
    if key is not None and key.casefold() in _HIDDEN_KEYS:
        return "[REDACTED]"
    if isinstance(value, str):
        return _SECRET.sub("[REDACTED]", value)
    if isinstance(value, dict):
        return {str(name): public_payload(item, str(name)) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [public_payload(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


class ProgressSink(Protocol):
    def emit(
        self,
        *,
        project_id: str,
        run_id: str,
        event_type: str,
        payload: dict[str, object],
        event_id: str | None = None,
        internal_payload_ref: str | None = None,
    ) -> EventRecord: ...


@dataclass(slots=True)
class DurableProgressSink:
    tasks: TaskService

    def emit(
        self,
        *,
        project_id: str,
        run_id: str,
        event_type: str,
        payload: dict[str, object],
        event_id: str | None = None,
        internal_payload_ref: str | None = None,
    ) -> EventRecord:
        safe = public_payload(payload)
        assert isinstance(safe, dict)
        return self.tasks.append_event(
            project_id,
            run_id,
            event_type,
            safe,
            event_id=event_id,
            internal_payload_ref=internal_payload_ref,
        )
