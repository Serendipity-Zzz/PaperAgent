from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from pydantic import JsonValue

from paperagent.engine.events import EngineEvent, EngineEventKind, TurnRequest
from paperagent.engine.lifecycle import CancelRequest, EngineSignal, GraphLifecycle, ResumeRequest
from paperagent.engine.persistence import ConversationPersistence


class ConversationEngine:
    """Durable outer shell around a LangGraph lifecycle and future AgentLoop."""

    def __init__(self, persistence: ConversationPersistence, lifecycle: GraphLifecycle) -> None:
        self.persistence = persistence
        self.lifecycle = lifecycle
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._cancelled: set[tuple[str, str]] = set()

    async def run_turn(self, request: TurnRequest) -> AsyncIterator[EngineEvent]:
        key = (request.project_id, request.task_id)
        async with self._locks.setdefault(key, asyncio.Lock()):
            replay = self.persistence.events_for_request(
                request.project_id, request.task_id, str(request.request_id)
            )
            if replay and replay[-1].kind in {
                EngineEventKind.COMPLETED,
                EngineEventKind.CANCELLED,
                EngineEventKind.FAILED,
                EngineEventKind.INTERRUPTED,
            }:
                for event in replay:
                    yield event
                return
            self.persistence.save_user_message(request)
            async for event in self._run(
                request=request,
                signals=self.lifecycle.start(request),
                first_kind=EngineEventKind.TURN_ACCEPTED,
            ):
                yield event

    async def resume(self, request: ResumeRequest) -> AsyncIterator[EngineEvent]:
        key = (request.project_id, request.task_id)
        async with self._locks.setdefault(key, asyncio.Lock()):
            replay = self.persistence.events_for_request(
                request.project_id, request.task_id, str(request.request_id)
            )
            if replay and replay[-1].kind in {
                EngineEventKind.COMPLETED,
                EngineEventKind.CANCELLED,
                EngineEventKind.FAILED,
                EngineEventKind.INTERRUPTED,
            }:
                for event in replay:
                    yield event
                return
            async for event in self._run(
                request=request,
                signals=self.lifecycle.resume(request),
                first_kind=EngineEventKind.GRAPH_RESUMED,
            ):
                yield event

    async def cancel(self, request: CancelRequest) -> EngineEvent:
        key = (request.project_id, request.task_id)
        existing = self.persistence.events_for_request(
            request.project_id, request.task_id, str(request.request_id)
        )
        if existing:
            return existing[-1]
        self._cancelled.add(key)
        await self.lifecycle.cancel(request)
        return await self._emit(
            request,
            EngineEventKind.CANCELLED,
            {"reason": request.reason, "idempotency_key": request.idempotency_key},
        )

    async def _run(
        self,
        *,
        request: TurnRequest | ResumeRequest,
        signals: AsyncIterator[EngineSignal],
        first_kind: EngineEventKind,
    ) -> AsyncIterator[EngineEvent]:
        key = (request.project_id, request.task_id)
        first = await self._emit(
            request,
            first_kind,
            {"idempotency_key": request.idempotency_key},
        )
        yield first
        if first_kind is EngineEventKind.TURN_ACCEPTED:
            yield await self._emit(request, EngineEventKind.GRAPH_STARTED, {})
        try:
            async for signal in signals:
                if key in self._cancelled:
                    return
                yield await self._emit(request, signal.kind, signal.payload)
                if signal.kind in {
                    EngineEventKind.INTERRUPTED,
                    EngineEventKind.CANCELLED,
                    EngineEventKind.FAILED,
                }:
                    return
            if key not in self._cancelled:
                yield await self._emit(request, EngineEventKind.COMPLETED, {})
        except asyncio.CancelledError:
            self._cancelled.add(key)
            yield await self._emit(request, EngineEventKind.CANCELLED, {"reason": "task_cancelled"})
        except Exception as error:
            yield await self._emit(
                request,
                EngineEventKind.FAILED,
                {"error_type": error.__class__.__name__, "message": str(error)[:2_000]},
            )
        finally:
            self._cancelled.discard(key)

    async def _emit(
        self,
        request: TurnRequest | ResumeRequest | CancelRequest,
        kind: EngineEventKind,
        payload: dict[str, JsonValue],
    ) -> EngineEvent:
        event_payload = dict(payload)
        event_payload["request_id"] = str(request.request_id)
        sequence = self.persistence.latest_sequence(request.project_id, request.task_id) + 1
        event = EngineEvent(
            trace_id=request.trace_id,
            project_id=request.project_id,
            thread_id=request.thread_id,
            task_id=request.task_id,
            sequence=sequence,
            kind=kind,
            payload=event_payload,
        )
        self.persistence.append_event(event)
        return event
