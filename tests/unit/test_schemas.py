import pytest
from pydantic import ValidationError

from paperagent.schemas import Artifact, ArtifactKind, ErrorDetail, Page


def test_artifact_json_round_trip() -> None:
    artifact = Artifact(
        kind=ArtifactKind.DOCUMENT,
        name="论文.md",
        media_type="text/markdown",
        relative_path="artifacts/论文.md",
        sha256="a" * 64,
        size_bytes=42,
        provenance={"source": "generated"},
    )
    restored = Artifact.model_validate_json(artifact.model_dump_json())
    assert restored == artifact


def test_invalid_error_code_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ErrorDetail(code="bad-code", message="invalid")


def test_page_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Page[int](items=[1], total=1, offset=0, limit=20, unknown=True)
