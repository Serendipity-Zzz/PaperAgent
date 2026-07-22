from __future__ import annotations

from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import pytest

from paperagent.artifacts import (
    ArtifactIntegrityError,
    ArtifactService,
    CompletionClaimValidator,
)
from paperagent.core.config import Settings
from paperagent.db import DatabaseManager


def service(tmp_path: Path) -> tuple[ArtifactService, Path]:
    settings = Settings(
        project_root=tmp_path / "repo",
        data_dir=tmp_path / "data",
        environment="test",
    )
    databases = DatabaseManager(settings)
    databases.initialize_global()
    project_id = str(uuid4())
    root = databases.project_root(project_id)
    root.mkdir(parents=True)
    databases.project_engine(project_id).dispose()
    return ArtifactService(databases, project_id), root


def test_artifact_registration_link_lookup_and_claim_validation(tmp_path: Path) -> None:
    artifacts, root = service(tmp_path)
    source = root / "runs" / "run-1" / "experiment.py"
    source.parent.mkdir(parents=True)
    source.write_text("print('measured')\n", encoding="utf-8")
    registered = artifacts.register(
        source,
        kind="source",
        producer_tool="code.materialize",
        run_id="run-1",
    )
    repeated = artifacts.register(
        source,
        kind="source",
        producer_tool="code.materialize",
        run_id="run-1",
    )
    assert repeated.id == registered.id

    artifacts.link(
        registered.id,
        relation="source",
        conversation_id="conversation-1",
        message_id="message-1",
        run_id="run-1",
    )
    matches = artifacts.lookup(conversation_id="conversation-1", relation="source")
    assert matches[0]["id"] == registered.id
    assert matches[0]["sha256"] == registered.sha256

    validator = CompletionClaimValidator(artifacts)
    validated = validator.validate("run-1", "已生成实验源码 experiment.py")
    assert [item.id for item in validated] == [registered.id]
    with pytest.raises(ArtifactIntegrityError):
        validator.validate("run-1", "已生成实验报告 report.pdf")
    with pytest.raises(ArtifactIntegrityError):
        validator.validate("missing-run", "已生成报告 report.pdf")


def test_artifact_tamper_and_outside_path_are_rejected(tmp_path: Path) -> None:
    artifacts, root = service(tmp_path)
    report = root / "artifacts" / "report.pdf"
    report.parent.mkdir(parents=True)
    report.write_bytes(b"%PDF-1.7\nfixture")
    registered = artifacts.register(
        report,
        kind="output",
        producer_tool="document.render",
        run_id="run-2",
    )
    report.write_bytes(b"%PDF-1.7\ntampered")
    with pytest.raises(ArtifactIntegrityError):
        artifacts.get(registered.id)

    outside = root.parent / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    with pytest.raises(PermissionError):
        artifacts.register(
            outside,
            kind="output",
            producer_tool="document.render",
        )


def test_document_claim_rejects_placeholder_and_missing_revision_lineage(
    tmp_path: Path,
) -> None:
    artifacts, root = service(tmp_path)
    markdown = root / "artifacts" / "report.md"
    markdown.parent.mkdir(parents=True)
    markdown.write_text(
        "# Report\n\nVerified source content is supplied by the renderer.\n",
        encoding="utf-8",
    )
    artifacts.register(
        markdown,
        kind="output",
        producer_tool="document.render",
        run_id="run-document",
    )
    with pytest.raises(ArtifactIntegrityError, match="derivation evidence"):
        CompletionClaimValidator(artifacts).validate("run-document", "已生成报告 report.md")


def test_document_claim_rejects_invalid_docx_and_missing_embedded_image(
    tmp_path: Path,
) -> None:
    artifacts, root = service(tmp_path)
    invalid = root / "artifacts" / "invalid.docx"
    invalid.parent.mkdir(parents=True)
    with ZipFile(invalid, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
    artifacts.register(
        invalid,
        kind="output",
        producer_tool="legacy.import",
        run_id="run-invalid-docx",
    )
    with pytest.raises(ArtifactIntegrityError, match="native document structure"):
        CompletionClaimValidator(artifacts).validate(
            "run-invalid-docx", "已生成 Word 报告 invalid.docx"
        )

    markdown = root / "artifacts" / "no-image.md"
    markdown.write_text("# Report\n\nNo embedded figure.\n", encoding="utf-8")
    artifacts.register(
        markdown,
        kind="output",
        producer_tool="legacy.import",
        run_id="run-no-image",
    )
    with pytest.raises(ArtifactIntegrityError, match="required image"):
        CompletionClaimValidator(artifacts).validate(
            "run-no-image", "已生成包含实验图的报告 no-image.md"
        )
