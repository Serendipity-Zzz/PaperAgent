from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

from jsonschema import Draft202012Validator
from pydantic import JsonValue

from paperagent.execution.contracts import AuthorizationGrant
from paperagent.recovery.service import SideEffectAction, SideEffectState, SideEffectStore
from paperagent.tools.contracts import (
    ConcurrencyPolicy,
    SideEffect,
    ToolCall,
    ToolError,
    ToolResult,
    ToolResultStatus,
)
from paperagent.tools.hooks import NoopToolHooks, ToolHooks
from paperagent.tools.permissions import (
    DeterministicPermissionEvaluator,
    PermissionEvaluator,
    PermissionOutcome,
)
from paperagent.tools.registry import RegisteredTool, ToolRegistry
from paperagent.tools.result_store import ToolResultStore


@dataclass(frozen=True)
class ToolExecutionContext:
    project_id: str
    workspace: Path
    agent_type: str
    provider_capabilities: set[str]
    approved: bool = False
    authorization_grant: AuthorizationGrant | None = None


class ToolPipelineError(ValueError):
    def __init__(self, code: str, message: str, category: str) -> None:
        super().__init__(message)
        self.code = code
        self.category = category


@runtime_checkable
class InputConcurrencyAware(Protocol):
    def is_concurrency_safe(self, arguments: dict[str, JsonValue]) -> bool: ...


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        result_store: ToolResultStore,
        *,
        permissions: PermissionEvaluator | None = None,
        hooks: ToolHooks | None = None,
        side_effects: SideEffectStore | None = None,
        max_concurrency: int = 10,
    ) -> None:
        self.registry = registry
        self.result_store = result_store
        self.permissions = permissions or DeterministicPermissionEvaluator()
        self.hooks = hooks or NoopToolHooks()
        self.side_effects = side_effects
        self.max_concurrency = max_concurrency

    async def execute(self, call: ToolCall, context: ToolExecutionContext) -> ToolResult:
        ledger_id: str | None = None
        try:
            registered = self.registry.resolve(
                call.tool_name,
                version=call.tool_version,
                agent_type=context.agent_type,
                provider_capabilities=context.provider_capabilities,
            )
            self._validate_schema(call.arguments, registered.spec.input_schema, "TOOL_INPUT_SCHEMA")
            self._validate_paths(call.arguments, context.workspace.resolve())
            decision = await self.permissions.evaluate(
                call,
                registered.spec,
                approved=context.approved,
                grant=context.authorization_grant,
            )
            if decision.outcome is not PermissionOutcome.ALLOW:
                code = (
                    "APPROVAL_REQUIRED"
                    if decision.outcome is PermissionOutcome.REQUIRE_APPROVAL
                    else "PERMISSION_DENIED"
                )
                return self._failure(
                    call,
                    code,
                    decision.reason,
                    "permission",
                    status=ToolResultStatus.DENIED,
                )
            await self.hooks.before(call, registered.spec)
            ledger_id = self._intent(call, context, registered)
            if ledger_id and self.side_effects:
                self.side_effects.transition(ledger_id, SideEffectState.RUNNING)
            content = await registered.adapter.invoke(call.arguments)
            if registered.spec.output_schema is not None:
                self._validate_schema(
                    content, registered.spec.output_schema, "TOOL_OUTPUT_SCHEMA"
                )
            result = ToolResult(
                call_id=call.call_id,
                status=ToolResultStatus.SUCCESS,
                content=content,
                artifact_refs=self._artifact_refs(content),
            )
            result = self.result_store.externalize(result, registered.spec.max_inline_chars)
            if ledger_id and self.side_effects:
                self.side_effects.transition(
                    ledger_id,
                    SideEffectState.SUCCEEDED,
                    result={"content_hash": result.content_hash, "refs": result.artifact_refs},
                    checkpoint=result.full_result_ref,
                )
            await self.hooks.after(call, registered.spec, result)
            return result
        except Exception as error:
            state_unknown = isinstance(error, (TimeoutError, ConnectionError))
            if ledger_id and self.side_effects:
                try:
                    self.side_effects.transition(
                        ledger_id,
                        SideEffectState.UNKNOWN if state_unknown else SideEffectState.FAILED,
                        result={"error_type": error.__class__.__name__},
                    )
                except (KeyError, ValueError):
                    state_unknown = True
            code = error.code if isinstance(error, ToolPipelineError) else "TOOL_EXECUTION_ERROR"
            category = error.category if isinstance(error, ToolPipelineError) else "execution"
            return self._failure(
                call,
                code,
                str(error)[:2_000] or error.__class__.__name__,
                category,
                state_unknown=state_unknown,
            )

    @staticmethod
    def _artifact_refs(content: JsonValue) -> list[str]:
        if not isinstance(content, dict):
            return []
        raw = content.get("artifact_refs", [])
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, str)]

    async def execute_many(
        self, calls: list[ToolCall], context: ToolExecutionContext
    ) -> list[ToolResult]:
        results: list[ToolResult] = []
        safe_batch: list[ToolCall] = []

        async def flush() -> None:
            if not safe_batch:
                return
            semaphore = asyncio.Semaphore(self.max_concurrency)

            async def limited(item: ToolCall) -> ToolResult:
                async with semaphore:
                    return await self.execute(item, context)

            results.extend(await asyncio.gather(*(limited(item) for item in safe_batch)))
            safe_batch.clear()

        for call in calls:
            registered = self.registry.resolve(
                call.tool_name,
                version=call.tool_version,
                agent_type=context.agent_type,
                provider_capabilities=context.provider_capabilities,
            )
            if self._concurrency_safe(registered, call.arguments):
                safe_batch.append(call)
                continue
            await flush()
            results.append(await self.execute(call, context))
        await flush()
        return results

    @staticmethod
    def _concurrency_safe(tool: RegisteredTool, arguments: dict[str, JsonValue]) -> bool:
        if tool.spec.concurrency_policy is ConcurrencyPolicy.SAFE:
            return True
        if tool.spec.concurrency_policy is not ConcurrencyPolicy.INPUT_DEPENDENT:
            return False
        if not isinstance(tool.adapter, InputConcurrencyAware):
            return False
        try:
            return tool.adapter.is_concurrency_safe(arguments)
        except Exception:
            return False

    @staticmethod
    def _validate_schema(value: JsonValue, schema: dict[str, JsonValue], code: str) -> None:
        validator = Draft202012Validator(cast(dict[str, object], schema))
        errors = sorted(validator.iter_errors(value), key=lambda item: list(item.path))
        if errors:
            error = errors[0]
            path = ".".join(str(item) for item in error.path) or "$"
            raise ToolPipelineError(code, f"{code} at {path}: {error.message}", "schema")

    @classmethod
    def _validate_paths(cls, value: JsonValue, workspace: Path, key: str = "") -> None:
        if isinstance(value, dict):
            for name, child in value.items():
                cls._validate_paths(child, workspace, name)
        elif isinstance(value, list):
            for child in value:
                cls._validate_paths(child, workspace, key)
        elif isinstance(value, str) and (key.endswith("path") or key == "repository"):
            target = Path(value)
            target = target.resolve() if target.is_absolute() else (workspace / target).resolve()
            if target != workspace and workspace not in target.parents:
                raise ToolPipelineError(
                    "TOOL_PATH_VIOLATION", f"path escapes workspace: {key}", "path"
                )

    def _intent(
        self, call: ToolCall, context: ToolExecutionContext, tool: RegisteredTool
    ) -> str | None:
        if tool.spec.side_effect is SideEffect.NONE or self.side_effects is None:
            return None
        action = {
            SideEffect.LOCAL_WRITE: SideEffectAction.FILE,
            SideEffect.EXTERNAL: SideEffectAction.API,
            SideEffect.PAID: SideEffectAction.API,
            SideEffect.DESTRUCTIVE: SideEffectAction.DELETE,
        }[tool.spec.side_effect]
        record = self.side_effects.intent(
            context.project_id,
            action,
            call.idempotency_key,
            f"tool:{tool.spec.name}@{tool.spec.version}",
            scope=cast(dict[str, object], call.arguments),
            paid=tool.spec.side_effect is SideEffect.PAID,
            requires_approval=tool.spec.permission_policy.value == "require_approval",
            request_id=call.call_id,
        )
        return record.id

    @staticmethod
    def _failure(
        call: ToolCall,
        code: str,
        message: str,
        category: str,
        *,
        state_unknown: bool = False,
        status: ToolResultStatus | None = None,
    ) -> ToolResult:
        return ToolResult(
            call_id=call.call_id,
            status=status
            or (ToolResultStatus.UNKNOWN if state_unknown else ToolResultStatus.ERROR),
            error=ToolError(
                code=code,
                message=message,
                category=category,
                retryable=False,
                state_unknown=state_unknown,
            ),
        )
