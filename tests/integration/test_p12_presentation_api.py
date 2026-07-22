from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

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
from paperagent.db.models import EventRecord
from paperagent.presentation import presentation_from_requirement
from paperagent.schemas.presentation import (
    RequirementCoverField,
    RequirementCoverSpec,
    RequirementPageChromeSpec,
    RequirementPresentationSpec,
)
from paperagent.security import LocalSessionTokens


def _document() -> DocumentIR:
    document_id = uuid4()
    return DocumentIR(
        document_id=document_id,
        requirement_id=uuid4(),
        requirement_version=1,
        outline_id=uuid4(),
        title="驻波实验报告",
        language="zh",
        presentation=presentation_from_requirement(
            RequirementPresentationSpec(
                cover=RequirementCoverSpec(
                    enabled=True,
                    fields=[
                        RequirementCoverField(
                            semantic_key="author",
                            label="姓名",
                            value="合成用户甲",
                        ),
                        RequirementCoverField(
                            semantic_key="institution",
                            label="学校",
                            value="合成大学甲",
                        ),
                    ],
                ),
                page_chrome=RequirementPageChromeSpec(
                    header_center="大学物理实验报告",
                    page_number=True,
                    total_pages=True,
                    hide_on_cover=True,
                ),
            ),
            document_id=document_id,
        ),
        sections=[
            DocumentSection(
                title="正文",
                goal="API contract",
                blocks=[
                    DocumentBlock(
                        kind=BlockKind.PARAGRAPH,
                        text="正文保持不变。",
                        provenance=Provenance(agent="test"),
                    )
                ],
            )
        ],
    )


def test_presentation_summary_preview_patch_and_private_activity(tmp_path: Path) -> None:
    settings = Settings(
        project_root=tmp_path / "repo",
        data_dir=tmp_path / "data",
        environment="test",
    )
    tokens = LocalSessionTokens(secret=b"p" * 32)
    document = _document()
    with TestClient(create_app(settings, tokens)) as client:
        headers = {"Authorization": f"Bearer {tokens.issue()}"}
        project = client.post(
            "/api/projects",
            headers=headers,
            json={"name": "presentation-api"},
        ).json()
        base = f"/api/projects/{project['id']}"
        saved = client.put(
            f"{base}/documents/{document.document_id}",
            headers=headers,
            json=document.model_dump(mode="json"),
        )
        assert saved.status_code == 200, saved.text

        summary_response = client.get(
            f"{base}/documents/{document.document_id}/presentation",
            headers=headers,
        )
        assert summary_response.status_code == 200
        summary = summary_response.json()
        assert summary["revision"] == 1
        assert summary["presentation"]["cover"]["fields"][0]["value"] == "合成用户甲"
        assert summary["impact"] == {
            "rewrites_content": False,
            "reruns_experiment": False,
            "reruns_assets": False,
        }

        preview = client.get(
            f"{base}/documents/{document.document_id}/presentation-preview",
            headers=headers,
        )
        assert preview.status_code == 200
        assert "paper-cover" in preview.json()["html"]
        assert preview.json()["presentation_hash"] == summary["presentation_hash"]

        patched = client.post(
            f"{base}/documents/{document.document_id}/presentation/patch",
            headers=headers,
            json={
                "revision": 1,
                "operations": [
                    {
                        "kind": "upsert_cover_field",
                        "semantic_key": "institution",
                        "label": "学校",
                        "value": "合成大学乙",
                    }
                ],
                "formats": ["md"],
            },
        )
        assert patched.status_code == 200, patched.text
        result = patched.json()
        assert result["summary"]["revision"] == 2
        assert result["summary"]["content_hash"] == summary["content_hash"]
        assert result["summary"]["asset_set_hash"] == summary["asset_set_hash"]
        assert result["summary"]["presentation_hash"] != summary["presentation_hash"]
        assert result["rerendered_formats"] == ["md"]
        assert result["render_errors"] == {}
        assert len(result["artifacts"]) == 1

        conflict = client.post(
            f"{base}/documents/{document.document_id}/presentation/patch",
            headers=headers,
            json={
                "revision": 1,
                "operations": [{"kind": "set_cover_title", "value": "过期修改"}],
            },
        )
        assert conflict.status_code == 409

    databases = DatabaseManager(settings)
    with databases.project_session(project["id"]) as session:
        event = session.scalar(
            select(EventRecord).where(
                EventRecord.type == "document.presentation_revised"
            )
        )
        assert event is not None
        public_payload = json.loads(event.payload_json)
        assert public_payload["field_count"] == 2
        serialized = json.dumps(public_payload, ensure_ascii=False)
        assert "合成用户甲" not in serialized
        assert "合成大学乙" not in serialized
