from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from pydantic import Field, JsonValue

from paperagent.execution.contracts import (
    CapabilityDescriptor,
    CapabilityKind,
    CapabilitySnapshot,
)
from paperagent.schemas.common import StrictModel
from paperagent.tools.contracts import ToolSpec

_TOKEN = re.compile(r"[a-z0-9_.-]+|[\u3400-\u9fff]+", re.I)


class ToolAdapter(Protocol):
    async def invoke(self, arguments: dict[str, JsonValue]) -> JsonValue: ...


@dataclass(frozen=True)
class RegisteredTool:
    spec: ToolSpec
    adapter: ToolAdapter


class ToolDescriptor(StrictModel):
    name: str
    version: str
    description: str
    capabilities: list[str]
    required_input: list[str]
    side_effect: str
    source: str
    schema_hash: str
    deferred: bool


class ToolSearchQuery(StrictModel):
    text: str = Field(min_length=1, max_length=2_000)
    agent_type: str
    provider_capabilities: set[str] = Field(default_factory=lambda: {"tools"})
    limit: int = Field(default=8, ge=1, le=50)
    include_deferred: bool = True


class ToolMatch(StrictModel):
    descriptor: ToolDescriptor
    score: float = Field(ge=0)
    reasons: list[str]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[tuple[str, str], RegisteredTool] = {}

    def register(self, spec: ToolSpec, adapter: ToolAdapter) -> RegisteredTool:
        key = (spec.name, spec.version)
        existing = self._tools.get(key)
        if existing is not None:
            if existing.spec.schema_hash() != spec.schema_hash():
                raise ValueError(f"tool version schema conflict: {spec.name}@{spec.version}")
            if existing.adapter is not adapter:
                raise ValueError(f"tool version already has an adapter: {spec.name}@{spec.version}")
            return existing
        record = RegisteredTool(spec=spec, adapter=adapter)
        self._tools[key] = record
        return record

    def resolve(
        self,
        name: str,
        *,
        version: str | None = None,
        agent_type: str,
        provider_capabilities: set[str] | None = None,
    ) -> RegisteredTool:
        candidates = [
            record
            for (tool_name, tool_version), record in self._tools.items()
            if tool_name == name and (version is None or tool_version == version)
        ]
        if not candidates:
            suffix = f"@{version}" if version else ""
            raise KeyError(f"tool not registered: {name}{suffix}")
        allowed = [
            record
            for record in candidates
            if self._allowed(record.spec, agent_type, provider_capabilities or {"tools"})
        ]
        if not allowed:
            raise PermissionError(f"tool is not available to agent/provider: {name}")
        return max(allowed, key=lambda record: self._version_key(record.spec.version))

    def search(self, query: ToolSearchQuery) -> list[ToolMatch]:
        query_tokens = self._tokens(query.text)
        matches: list[ToolMatch] = []
        for record in self._latest_versions():
            spec = record.spec
            if spec.deferred and not query.include_deferred:
                continue
            if not self._allowed(spec, query.agent_type, query.provider_capabilities):
                continue
            fields = {
                "name": self._tokens(spec.name),
                "capability": self._tokens(" ".join(sorted(spec.capabilities))),
                "hint": self._tokens(" ".join(spec.search_hints)),
                "description": self._tokens(spec.description),
            }
            score = 0.0
            reasons: list[str] = []
            for field, tokens in fields.items():
                overlap = query_tokens & tokens
                if not overlap:
                    continue
                weight = {"name": 5.0, "capability": 4.0, "hint": 3.0, "description": 1.0}[
                    field
                ]
                score += weight * len(overlap)
                reasons.append(f"{field}:{','.join(sorted(overlap))}")
            if not score:
                continue
            matches.append(
                ToolMatch(descriptor=self._descriptor(spec), score=score, reasons=reasons)
            )
        return sorted(
            matches,
            key=lambda match: (-match.score, match.descriptor.name, match.descriptor.version),
        )[: query.limit]

    def manifest(
        self,
        *,
        agent_type: str,
        provider_capabilities: set[str],
        include_deferred: bool = False,
    ) -> list[ToolDescriptor]:
        return [
            self._descriptor(record.spec)
            for record in self._latest_versions()
            if (include_deferred or not record.spec.deferred)
            and self._allowed(record.spec, agent_type, provider_capabilities)
        ]

    def schema(self, name: str, version: str, *, agent_type: str) -> ToolSpec:
        return self.resolve(name, version=version, agent_type=agent_type).spec

    def capability_snapshot(
        self,
        *,
        agent_types: set[str] | None = None,
        provider_capabilities: set[str] | None = None,
    ) -> CapabilitySnapshot:
        providers = provider_capabilities or {"tools"}
        descriptors: list[CapabilityDescriptor] = []
        for record in self._latest_versions():
            spec = record.spec
            available_to = spec.allowed_agents or (agent_types or set())
            if agent_types is not None:
                available_to = available_to & agent_types
            provider_ready = spec.required_provider_capabilities <= providers
            provider_requirements: list[JsonValue] = [
                str(item) for item in sorted(spec.required_provider_capabilities)
            ]
            descriptors.append(
                CapabilityDescriptor(
                    name=spec.name,
                    version=spec.version,
                    kind=CapabilityKind.TOOL,
                    input_types={str(spec.input_schema.get("type", "object"))},
                    output_types=(
                        {str(spec.output_schema.get("type", "object"))}
                        if spec.output_schema
                        else set()
                    ),
                    tags=set(spec.capabilities),
                    side_effect=spec.side_effect.value,
                    permission_policy=spec.permission_policy.value,
                    allowed_agents=set(available_to),
                    resource_requirements={
                        "provider_capabilities": provider_requirements
                    },
                    available=provider_ready,
                    unavailable_reason=(
                        None
                        if provider_ready
                        else "provider does not satisfy required capabilities"
                    ),
                )
            )
        return CapabilitySnapshot(descriptors=descriptors)

    def _latest_versions(self) -> list[RegisteredTool]:
        names = sorted({name for name, _version in self._tools})
        return [
            max(
                (
                    record
                    for (tool_name, _version), record in self._tools.items()
                    if tool_name == name
                ),
                key=lambda record: self._version_key(record.spec.version),
            )
            for name in names
        ]

    @staticmethod
    def _allowed(spec: ToolSpec, agent_type: str, provider_capabilities: set[str]) -> bool:
        return (
            (not spec.allowed_agents or agent_type in spec.allowed_agents)
            and spec.required_provider_capabilities <= provider_capabilities
        )

    @staticmethod
    def _descriptor(spec: ToolSpec) -> ToolDescriptor:
        required = spec.input_schema.get("required", [])
        required_input = [str(item) for item in required] if isinstance(required, list) else []
        return ToolDescriptor(
            name=spec.name,
            version=spec.version,
            description=spec.description,
            capabilities=sorted(spec.capabilities),
            required_input=required_input,
            side_effect=spec.side_effect.value,
            source=spec.source,
            schema_hash=spec.schema_hash(),
            deferred=spec.deferred,
        )

    @staticmethod
    def _tokens(value: str) -> set[str]:
        tokens: set[str] = set()
        for match in _TOKEN.finditer(value):
            token = match.group(0).casefold()
            tokens.add(token)
            if all("\u3400" <= char <= "\u9fff" for char in token):
                tokens.update(token)
                tokens.update(token[index : index + 2] for index in range(len(token) - 1))
        return tokens

    @staticmethod
    def _version_key(version: str) -> tuple[int, int, int, int, str]:
        match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+]([A-Za-z0-9.-]+))?", version)
        if not match:
            raise ValueError(f"invalid tool version: {version}")
        major, minor, patch = (int(match.group(index)) for index in range(1, 4))
        suffix = match.group(4) or ""
        return major, minor, patch, int(not suffix), suffix
