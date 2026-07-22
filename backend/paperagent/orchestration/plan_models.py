from __future__ import annotations

from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4

from pydantic import Field, JsonValue, model_validator

from paperagent.engine.budgets import BudgetLimits
from paperagent.schemas.common import SCHEMA_VERSION, StrictModel, stable_json_hash


class CandidateEdgeCondition(StrEnum):
    ON_SUCCESS = "on_success"
    ALWAYS = "always"
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_INPUT = "needs_input"
    REPAIR_REQUIRED = "repair_required"


class ApprovalRequirement(StrictModel):
    action: str = Field(min_length=1, max_length=255)
    risk: str = Field(min_length=1, max_length=2_000)
    consequence: str = Field(min_length=1, max_length=2_000)
    scope: dict[str, JsonValue] = Field(default_factory=dict)


class CandidateNode(StrictModel):
    node_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,127}$")
    agent_type: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    objective: str = Field(min_length=1, max_length=4_000)
    input_refs: list[str] = Field(default_factory=list)
    output_keys: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    allow_parallel: bool = False
    max_attempts: int = Field(default=3, ge=1, le=20)
    timeout_ms: int = Field(default=300_000, gt=0)
    approval: ApprovalRequirement | None = None
    success_criteria: list[str] = Field(default_factory=list)


class CandidateEdge(StrictModel):
    source: str
    target: str
    condition: CandidateEdgeCondition = CandidateEdgeCondition.ON_SUCCESS


class CandidatePlan(StrictModel):
    schema_version: str = SCHEMA_VERSION
    plan_id: UUID = Field(default_factory=uuid4)
    requirement_id: UUID
    requirement_version: int = Field(ge=1)
    entry_node: str
    terminal_nodes: set[str] = Field(min_length=1)
    nodes: list[CandidateNode] = Field(min_length=1)
    edges: list[CandidateEdge] = Field(default_factory=list)
    limits: BudgetLimits
    max_repair_rounds: int = Field(default=3, ge=0, le=10)
    rationale: str = Field(min_length=1, max_length=8_000)
    assumptions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        node_ids = [node.node_id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("candidate plan contains duplicate node ids")
        known = set(node_ids)
        if self.entry_node not in known or not self.terminal_nodes <= known:
            raise ValueError("candidate plan entry or terminal node is unknown")
        if any(edge.source not in known or edge.target not in known for edge in self.edges):
            raise ValueError("candidate plan edge references an unknown node")
        return self

    def stable_hash(self) -> str:
        return stable_json_hash(self.model_dump(mode="json", exclude={"plan_id"}))
