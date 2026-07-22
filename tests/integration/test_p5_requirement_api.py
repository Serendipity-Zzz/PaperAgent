from pathlib import Path

from fastapi.testclient import TestClient

from paperagent.api import create_app
from paperagent.core.config import Settings
from paperagent.security import LocalSessionTokens


def test_requirement_clarification_confirmation_outline_and_plan_api(tmp_path: Path) -> None:
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data", environment="test")
    tokens = LocalSessionTokens(secret=b"r" * 32)
    with TestClient(create_app(settings, tokens)) as client:
        headers = {"Authorization": f"Bearer {tokens.issue()}"}
        project_id = client.post(
            "/api/projects", json={"name": "agent-loop"}, headers=headers
        ).json()["id"]
        vague = client.post(
            f"/api/projects/{project_id}/requirements/analyze",
            json={"text": "帮我写个论文, 要有数据"},
            headers=headers,
        )
        assert vague.status_code == 200
        body = vague.json()
        assert body["requirement"]["status"] == "needs_input"
        assert body["requirement"]["raw_request"]["text"] == "帮我写个论文, 要有数据"
        assert body["requirement"]["normalized_request"]
        assert body["requirement"]["research_formulation"]["research_topic"]
        assert body["requirement"]["open_questions"]

        complete = client.post(
            f"/api/projects/{project_id}/requirements/analyze",
            json={
                "text": (
                    "写一篇 5000 字的本地智能体实验报告, 输出 docx pdf, 引用文献; "
                    "正文使用 Times New Roman 字体, 字号12pt, 标题黑体三号, 行距1.5倍"
                )
            },
            headers=headers,
        ).json()
        confirmed = client.post(
            f"/api/projects/{project_id}/requirements/confirm",
            json={"requirement": complete["requirement"]},
            headers=headers,
        )
        assert confirmed.status_code == 200, confirmed.text
        result = confirmed.json()
        assert result["requirement"]["status"] == "confirmed"
        assert result["requirement"]["confirmed_requirement"]["content_hash"]
        assert result["requirement"]["confirmed_requirement"]["typography"] == {
            "body_font": "Times New Roman",
            "heading_font": "黑体",
            "code_font": None,
            "body_size_pt": 12.0,
            "heading_size_pt": 16.0,
            "line_spacing": 1.5,
            "first_line_indent_chars": None,
        }
        assert result["outline"]["document_type"] == "experiment_report"
        assert sum(item["target_length"] for item in result["outline"]["sections"]) == 5000
        assert any(item["node"] == "experiment" for item in result["plan_preview"])
