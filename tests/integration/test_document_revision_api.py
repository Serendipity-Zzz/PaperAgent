from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from paperagent.agents.document_ir import (
    BlockKind,
    DocumentBlock,
    DocumentIR,
    DocumentSection,
    Provenance,
)
from paperagent.api import create_app
from paperagent.core.config import Settings
from paperagent.security import LocalSessionTokens


def test_document_revision_api_returns_canonical_body_lineage_and_branch(
    tmp_path: Path,
) -> None:
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data", environment="test")
    tokens = LocalSessionTokens(secret=b"r" * 32)
    document = DocumentIR(
        requirement_id=uuid4(),
        requirement_version=1,
        outline_id=uuid4(),
        title="Revision API",
        language="mixed",
        metadata={"source_conversation_id": "conversation-contract"},
        sections=[
            DocumentSection(
                title="Body",
                goal="API contract",
                blocks=[
                    DocumentBlock(
                        kind=BlockKind.PARAGRAPH,
                        text="Canonical semantic content",
                        provenance=Provenance(agent="test"),
                    )
                ],
            )
        ],
    )
    with TestClient(create_app(settings, tokens)) as client:
        headers = {"Authorization": f"Bearer {tokens.issue()}"}
        project = client.post(
            "/api/projects", headers=headers, json={"name": "revision-api"}
        ).json()
        base = f"/api/projects/{project['id']}"
        saved = client.put(
            f"{base}/documents/{document.document_id}",
            headers=headers,
            json=document.model_dump(mode="json"),
        )
        assert saved.status_code == 200, saved.text
        assert set(saved.json()["hashes"]) == {
            "content_hash",
            "structure_hash",
            "style_hash",
            "asset_set_hash",
            "citation_set_hash",
            "presentation_hash",
            "numbering_hash",
        }

        fetched = client.get(f"{base}/documents/{document.document_id}", headers=headers)
        assert fetched.status_code == 200
        assert "body" in fetched.json() and "sections" not in fetched.json()
        assert fetched.json()["body"][0]["blocks"][0]["text"] == document.sections[0].blocks[0].text

        latest = client.get(
            f"{base}/document-revisions/latest",
            headers=headers,
            params={"conversation_id": "conversation-contract"},
        )
        assert latest.status_code == 200
        assert latest.json()["document_id"] == str(document.document_id)

        lineage = client.get(f"{base}/documents/{document.document_id}/lineage", headers=headers)
        assert lineage.status_code == 200
        assert [item["revision"] for item in lineage.json()] == [1]

        branched = client.post(
            f"{base}/documents/{document.document_id}/branch",
            headers=headers,
            params={"revision": 1},
        )
        assert branched.status_code == 200, branched.text
        assert branched.json()["document"]["document_id"] != str(document.document_id)
        assert branched.json()["document"]["metadata"]["branched_from"]["revision"] == 1
