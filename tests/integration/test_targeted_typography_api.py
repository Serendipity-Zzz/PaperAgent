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


def test_annotation_typography_revision_outputs_are_previewable(tmp_path: Path) -> None:
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data", environment="test")
    tokens = LocalSessionTokens(secret=b"t" * 32)
    block = DocumentBlock(
        kind=BlockKind.PARAGRAPH,
        text="这段正文用于稳定锚点测试",
        provenance=Provenance(agent="test"),
    )
    document = DocumentIR(
        requirement_id=uuid4(),
        requirement_version=1,
        outline_id=uuid4(),
        title="定向修改",
        language="zh",
        sections=[DocumentSection(title="正文", goal="测试", blocks=[block])],
    )
    with TestClient(create_app(settings, tokens)) as client:
        headers = {"Authorization": f"Bearer {tokens.issue()}"}
        project = client.post(
            "/api/projects", headers=headers, json={"name": "typography-api"}
        ).json()
        base = f"/api/projects/{project['id']}"
        saved = client.put(
            f"{base}/documents/{document.document_id}",
            headers=headers,
            json=document.model_dump(mode="json"),
        )
        assert saved.status_code == 200

        revised = client.post(
            f"{base}/documents/{document.document_id}/typography/from-annotation",
            headers=headers,
            json={
                "anchor": {
                    "source_file_id": "generated-output",
                    "source_hash": "a" * 64,
                    "format": "pdf",
                    "page": 1,
                    "quote": block.text,
                },
                "body": "正文字号设为12pt, 行距1.5倍",
                "formats": ["md", "docx"],
            },
        )
        assert revised.status_code == 200, revised.text
        result = revised.json()
        assert result["document"]["revision"] == 2
        assert result["invalidation"]["affected_block_ids"] == [str(block.block_id)]
        assert not result["invalidation"]["regenerate_text"]
        assert len(result["files"]) == 2

        files = client.get(f"{base}/files", headers=headers).json()
        output = next(item for item in files if item["original_name"].endswith(".docx"))
        preview = client.post(f"{base}/preview/{output['id']}", headers=headers)
        assert preview.status_code == 200
        provenance = preview.json()["payload"]["options"]["provenance"]
        assert provenance["document_id"] == str(document.document_id)

        missing = client.post(
            f"{base}/documents/{document.document_id}/typography",
            headers=headers,
            json={
                "intent": {
                    "scope": "global",
                    "typography_patch": {"body_font": "Definitely Missing Font 9000"},
                },
                "formats": ["docx"],
            },
        )
        assert missing.status_code == 409
        assert missing.json()["detail"]["code"] == "FONT_ACTION_REQUIRED"
