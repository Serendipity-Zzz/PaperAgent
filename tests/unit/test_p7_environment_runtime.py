import subprocess
import sys
from pathlib import Path

import pytest

from paperagent.experiments.runtime import (
    CapabilityAnalyzer,
    EnvironmentManager,
    EnvironmentRecord,
    EnvironmentRegistry,
    ExecutionApproval,
    ExperimentResultPackage,
    ProcessExecutor,
)


def test_environment_fingerprint_reuse_cuda_lock_cleanup_and_source_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = EnvironmentRegistry(tmp_path / "runtimes")
    uv = tmp_path / "uv.exe"
    uv.write_bytes(b"mock")

    def run(command, **kwargs):
        del kwargs
        Path(command[2]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    manager = EnvironmentManager(registry, uv.resolve(), tmp_path / "cache")
    first = manager.ensure("numpy==2.0", python_version="3.12")
    reused = manager.ensure(" numpy==2.0 \n", python_version="3.12")
    cuda = registry.fingerprint("numpy==2.0", "3.12", "12.4")
    assert first.environment_id == reused.environment_id
    assert first.fingerprint != cuda
    assert reused.refcount == 2
    with pytest.raises(PermissionError, match="source"):
        manager.ensure("pkg @ https://example.test/pkg.zip", python_version="3.12")
    idle_path = registry.root / "venvs" / "idle"
    idle_path.mkdir(parents=True)
    idle = EnvironmentRecord(
        fingerprint="b" * 64,
        path=str(idle_path),
        lock_hash="c" * 64,
        python_version="3.12",
        size_bytes=10_000,
    )
    registry.save(idle)
    assert registry.cleanup_candidates(1)[0].fingerprint == idle.fingerprint
    with pytest.raises(PermissionError):
        registry.delete(idle.fingerprint, approved=False)
    registry.delete(idle.fingerprint, approved=True)
    assert not idle_path.exists()


def test_repository_capability_verdicts_and_experiment_fact_boundary(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text(
        "Requires Python >=3.11, CUDA 12.4 and 12 GB model weights", encoding="utf-8"
    )
    no_gpu = CapabilityAnalyzer().analyze(repo, gpu_name=None, vram_gb=None, disk_free_gb=100)
    assert no_gpu.verdict == "unreasonable" and no_gpu.cuda_requirement == "12.4"
    small_gpu = CapabilityAnalyzer().analyze(repo, gpu_name="GPU", vram_gb=8, disk_free_gb=100)
    assert small_gpu.verdict == "degraded"
    package = ExperimentResultPackage(
        repository="repo",
        commit="a" * 40,
        environment_fingerprint="b" * 64,
        command=["python", "train.py"],
        hardware={"gpu": "GPU"},
        metrics={"accuracy": 0.9},
        status="completed",
        simulated_data=True,
    )
    assert not package.eligible_as_experiment_evidence
    assert package.model_copy(update={"simulated_data": False}).eligible_as_experiment_evidence


def test_process_execution_approval_path_timeout_and_log_limit(tmp_path: Path) -> None:
    approval = ExecutionApproval(
        command=[sys.executable, "-c", "print('x' * 5000)"],
        working_directory=str(tmp_path),
        writable_paths=[str(tmp_path)],
        network_allowed=False,
        timeout_seconds=10,
        max_log_bytes=100,
    )
    with pytest.raises(PermissionError):
        ProcessExecutor().run(approval)
    chunks: list[tuple[str, str]] = []
    result = ProcessExecutor().run(
        approval.model_copy(update={"approved": True}),
        on_output=lambda channel, content: chunks.append((channel, content)),
    )
    assert result.status == "completed" and result.truncated
    assert len(result.stdout) <= 50
    assert chunks and chunks[0][0] == "stdout"
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    violation = approval.model_copy(
        update={
            "command": [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('outside.txt').write_text('bad')",
            ],
            "writable_paths": [str(allowed)],
            "approved": True,
            "max_log_bytes": 1000,
        }
    )
    assert ProcessExecutor().run(violation).status == "policy_violation"
