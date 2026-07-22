from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import uuid4

from paperagent.schemas import TaskStatus
from paperagent.services.resources import FairResourceScheduler, ResourceRequest
from paperagent.services.tasks import TaskService

RunFactory = Callable[[], Awaitable[None]]


@dataclass(slots=True)
class RunHandle:
    task: asyncio.Task[None]
    gate: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self.gate.set()


class DurableRunScheduler:
    """Own local workers while TaskService remains the durable source of truth."""

    def __init__(
        self,
        tasks: TaskService,
        worker_id: str | None = None,
        resources: FairResourceScheduler | None = None,
    ) -> None:
        self.tasks = tasks
        self.worker_id = worker_id or f"local-{uuid4()}"
        self.resources = resources or FairResourceScheduler()
        self.handles: dict[tuple[str, str], RunHandle] = {}

    def launch(
        self,
        project_id: str,
        run_id: str,
        factory: RunFactory,
        resource_request: ResourceRequest | None = None,
    ) -> bool:
        key = (project_id, run_id)
        current = self.handles.get(key)
        if current is not None and not current.task.done():
            return False
        request = resource_request or ResourceRequest()
        self.resources.validate(request)
        self.tasks.set_resource_request(project_id, run_id, request.model_dump(mode="json"))
        self.tasks.update_phase(project_id, run_id, "waiting_resource")

        async def run() -> None:
            async with self.resources.reserve(project_id, run_id, request):
                handle = self.handles[key]
                await handle.gate.wait()
                self.tasks.claim(project_id, run_id, f"{self.worker_id}:{run_id}")
                self.tasks.update_phase(project_id, run_id, "starting")
                await factory()

        task: asyncio.Task[None] = asyncio.create_task(
            run(), name=f"paperagent:{project_id}:{run_id}"
        )
        self.handles[key] = RunHandle(task=task)
        return True

    def is_active(self, project_id: str, run_id: str) -> bool:
        handle = self.handles.get((project_id, run_id))
        return handle is not None and not handle.task.done()

    async def checkpoint(self, project_id: str, run_id: str) -> None:
        handle = self.handles.get((project_id, run_id))
        if handle is None:
            raise asyncio.CancelledError
        await handle.gate.wait()

    def pause(self, project_id: str, run_id: str) -> None:
        handle = self.handles.get((project_id, run_id))
        if handle is None or handle.task.done():
            raise ValueError("Run is not active")
        handle.gate.clear()
        self.tasks.transition(project_id, run_id, TaskStatus.PAUSED)

    def resume(self, project_id: str, run_id: str) -> bool:
        handle = self.handles.get((project_id, run_id))
        if handle is None or handle.task.done():
            return False
        handle.gate.set()
        return True

    async def cancel(self, project_id: str, run_id: str) -> None:
        self.tasks.transition(project_id, run_id, TaskStatus.CANCELLED)
        handle = self.handles.get((project_id, run_id))
        if handle is not None and not handle.task.done():
            handle.task.cancel()
            await asyncio.gather(handle.task, return_exceptions=True)
        self.handles.pop((project_id, run_id), None)

    async def supersede(self, project_id: str, run_id: str) -> None:
        self.tasks.transition(project_id, run_id, TaskStatus.SUPERSEDED)
        handle = self.handles.get((project_id, run_id))
        if handle is not None and not handle.task.done():
            handle.task.cancel()
            await asyncio.gather(handle.task, return_exceptions=True)
        self.handles.pop((project_id, run_id), None)

    def discard(self, project_id: str, run_id: str) -> None:
        self.handles.pop((project_id, run_id), None)

    async def shutdown(self) -> None:
        pending: list[asyncio.Task[None]] = []
        for (project_id, run_id), handle in list(self.handles.items()):
            if handle.task.done():
                continue
            try:
                if TaskStatus(self.tasks.get(project_id, run_id).status) is TaskStatus.RUNNING:
                    self.tasks.transition(project_id, run_id, TaskStatus.PAUSED)
            except (KeyError, ValueError):
                pass
            handle.task.cancel()
            pending.append(handle.task)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.handles.clear()

    def resource_snapshot(self) -> dict[str, object]:
        return self.resources.snapshot()
