from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from paperagent.skills.registry import ProgressiveSkillLoader

BUILTIN_ROOT = (
    Path(__file__).resolve().parents[2]
    / "backend"
    / "paperagent"
    / "builtin_skills"
)


def test_layout_skill_uses_progressive_metadata_instruction_reference_loading() -> None:
    loader = ProgressiveSkillLoader(BUILTIN_ROOT)
    catalog = loader.catalog()
    assert [item.id for item in catalog] == ["professional-document-layout"]
    assert loader.match("分析普通 Python 函数") == []
    matched = loader.match("请调整实验报告的标题编号和 DOCX 排版")
    assert [item.id for item in matched] == ["professional-document-layout"]

    loaded = loader.load(matched[0].id)
    assert "Choose the shortest valid path" in loaded.instructions
    assert loaded.loaded_references == {}
    assert set(loaded.available_references) == {
        "references/contracts.md",
        "references/qa-and-repair.md",
    }
    with_contracts = loader.load_reference(loaded, "references/contracts.md")
    assert "Revision invariants" in with_contracts.loaded_references[
        "references/contracts.md"
    ]
    assert loaded.loaded_references == {}


def test_layout_skill_rejects_reference_traversal_and_permission_escalation(
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "builtin_skills"
    shutil.copytree(BUILTIN_ROOT, copied_root)
    loader = ProgressiveSkillLoader(copied_root)
    loaded = loader.load("professional-document-layout")
    with pytest.raises(ValueError, match="not declared"):
        loader.load_reference(loaded, "../secret.md")

    manifest_path = copied_root / "professional-document-layout" / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["permissions"] = ["shell"]
    manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    unsafe = ProgressiveSkillLoader(copied_root)
    with pytest.raises(PermissionError, match="cannot request"):
        unsafe.load("professional-document-layout")
