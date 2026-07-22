import asyncio
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from paperagent.agents.document_ir import (
    BlockKind,
    DocumentBlock,
    DocumentIR,
    DocumentSection,
    Provenance,
)
from paperagent.api import create_app
from paperagent.core.config import Settings
from paperagent.db import DatabaseManager
from paperagent.rendering import DocumentRevisionConflict, DocumentRevisionStore
from paperagent.schemas import TaskStatus
from paperagent.security import LocalSessionTokens
from paperagent.services.repositories import ProjectRepository
from paperagent.services.resources import (
    FairResourceScheduler,
    ProcessLedger,
    ResourceCapacityError,
    ResourceKind,
    ResourceLimits,
    ResourceRequest,
)
from paperagent.services.run_scheduler import DurableRunScheduler
from paperagent.services.tasks import TaskService


def runtime(tmp_path: Path) -> tuple[TaskService, str, str]:
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data", environment="test")
    databases = DatabaseManager(settings)
    databases.initialize_global()
    projects = ProjectRepository(databases)
    return TaskService(databases), projects.create("first").id, projects.create("second").id


def test_resource_limits_reject_unbounded_and_impossible_requests() -> None:
    with pytest.raises(ValidationError):
        ResourceLimits(remote_llm=0)
    with pytest.raises(ValidationError):
        ResourceLimits(gpu_job=999)
    scheduler = FairResourceScheduler(ResourceLimits(gpu_job=1))
    with pytest.raises(ResourceCapacityError, match="local limit"):
        scheduler.validate(ResourceRequest(resources={ResourceKind.GPU_JOB: 2}))


@pytest.mark.anyio
async def test_fair_queue_limits_parallelism_and_project_writes(tmp_path: Path) -> None:
    tasks, first_project, second_project = runtime(tmp_path)
    resources = FairResourceScheduler(ResourceLimits(remote_llm=2, document_write=2))
    scheduler = DurableRunScheduler(tasks, worker_id="test-worker", resources=resources)
    runs = [
        tasks.create(first_project, "parallel", f"parallel-{index}", {"session_id": f"s{index}"})
        for index in range(3)
    ]
    active = 0
    peak = 0
    order: list[int] = []
    lock = asyncio.Lock()

    async def work(index: int) -> None:
        nonlocal active, peak
        async with lock:
            active += 1
            peak = max(peak, active)
            order.append(index)
        await asyncio.sleep(0.04)
        tasks.transition(first_project, runs[index].id, TaskStatus.COMPLETED)
        async with lock:
            active -= 1

    for index, run in enumerate(runs):
        scheduler.launch(first_project, run.id, lambda index=index: work(index))
    handles = [handle.task for handle in scheduler.handles.values()]
    await asyncio.gather(*handles)
    assert peak == 2
    assert order == [0, 1, 2]
    assert resources.snapshot()["used"]["remote_llm"] == 0  # type: ignore[index]

    write_runs = [
        (first_project, tasks.create(first_project, "write", "write-a", {})),
        (first_project, tasks.create(first_project, "write", "write-b", {})),
        (second_project, tasks.create(second_project, "write", "write-c", {})),
    ]
    same_project_active = 0
    same_project_peak = 0
    global_active = 0
    global_peak = 0

    async def write(project_id: str, run_id: str) -> None:
        nonlocal same_project_active, same_project_peak, global_active, global_peak
        global_active += 1
        global_peak = max(global_peak, global_active)
        if project_id == first_project:
            same_project_active += 1
            same_project_peak = max(same_project_peak, same_project_active)
        await asyncio.sleep(0.04)
        tasks.transition(project_id, run_id, TaskStatus.COMPLETED)
        if project_id == first_project:
            same_project_active -= 1
        global_active -= 1

    request = ResourceRequest(resources={ResourceKind.DOCUMENT_WRITE: 1}, project_write=True)
    for project_id, run in write_runs:
        scheduler.launch(
            project_id,
            run.id,
            lambda project_id=project_id, run_id=run.id: write(project_id, run_id),
            request,
        )
    await asyncio.gather(
        *(handle.task for handle in scheduler.handles.values() if not handle.task.done())
    )
    assert same_project_peak == 1
    assert global_peak == 2


@pytest.mark.anyio
async def test_cancelled_queued_run_releases_ticket(tmp_path: Path) -> None:
    tasks, project_id, _ = runtime(tmp_path)
    resources = FairResourceScheduler(ResourceLimits(remote_llm=1))
    scheduler = DurableRunScheduler(tasks, resources=resources)
    first = tasks.create(project_id, "queued", "queue-1", {})
    second = tasks.create(project_id, "queued", "queue-2", {})
    release = asyncio.Event()

    async def blocker() -> None:
        await release.wait()
        tasks.transition(project_id, first.id, TaskStatus.COMPLETED)

    scheduler.launch(project_id, first.id, blocker)
    scheduler.launch(project_id, second.id, lambda: asyncio.sleep(0))
    await asyncio.sleep(0.02)
    assert second.id in resources.snapshot()["queued"]  # type: ignore[operator]
    await scheduler.cancel(project_id, second.id)
    release.set()
    await scheduler.handles[(project_id, first.id)].task
    assert tasks.get(project_id, second.id).status == "cancelled"
    assert second.id not in resources.snapshot()["queued"]  # type: ignore[operator]


def document() -> DocumentIR:
    return DocumentIR(
        requirement_id=uuid4(),
        requirement_version=1,
        outline_id=uuid4(),
        title="Parallel paper",
        language="en",
        sections=[
            DocumentSection(
                title="Introduction",
                goal="Introduce",
                blocks=[
                    DocumentBlock(
                        kind=BlockKind.PARAGRAPH,
                        text="base",
                        provenance=Provenance(agent="writer"),
                    )
                ],
            )
        ],
    )


def test_document_optimistic_commit_rejects_stale_sidecar(tmp_path: Path) -> None:
    store = DocumentRevisionStore(tmp_path)
    base = document()
    store.commit(base, expected_revision=0, input_hash="base-hash", run_id="main")
    snapshot = store.snapshot(base.document_id)
    block_id = base.sections[0].blocks[0].block_id
    first = snapshot.document.patch_block(block_id, {"text": "first writer"})
    stale = snapshot.document.patch_block(block_id, {"text": "stale sidecar"})
    store.commit(first, expected_revision=1, input_hash=snapshot.content_hash, run_id="run-a")
    with pytest.raises(DocumentRevisionConflict) as conflict:
        store.commit(stale, expected_revision=1, input_hash=snapshot.content_hash, run_id="run-b")
    assert conflict.value.current_revision == 2
    assert block_id in conflict.value.changed_block_ids
    assert store.load(base.document_id).sections[0].blocks[0].text == "first writer"


def test_owned_process_ledger_terminates_only_matching_orphan(tmp_path: Path) -> None:
    ledger = ProcessLedger(tmp_path / "processes.db")
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        ledger.register("run-process", process.pid, [sys.executable, "sleep"])
        audit = ledger.audit_orphans(terminate=True)
        process.wait(timeout=5)
        assert audit[0]["status"] == "terminated"
    finally:
        if process.poll() is None:
            process.kill()
        ledger.close()


def test_activity_aggregates_projects_and_clears_completed_unread(tmp_path: Path) -> None:
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data", environment="test")
    tokens = LocalSessionTokens(secret=b"a" * 32)
    with TestClient(create_app(settings, tokens)) as client:
        headers = {"Authorization": f"Bearer {tokens.issue()}"}
        project = client.post("/api/projects", headers=headers, json={"name": "activity"}).json()
        base = f"/api/projects/{project['id']}"
        run = client.post(
            f"{base}/tasks",
            headers=headers,
            json={"kind": "activity", "idempotency_key": "activity-1", "payload": {}},
        ).json()
        client.patch(f"{base}/tasks/{run['id']}", headers=headers, json={"status": "running"})
        client.patch(f"{base}/tasks/{run['id']}", headers=headers, json={"status": "completed"})
        activity = client.get("/api/activity", headers=headers).json()
        assert activity[0]["id"] == run["id"] and activity[0]["unread"] is True
        client.post(f"{base}/runs/{run['id']}/read", headers=headers)
        assert client.get("/api/activity", headers=headers).json() == []

        limits = client.put(
            "/api/runtime/resources",
            headers=headers,
            json={
                "remote_llm": 3,
                "document_write": 1,
                "cpu_job": 2,
                "gpu_job": 1,
                "image_generation": 2,
                "render": 1,
            },
        )
        assert limits.status_code == 200
        assert limits.json()["limits"]["remote_llm"] == 3
