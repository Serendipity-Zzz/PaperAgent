from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from threading import RLock
from uuid import uuid4

import psutil
from pydantic import BaseModel, Field, model_validator


class ResourceKind(StrEnum):
    REMOTE_LLM = "remote_llm"
    DOCUMENT_WRITE = "document_write"
    CPU_JOB = "cpu_job"
    GPU_JOB = "gpu_job"
    IMAGE_GENERATION = "image_generation"
    RENDER = "render"


class ResourceLimits(BaseModel):
    remote_llm: int = Field(default=4, ge=1, le=32)
    document_write: int = Field(default=1, ge=1, le=4)
    cpu_job: int = Field(default=2, ge=1, le=16)
    gpu_job: int = Field(default=1, ge=1, le=8)
    image_generation: int = Field(default=2, ge=1, le=16)
    render: int = Field(default=1, ge=1, le=8)

    def as_map(self) -> dict[ResourceKind, int]:
        return {kind: int(getattr(self, kind.value)) for kind in ResourceKind}


class ResourceRequest(BaseModel):
    resources: dict[ResourceKind, int] = Field(default_factory=lambda: {ResourceKind.REMOTE_LLM: 1})
    project_write: bool = False
    priority: int = Field(default=0, ge=-10, le=10)

    @model_validator(mode="after")
    def validate_quantities(self) -> ResourceRequest:
        if not self.resources:
            raise ValueError("at least one resource is required")
        if any(quantity < 1 or quantity > 8 for quantity in self.resources.values()):
            raise ValueError("resource quantities must be between 1 and 8")
        return self


class ResourceCapacityError(ValueError):
    pass


class ResourceLease(BaseModel):
    lease_id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    project_id: str
    resources: dict[ResourceKind, int]
    project_write: bool
    acquired_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


@dataclass(slots=True)
class ResourceTicket:
    project_id: str
    run_id: str
    request: ResourceRequest
    enqueued_at: float


class FairResourceScheduler:
    """FIFO multi-resource admission with finite capacities and project write locks."""

    def __init__(self, limits: ResourceLimits | None = None) -> None:
        self.limits = (limits or ResourceLimits()).as_map()
        self.used = {kind: 0 for kind in ResourceKind}
        self.project_writers: set[str] = set()
        self.queue: deque[ResourceTicket] = deque()
        self.condition = asyncio.Condition()

    def _validate_capacity(self, request: ResourceRequest) -> None:
        for kind, quantity in request.resources.items():
            if quantity > self.limits[kind]:
                raise ResourceCapacityError(
                    f"{kind.value} requests {quantity}, local limit is {self.limits[kind]}"
                )

    def validate(self, request: ResourceRequest) -> None:
        self._validate_capacity(request)

    def configure(self, limits: ResourceLimits) -> None:
        if self.queue or any(self.used.values()):
            raise ValueError("resource limits cannot change while runs are active or queued")
        self.limits = limits.as_map()

    def _available(self, project_id: str, request: ResourceRequest) -> bool:
        if request.project_write and project_id in self.project_writers:
            return False
        return all(
            self.used[kind] + quantity <= self.limits[kind]
            for kind, quantity in request.resources.items()
        )

    def _selected(self) -> ResourceTicket | None:
        now = time.monotonic()
        candidates = [
            (index, ticket)
            for index, ticket in enumerate(self.queue)
            if self._available(ticket.project_id, ticket.request)
        ]
        if not candidates:
            return None
        _, selected = max(
            candidates,
            key=lambda item: (
                item[1].request.priority + (now - item[1].enqueued_at) / 5.0,
                -item[0],
            ),
        )
        return selected

    async def acquire(
        self, project_id: str, run_id: str, request: ResourceRequest
    ) -> ResourceLease:
        self._validate_capacity(request)
        ticket = ResourceTicket(project_id, run_id, request, time.monotonic())
        async with self.condition:
            self.queue.append(ticket)
            try:
                while self._selected() is not ticket:
                    await self.condition.wait()
                self.queue.remove(ticket)
                for kind, quantity in request.resources.items():
                    self.used[kind] += quantity
                if request.project_write:
                    self.project_writers.add(project_id)
                self.condition.notify_all()
            except BaseException:
                if ticket in self.queue:
                    self.queue.remove(ticket)
                    self.condition.notify_all()
                raise
        return ResourceLease(
            run_id=run_id,
            project_id=project_id,
            resources=request.resources,
            project_write=request.project_write,
        )

    async def release(self, lease: ResourceLease) -> None:
        async with self.condition:
            for kind, quantity in lease.resources.items():
                self.used[kind] = max(0, self.used[kind] - quantity)
            if lease.project_write:
                self.project_writers.discard(lease.project_id)
            self.condition.notify_all()

    @asynccontextmanager
    async def reserve(
        self, project_id: str, run_id: str, request: ResourceRequest
    ) -> AsyncIterator[ResourceLease]:
        lease = await self.acquire(project_id, run_id, request)
        try:
            yield lease
        finally:
            await self.release(lease)

    def snapshot(self) -> dict[str, object]:
        return {
            "limits": {kind.value: value for kind, value in self.limits.items()},
            "used": {kind.value: value for kind, value in self.used.items()},
            "queued": [ticket.run_id for ticket in self.queue],
            "project_writers": sorted(self.project_writers),
        }


class ProcessLedger:
    """Track owned child processes so restart audit never kills an unrelated PID."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.lock = RLock()
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS owned_processes(
              process_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, pid INTEGER NOT NULL,
              create_time REAL NOT NULL, command_json TEXT NOT NULL, status TEXT NOT NULL,
              started_at TEXT NOT NULL, finished_at TEXT
            )
            """
        )
        self.connection.commit()

    def register(self, run_id: str, pid: int, command: list[str]) -> str:
        process = psutil.Process(pid)
        process_id = str(uuid4())
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO owned_processes VALUES (?,?,?,?,?,'running',?,NULL)",
                (
                    process_id,
                    run_id,
                    pid,
                    process.create_time(),
                    json.dumps(command, ensure_ascii=False),
                    datetime.now(UTC).isoformat(),
                ),
            )
        return process_id

    def complete(self, process_id: str) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE owned_processes SET status='completed', finished_at=? WHERE process_id=?",
                (datetime.now(UTC).isoformat(), process_id),
            )

    def audit_orphans(self, *, terminate: bool = False) -> list[dict[str, object]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT process_id,run_id,pid,create_time,command_json FROM owned_processes "
                "WHERE status='running'"
            ).fetchall()
        result: list[dict[str, object]] = []
        for process_id, run_id, pid, create_time, command_json in rows:
            alive = False
            try:
                process = psutil.Process(pid)
                alive = abs(process.create_time() - create_time) < 0.01
                if alive and terminate:
                    for child in process.children(recursive=True):
                        child.terminate()
                    process.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                alive = False
            status = "terminated" if alive and terminate else "orphaned" if alive else "exited"
            with self.lock, self.connection:
                self.connection.execute(
                    "UPDATE owned_processes SET status=?, finished_at=? WHERE process_id=?",
                    (status, datetime.now(UTC).isoformat(), process_id),
                )
            result.append(
                {
                    "process_id": process_id,
                    "run_id": run_id,
                    "pid": pid,
                    "command": json.loads(command_json),
                    "status": status,
                }
            )
        return result

    def close(self) -> None:
        with self.lock:
            self.connection.close()
