from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from paperagent.execution import (
    ArtifactLink,
    ArtifactRelation,
    AuthorizationGrant,
    CapabilityDescriptor,
    CapabilityKind,
    CapabilitySnapshot,
    ExecutionRecord,
    ExecutionRequest,
    ExecutionStatus,
    ManagedPathPolicy,
    PathOperation,
)


def test_capability_snapshot_is_order_stable_and_rejects_duplicates() -> None:
    first = CapabilityDescriptor(name="process.execute", version="1.0.0", kind=CapabilityKind.TOOL)
    second = CapabilityDescriptor(name="writer.agent", version="1.0.0", kind=CapabilityKind.AGENT)
    left = CapabilitySnapshot(descriptors=[first, second])
    right = CapabilitySnapshot(descriptors=[second, first])
    assert left.snapshot_hash == right.snapshot_hash
    with pytest.raises(ValidationError, match="duplicate identities"):
        CapabilitySnapshot(descriptors=[first, first])


def test_reusable_authorization_cannot_allow_delete_or_outside_write() -> None:
    request = ExecutionRequest(run_id="run-1", argv=["python", "main.py"], cwd="runs/run-1")
    grant = AuthorizationGrant(
        subject="project:p1",
        capabilities={"process.execute"},
        write_roots=["runs/run-1"],
        action_hash=request.action_hash(),
    )
    assert grant.authorizes("process.execute", request.action_hash())
    with pytest.raises(ValidationError, match="delete permission"):
        AuthorizationGrant(
            subject="project:p1",
            capabilities={"process.execute"},
            action_hash=request.action_hash(),
            delete_allowed=True,
        )


def test_execution_request_rejects_shell_and_delete() -> None:
    with pytest.raises(ValidationError, match="shell interpreters"):
        ExecutionRequest(run_id="run-1", argv=["pwsh", "-Command", "echo ok"], cwd=".")
    with pytest.raises(ValidationError, match="deletion"):
        ExecutionRequest(
            run_id="run-1",
            argv=["python", "main.py"],
            cwd=".",
            expected_delete_paths=["old.txt"],
        )


def test_managed_path_policy_requires_approval_for_delete_and_external_write(
    tmp_path: Path,
) -> None:
    managed = tmp_path / "managed"
    readonly = tmp_path / "inputs"
    managed.mkdir()
    readonly.mkdir()
    policy = ManagedPathPolicy(read_roots=[readonly], write_roots=[managed])
    assert policy.classify(managed / "result.pdf", PathOperation.WRITE).allowed
    outside = policy.classify(tmp_path / "desktop.txt", PathOperation.WRITE)
    assert not outside.allowed and outside.requires_approval
    deletion = policy.classify(managed / "result.pdf", PathOperation.DELETE)
    assert not deletion.allowed and deletion.requires_approval


def test_execution_record_and_artifact_link_invariants() -> None:
    now = datetime.now(UTC)
    record = ExecutionRecord(
        request_id=uuid4(),
        run_id="run-1",
        status=ExecutionStatus.SUCCEEDED,
        command_hash="a" * 64,
        exit_code=0,
        started_at=now,
        finished_at=now + timedelta(seconds=1),
    )
    assert record.status is ExecutionStatus.SUCCEEDED
    with pytest.raises(ValidationError, match="requires conversation"):
        ArtifactLink(artifact_id=uuid4(), relation=ArtifactRelation.OUTPUT)
