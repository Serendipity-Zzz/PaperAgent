from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import monotonic
from uuid import UUID, uuid4

from pydantic import Field

from paperagent.providers import Capability, ChatMessage, ChatRequest, ProviderToolCall, Usage
from paperagent.providers.routing import ProviderRouter, RouteDecision
from paperagent.schemas.common import SCHEMA_VERSION, StrictModel
from paperagent.tools import (
    ToolCall,
    ToolExecutionContext,
    ToolExecutor,
    ToolRegistry,
    ToolResult,
    ToolResultStatus,
)


class AgentLoopRequest(StrictModel):
    schema_version: str = SCHEMA_VERSION
    trace_id: UUID = Field(default_factory=uuid4)
    project_id: str
    agent_type: str
    messages: list[ChatMessage] = Field(min_length=1)
    tool_names: list[str] = Field(default_factory=list)
    max_rounds: int = Field(default=8, ge=1, le=50)
    max_tool_calls: int = Field(default=20, ge=0, le=1_000)
    max_total_input_tokens: int | None = Field(default=None, gt=0)
    max_total_output_tokens: int | None = Field(default=None, gt=0)
    max_elapsed_ms: int = Field(default=300_000, gt=0)
    max_cost: float | None = Field(default=None, ge=0)
    max_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0, le=2)
    response_schema: dict[str, object] | None = None
    required_successful_tools: list[str] = Field(default_factory=list)
    approved: bool = False


class AgentLoopResult(StrictModel):
    schema_version: str = SCHEMA_VERSION
    content: str
    messages: list[ChatMessage]
    rounds: int = Field(ge=1)
    tool_call_count: int = Field(ge=0)
    usage: Usage
    routes: list[str]
    finish_reason: str


class AgentLoopLimitError(RuntimeError):
    def __init__(self, message: str, messages: list[ChatMessage]) -> None:
        super().__init__(message)
        self.messages = messages


class AgentLoop:
    def __init__(
        self,
        router: ProviderRouter,
        registry: ToolRegistry,
        executor: ToolExecutor,
        workspace: Path,
        control_hook: Callable[[], Awaitable[None]] | None = None,
        guidance_hook: Callable[[], Awaitable[list[ChatMessage]]] | None = None,
    ) -> None:
        self.router = router
        self.registry = registry
        self.executor = executor
        self.workspace = workspace.resolve()
        self.control_hook = control_hook
        self.guidance_hook = guidance_hook

    async def run(self, request: AgentLoopRequest) -> AgentLoopResult:
        messages = list(request.messages)
        tool_count = 0
        input_tokens = output_tokens = 0
        estimated_cost = 0.0
        routes: list[str] = []
        started = monotonic()
        tools, required_capabilities = self._tools(request)
        tool_contract_ready = False
        for round_number in range(1, request.max_rounds + 1):
            await self._control_point()
            messages.extend(await self._guidance())
            response, route = await self.router.chat(
                ChatRequest(
                    messages=messages,
                    temperature=request.temperature,
                    max_tokens=request.max_tokens,
                    tools=tools,
                    response_schema=request.response_schema,
                    required_capabilities_extra=required_capabilities,
                    idempotency_key=f"{request.trace_id}:model:{round_number}",
                )
            )
            routes.append(self._route(route))
            input_tokens += response.usage.input_tokens
            output_tokens += response.usage.output_tokens
            estimated_cost += response.usage.estimated_cost
            assistant = ChatMessage(
                role="assistant", content=response.content, tool_calls=response.tool_calls
            )
            messages.append(assistant)
            limit_reason = self._usage_limit(
                request,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost=estimated_cost,
                elapsed_ms=int((monotonic() - started) * 1_000),
            )
            if limit_reason:
                self._append_cancelled(messages, response.tool_calls, limit_reason)
                raise AgentLoopLimitError(limit_reason, messages)
            if tool_contract_ready and response.tool_calls:
                # Some OpenAI-compatible providers keep calling inspection tools even
                # after every required side effect has succeeded.  Never execute those
                # duplicate calls: the graph contract, rather than provider etiquette,
                # is the authoritative termination condition.
                self._append_cancelled(
                    messages,
                    response.tool_calls,
                    "NODE_TOOL_CONTRACT_ALREADY_SATISFIED",
                )
                return self._contract_result(
                    request,
                    messages,
                    rounds=round_number,
                    tool_count=tool_count,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    estimated_cost=estimated_cost,
                    routes=routes,
                )
            if not response.tool_calls:
                await self._control_point()
                guidance = await self._guidance()
                if guidance:
                    messages.extend(guidance)
                    continue
                if request.required_successful_tools and not self._tool_contract_satisfied(
                    messages, request.required_successful_tools
                ):
                    missing = sorted(
                        set(request.required_successful_tools)
                        - set(self._successful_tool_results(messages))
                    )
                    messages.append(
                        ChatMessage(
                            role="developer",
                            content=(
                                "This node cannot finish with prose only. Call the missing "
                                f"registered tools now: {', '.join(missing)}. Do not restart "
                                "already successful tools and do not describe commands for the "
                                "user to run manually."
                            ),
                        )
                    )
                    continue
                return AgentLoopResult(
                    content=response.content,
                    messages=messages,
                    rounds=round_number,
                    tool_call_count=tool_count,
                    usage=Usage(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        estimated_cost=estimated_cost,
                    ),
                    routes=routes,
                    finish_reason=response.finish_reason,
                )
            tool_count += len(response.tool_calls)
            call_ids = [item.id for item in response.tool_calls]
            if len(call_ids) != len(set(call_ids)):
                raise ValueError("provider returned duplicate tool call ids")
            if tool_count > request.max_tool_calls:
                self._append_cancelled(messages, response.tool_calls, "TOOL_BUDGET_EXCEEDED")
                raise AgentLoopLimitError("tool call budget exceeded", messages)
            calls = [
                self._call(request, item, tool_count - len(response.tool_calls) + index)
                for index, item in enumerate(response.tool_calls, start=1)
            ]
            await self._control_point()
            results = await self.executor.execute_many(
                calls,
                ToolExecutionContext(
                    project_id=request.project_id,
                    workspace=self.workspace,
                    agent_type=request.agent_type,
                    provider_capabilities=self._provider_capabilities(route.provider_id),
                    approved=request.approved,
                ),
            )
            by_id = {item.id: item for item in response.tool_calls}
            for result in results:
                provider_call = by_id[result.call_id]
                messages.append(
                    ChatMessage(
                        role="tool",
                        content=result.model_dump_json(),
                        tool_call_id=result.call_id,
                        tool_name=provider_call.name,
                    )
                )
            if self._tool_contract_satisfied(messages, request.required_successful_tools):
                tool_contract_ready = True
                messages.append(
                    ChatMessage(
                        role="developer",
                        content=(
                            "The required tool contract is complete. Do not call any more "
                            "tools or repeat side effects. Return one concise summary of the "
                            "verified results and artifact references now."
                        ),
                    )
                )
        if tool_contract_ready:
            return self._contract_result(
                request,
                messages,
                rounds=request.max_rounds,
                tool_count=tool_count,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost=estimated_cost,
                routes=routes,
            )
        raise AgentLoopLimitError("model-tool round limit exceeded", messages)

    @staticmethod
    def _successful_tool_results(messages: list[ChatMessage]) -> dict[str, ToolResult]:
        successful: dict[str, ToolResult] = {}
        for message in messages:
            if message.role != "tool" or not message.tool_name:
                continue
            try:
                result = ToolResult.model_validate_json(message.content)
            except ValueError:
                continue
            if result.status is ToolResultStatus.SUCCESS:
                successful[message.tool_name] = result
        return successful

    @classmethod
    def _tool_contract_satisfied(
        cls, messages: list[ChatMessage], required_tools: list[str]
    ) -> bool:
        required = set(required_tools)
        return bool(required) and required <= set(cls._successful_tool_results(messages))

    @classmethod
    def _contract_result(
        cls,
        request: AgentLoopRequest,
        messages: list[ChatMessage],
        *,
        rounds: int,
        tool_count: int,
        input_tokens: int,
        output_tokens: int,
        estimated_cost: float,
        routes: list[str],
    ) -> AgentLoopResult:
        successful = cls._successful_tool_results(messages)
        evidence = [
            {
                "tool": name,
                "artifact_refs": successful[name].artifact_refs,
                "content_hash": successful[name].content_hash,
                "result": successful[name].content,
            }
            for name in request.required_successful_tools
            if name in successful
        ]
        return AgentLoopResult(
            content=(
                "Verified node tool contract completed: "
                + json.dumps(evidence, ensure_ascii=False)
            ),
            messages=messages,
            rounds=rounds,
            tool_call_count=tool_count,
            usage=Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost=estimated_cost,
            ),
            routes=routes,
            finish_reason="tool_contract_completed",
        )

    async def _control_point(self) -> None:
        if self.control_hook is not None:
            await self.control_hook()

    async def _guidance(self) -> list[ChatMessage]:
        return await self.guidance_hook() if self.guidance_hook is not None else []

    def _tools(self, request: AgentLoopRequest) -> tuple[list[dict[str, object]], set[Capability]]:
        definitions: list[dict[str, object]] = []
        required_capabilities: set[Capability] = set()
        available = {
            capability.value
            for provider in self.router.providers
            for capability in provider.config.capabilities
        }
        for name in request.tool_names:
            record = self.registry.resolve(
                name,
                agent_type=request.agent_type,
                provider_capabilities=available,
            )
            required_capabilities.update(
                Capability(capability) for capability in record.spec.required_provider_capabilities
            )
            definitions.append(
                {
                    "name": record.spec.name,
                    "description": record.spec.description,
                    "input_schema": record.spec.input_schema,
                }
            )
        return definitions, required_capabilities

    def _provider_capabilities(self, provider_id: str) -> set[str]:
        for provider in self.router.providers:
            if provider.config.id == provider_id:
                return {capability.value for capability in provider.config.capabilities}
        raise LookupError(f"routed provider not found: {provider_id}")

    @staticmethod
    def _append_cancelled(
        messages: list[ChatMessage],
        calls: list[ProviderToolCall],
        reason: str,
    ) -> None:
        for item in calls:
            messages.append(
                ChatMessage(
                    role="tool",
                    content=f'{{"status":"cancelled","error":{{"code":"{reason}"}}}}',
                    tool_call_id=item.id,
                    tool_name=item.name,
                )
            )

    @staticmethod
    def _usage_limit(
        request: AgentLoopRequest,
        *,
        input_tokens: int,
        output_tokens: int,
        estimated_cost: float,
        elapsed_ms: int,
    ) -> str | None:
        if request.max_total_input_tokens and input_tokens > request.max_total_input_tokens:
            return "INPUT_TOKEN_BUDGET_EXCEEDED"
        if request.max_total_output_tokens and output_tokens > request.max_total_output_tokens:
            return "OUTPUT_TOKEN_BUDGET_EXCEEDED"
        if request.max_cost is not None and estimated_cost > request.max_cost:
            return "COST_BUDGET_EXCEEDED"
        if elapsed_ms > request.max_elapsed_ms:
            return "TIME_BUDGET_EXCEEDED"
        return None

    @staticmethod
    def _call(request: AgentLoopRequest, item: ProviderToolCall, sequence: int) -> ToolCall:
        return ToolCall(
            call_id=item.id,
            trace_id=request.trace_id,
            sequence=sequence,
            tool_name=item.name,
            arguments=item.arguments,
            requested_by=request.agent_type,
            idempotency_key=f"{request.trace_id}:tool:{item.id}",
        )

    @staticmethod
    def _route(route: RouteDecision) -> str:
        return f"{route.provider_id}:{route.reason}"
