from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from paperagent.schemas.common import SCHEMA_VERSION, StrictModel


class BudgetLimits(StrictModel):
    max_input_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    max_tool_calls: int = Field(default=20, ge=0, le=1_000)
    max_tool_output_chars: int = Field(default=120_000, ge=0)
    max_elapsed_ms: int = Field(default=300_000, gt=0)
    max_cost: float | None = Field(default=None, ge=0)


class BudgetUsage(StrictModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    tool_output_chars: int = Field(default=0, ge=0)
    elapsed_ms: int = Field(default=0, ge=0)
    estimated_cost: float = Field(default=0, ge=0)


class BudgetDecision(StrictModel):
    schema_version: str = SCHEMA_VERSION
    limits: BudgetLimits
    usage: BudgetUsage = Field(default_factory=BudgetUsage)
    selected_context_ids: list[str] = Field(default_factory=list)
    omitted_context_ids: list[str] = Field(default_factory=list)
    selected_tool_names: list[str] = Field(default_factory=list)
    frozen: bool = False
    reason: str = ""

    @model_validator(mode="after")
    def validate_usage(self) -> Self:
        exceeded: list[str] = []
        pairs = (
            ("input_tokens", self.usage.input_tokens, self.limits.max_input_tokens),
            ("output_tokens", self.usage.output_tokens, self.limits.max_output_tokens),
            ("tool_calls", self.usage.tool_calls, self.limits.max_tool_calls),
            (
                "tool_output_chars",
                self.usage.tool_output_chars,
                self.limits.max_tool_output_chars,
            ),
            ("elapsed_ms", self.usage.elapsed_ms, self.limits.max_elapsed_ms),
        )
        exceeded.extend(name for name, used, limit in pairs if used > limit)
        if self.limits.max_cost is not None and self.usage.estimated_cost > self.limits.max_cost:
            exceeded.append("estimated_cost")
        if exceeded and not self.reason:
            raise ValueError(f"budget exceeded without decision reason: {sorted(exceeded)}")
        return self
