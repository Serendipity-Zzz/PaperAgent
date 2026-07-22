import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import JsonValue

from paperagent.recovery import SideEffectStore
from paperagent.tools import (
    ConcurrencyPolicy,
    PermissionPolicy,
    SideEffect,
    ToolCall,
    ToolExecutionContext,
    ToolExecutor,
    ToolResult,
    ToolResultStatus,
    ToolResultStore,
    ToolSpec,
)
from paperagent.tools.adapters import CallableToolAdapter
from paperagent.tools.registry import ToolRegistry


def tool_spec(
    name: str,
    *,
    side_effect: SideEffect = SideEffect.NONE,
    concurrency: ConcurrencyPolicy = ConcurrencyPolicy.SAFE,
    max_inline_chars: int = 12_000,
    approval: bool = False,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        version="1.0.0",
        description=f"Execute {name}.",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}, "output_path": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        side_effect=side_effect,
        concurrency_policy=concurrency,
        permission_policy=(
            PermissionPolicy.REQUIRE_APPROVAL if approval else PermissionPolicy.DETERMINISTIC
        ),
        max_inline_chars=max_inline_chars,
    )


def call(name: str, arguments: dict[str, JsonValue]) -> ToolCall:
    return ToolCall(
        call_id=f"call-{uuid4()}",
        trace_id=uuid4(),
        sequence=1,
        tool_name=name,
        tool_version="1.0.0",
        arguments=arguments,
        requested_by="test_agent",
        idempotency_key=f"idem-{uuid4()}",
    )


def context(tmp_path: Path, *, approved: bool = False) -> ToolExecutionContext:
    return ToolExecutionContext(
        project_id="project-1",
        workspace=tmp_path,
        agent_type="test_agent",
        provider_capabilities={"tools"},
        approved=approved,
    )


def test_schema_path_and_permission_fail_before_adapter(tmp_path: Path) -> None:
    invoked = 0

    def handler(arguments: dict[str, JsonValue]) -> JsonValue:
        nonlocal invoked
        invoked += 1
        return arguments

    registry = ToolRegistry()
    registry.register(tool_spec("test.read"), CallableToolAdapter(handler))
    registry.register(
        tool_spec("test.paid", side_effect=SideEffect.PAID, approval=True),
        CallableToolAdapter(handler),
    )
    executor = ToolExecutor(registry, ToolResultStore(tmp_path / "results"))
    invalid = asyncio.run(executor.execute(call("test.read", {"value": "bad"}), context(tmp_path)))
    escaped = asyncio.run(
        executor.execute(
            call("test.read", {"value": 1, "output_path": "../outside.txt"}),
            context(tmp_path),
        )
    )
    approval = asyncio.run(executor.execute(call("test.paid", {"value": 1}), context(tmp_path)))
    assert invalid.status is ToolResultStatus.ERROR
    assert "TOOL_INPUT_SCHEMA" in invalid.error.message  # type: ignore[union-attr]
    assert escaped.status is ToolResultStatus.ERROR
    assert "escapes workspace" in escaped.error.message  # type: ignore[union-attr]
    assert approval.error is not None and approval.error.code == "APPROVAL_REQUIRED"
    assert invoked == 0


def test_adapter_artifact_refs_are_promoted_to_tool_result(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(
        tool_spec("test.artifact"),
        CallableToolAdapter(
            lambda _arguments: {"artifact_refs": ["artifact-1", "artifact-2"]}
        ),
    )
    result = asyncio.run(
        ToolExecutor(registry, ToolResultStore(tmp_path / "results")).execute(
            call("test.artifact", {"value": 1}), context(tmp_path)
        )
    )
    assert result.status is ToolResultStatus.SUCCESS
    assert result.artifact_refs == ["artifact-1", "artifact-2"]


def test_large_result_is_externalized_and_side_effect_is_checkpointed(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(
        tool_spec(
            "test.write",
            side_effect=SideEffect.LOCAL_WRITE,
            concurrency=ConcurrencyPolicy.EXCLUSIVE,
            max_inline_chars=20,
        ),
        CallableToolAdapter(lambda _arguments: {"text": "x" * 200}),
    )
    ledger = SideEffectStore(tmp_path / "recovery.db")
    executor = ToolExecutor(
        registry,
        ToolResultStore(tmp_path / "results"),
        side_effects=ledger,
    )
    result = asyncio.run(
        executor.execute(call("test.write", {"value": 1}), context(tmp_path, approved=True))
    )
    assert result.status is ToolResultStatus.SUCCESS
    assert result.truncated and result.full_result_ref
    assert (tmp_path / "results" / result.full_result_ref).is_file()
    records = ledger.list("project-1")
    assert len(records) == 1
    assert records[0].state == "succeeded"
    assert records[0].result["content_hash"] == result.content_hash


def test_externalized_result_can_be_hydrated_with_integrity_check(tmp_path: Path) -> None:
    store = ToolResultStore(tmp_path / "results")
    original = ToolResult(
        call_id="large-result",
        status=ToolResultStatus.SUCCESS,
        content={"document": {"title": "驻波", "body": "x" * 200}},
    )
    externalized = store.externalize(original, 20)

    assert externalized.truncated
    assert "document" not in externalized.content
    hydrated = store.hydrate(externalized)
    assert hydrated.content == original.content
    assert hydrated.content_hash == original.content_hash


def test_hydrate_rejects_tampered_externalized_result(tmp_path: Path) -> None:
    store = ToolResultStore(tmp_path / "results")
    externalized = store.externalize(
        ToolResult(
            call_id="tampered-result",
            status=ToolResultStatus.SUCCESS,
            content={"text": "x" * 200},
        ),
        20,
    )
    assert externalized.full_result_ref is not None
    (store.root / externalized.full_result_ref).write_text(
        '{"text":"tampered"}', encoding="utf-8"
    )

    with pytest.raises(ValueError, match="integrity"):
        store.hydrate(externalized)


def test_safe_calls_run_together_and_exclusive_call_is_a_barrier(tmp_path: Path) -> None:
    registry = ToolRegistry()
    active = 0
    max_active = 0
    order: list[str] = []

    async def safe_handler(arguments: dict[str, JsonValue]) -> JsonValue:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        order.append(f"safe-{arguments['value']}")
        active -= 1
        return {"value": arguments["value"]}

    async def exclusive_handler(arguments: dict[str, JsonValue]) -> JsonValue:
        assert active == 0
        order.append("exclusive")
        return {"value": arguments["value"]}

    registry.register(tool_spec("test.safe"), CallableToolAdapter(safe_handler))
    registry.register(
        tool_spec("test.exclusive", concurrency=ConcurrencyPolicy.EXCLUSIVE),
        CallableToolAdapter(exclusive_handler),
    )
    executor = ToolExecutor(registry, ToolResultStore(tmp_path / "results"))
    results = asyncio.run(
        executor.execute_many(
            [
                call("test.safe", {"value": 1}),
                call("test.safe", {"value": 2}),
                call("test.exclusive", {"value": 3}),
                call("test.safe", {"value": 4}),
            ],
            context(tmp_path),
        )
    )
    assert all(result.status is ToolResultStatus.SUCCESS for result in results)
    assert max_active == 2
    assert order.index("exclusive") > order.index("safe-1")
    assert order.index("exclusive") > order.index("safe-2")
    assert order.index("exclusive") < order.index("safe-4")


def test_side_effect_failure_updates_ledger_instead_of_leaving_running(tmp_path: Path) -> None:
    registry = ToolRegistry()

    def fail(_arguments: dict[str, JsonValue]) -> JsonValue:
        raise RuntimeError("write failed")

    registry.register(
        tool_spec("test.fail", side_effect=SideEffect.LOCAL_WRITE),
        CallableToolAdapter(fail),
    )
    ledger = SideEffectStore(tmp_path / "recovery.db")
    executor = ToolExecutor(
        registry,
        ToolResultStore(tmp_path / "results"),
        side_effects=ledger,
    )
    result = asyncio.run(
        executor.execute(call("test.fail", {"value": 1}), context(tmp_path, approved=True))
    )
    assert result.status is ToolResultStatus.ERROR
    assert ledger.list("project-1")[0].state == "failed"
