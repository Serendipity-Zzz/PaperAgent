import asyncio

import pytest

from paperagent.tools.adapters import CallableToolAdapter
from paperagent.tools.builtins import builtin_tool_specs
from paperagent.tools.contracts import ToolSpec
from paperagent.tools.registry import ToolRegistry, ToolSearchQuery


async def _invoke(adapter: CallableToolAdapter) -> object:
    return await adapter.invoke({"value": 2})


def adapter() -> CallableToolAdapter:
    return CallableToolAdapter(lambda arguments: {"value": int(arguments["value"]) * 2})


def spec(version: str, *, deferred: bool = False) -> ToolSpec:
    return ToolSpec(
        name="knowledge.search",
        version=version,
        description="Search academic evidence and citations.",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        capabilities={"retrieval", "evidence"},
        search_hints=["论文 文献 证据 citation"],
        allowed_agents={"evidence_agent"},
        deferred=deferred,
    )


def test_registry_version_resolution_is_deterministic_and_conflicts_fail_closed() -> None:
    registry = ToolRegistry()
    first_adapter = adapter()
    registry.register(spec("1.0.0"), first_adapter)
    registry.register(spec("1.2.0"), first_adapter)
    registry.register(spec("2.0.0-beta.1"), first_adapter)
    selected = registry.resolve("knowledge.search", agent_type="evidence_agent")
    assert selected.spec.version == "2.0.0-beta.1"
    assert registry.resolve(
        "knowledge.search", version="1.0.0", agent_type="evidence_agent"
    ).spec.version == "1.0.0"
    assert registry.register(spec("1.0.0"), first_adapter).adapter is first_adapter
    with pytest.raises(ValueError, match="already has an adapter"):
        registry.register(spec("1.0.0"), adapter())
    changed = spec("1.0.0").model_copy(update={"description": "Different schema contract"})
    with pytest.raises(ValueError, match="schema conflict"):
        registry.register(changed, first_adapter)


def test_registry_enforces_agent_and_provider_capability_allowlists() -> None:
    registry = ToolRegistry()
    registry.register(spec("1.0.0"), adapter())
    with pytest.raises(PermissionError, match="not available"):
        registry.resolve("knowledge.search", agent_type="visual_agent")
    with pytest.raises(PermissionError, match="not available"):
        registry.resolve(
            "knowledge.search",
            agent_type="evidence_agent",
            provider_capabilities={"chat"},
        )
    with pytest.raises(KeyError, match="not registered"):
        registry.resolve("unknown.tool", agent_type="evidence_agent")


def test_deferred_search_returns_concise_descriptors_and_manifest_hides_deferred() -> None:
    registry = ToolRegistry()
    for builtin in builtin_tool_specs():
        registry.register(builtin, adapter())
    manifest = registry.manifest(
        agent_type="evidence_agent", provider_capabilities={"tools", "vision"}
    )
    assert [item.name for item in manifest] == ["file.read", "knowledge.search"]
    matches = registry.search(
        ToolSearchQuery(
            text="检查 CUDA 实验环境",
            agent_type="experiment_agent",
            provider_capabilities={"tools"},
        )
    )
    assert matches[0].descriptor.name == "experiment.assess"
    payload = matches[0].model_dump(mode="json")
    assert "input_schema" not in payload["descriptor"]
    assert payload["descriptor"]["schema_hash"]
    assert registry.schema(
        "experiment.assess", "1.0.0", agent_type="experiment_agent"
    ).deferred


def test_callable_adapter_normalizes_sync_and_async_json_results() -> None:
    assert asyncio.run(_invoke(adapter())) == {"value": 4}

    async def async_handler(arguments: dict[str, object]) -> object:
        return {"value": int(arguments["value"]) + 1}

    async_adapter = CallableToolAdapter(async_handler)  # type: ignore[arg-type]
    assert asyncio.run(_invoke(async_adapter)) == {"value": 3}


def test_registry_freezes_capability_snapshot_with_unavailable_provider_tools() -> None:
    registry = ToolRegistry()
    for builtin in builtin_tool_specs():
        registry.register(builtin, adapter())
    snapshot = registry.capability_snapshot(
        agent_types={"evidence_agent", "visual_agent"},
        provider_capabilities={"tools"},
    )
    by_name = {item.name: item for item in snapshot.descriptors}
    assert snapshot.snapshot_hash
    assert by_name["knowledge.search"].available
    assert not by_name["visual.generate"].available
