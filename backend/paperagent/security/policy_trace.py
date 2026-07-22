from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import Field

from paperagent.schemas.common import StrictModel


class PolicyTrace(StrictModel):
    trace_id: UUID = Field(default_factory=uuid4)
    tool_name: str
    call_id: str
    stages: list[str]
    outcome: str
    reason: str
    shadow_outcome: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
