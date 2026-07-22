import subprocess
from pathlib import Path
from uuid import uuid4

from tests.unit.test_p6_rendering import document

from paperagent.preview.schemas import PreviewAnchor
from paperagent.rendering.artifacts import AnchorBinding, ArtifactVersionService
from paperagent.rendering.translation import (
    GlossaryTerm,
    PdfMathTranslateAdapter,
    TranslationAgent,
)


def test_translation_protects_formula_code_citation_number_unit_and_glossary() -> None:
    source = document()
    source.sections[0].blocks[0].text = "智能体在 20 °C 得到 95% [1], 公式 $E=mc^2$."
    translated, report = TranslationAgent().translate(
        source,
        lambda text: text.replace("在", "at").replace("得到", "obtains"),
        direction="zh_to_en",
        glossary=[GlossaryTerm(source="智能体", target="agent", confirmed=True)],
    )
    text = translated.sections[0].blocks[0].text
    assert "agent" in text
    assert "20 °C" in text and "95%" in text and "[1]" in text and "$E=mc^2$" in text
    assert translated.sections[0].blocks[3].text == "print('safe')"
    assert report.protected_tokens >= 4
    bilingual, _ = TranslationAgent().translate(
        source, lambda text: "translated " + text, direction="zh_to_en", bilingual=True
    )
    assert bilingual.language == "mixed"
    assert "\n\ntranslated" in bilingual.sections[0].blocks[0].text


def test_pdfmathtranslate_next_missing_key_cancel_version_and_success(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"pdf")

    def runner(command: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
        del timeout
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, "pdf2zh-next 2.0 AGPL-3.0", "")
        output = Path(command[-1])
        output.mkdir(parents=True, exist_ok=True)
        (output / "translated.pdf").write_bytes(b"translated")
        return subprocess.CompletedProcess(command, 0, "ok", "")

    missing = PdfMathTranslateAdapter(executable="")
    missing.executable = None
    assert (
        missing.translate(
            source, tmp_path / "missing", language="zh", api_configured=True
        ).error_code
        == "PDF2ZH_MISSING"
    )
    adapter = PdfMathTranslateAdapter(executable="pdf2zh", runner=runner, environment=tmp_path)
    assert "AGPL" in adapter.version()
    assert (
        adapter.translate(
            source, tmp_path / "nokey", language="zh", api_configured=False
        ).error_code
        == "PDF2ZH_NO_KEY"
    )
    assert (
        adapter.translate(
            source, tmp_path / "cancel", language="zh", api_configured=True, cancelled=lambda: True
        ).error_code
        == "CANCELLED"
    )
    assert adapter.translate(source, tmp_path / "ok", language="zh", api_configured=True).success


def test_artifact_versions_anchor_relocation_diff_and_local_rerender(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = document()
    first_path = project / "paper-v1.md"
    first_path.write_text("v1", encoding="utf-8")
    service = ArtifactVersionService(project)
    first = service.register(source, first_path)
    block = source.sections[0].blocks[0]
    anchor = PreviewAnchor(
        source_file_id="artifact",
        source_hash=first.sha256,
        format="md",
        line_start=1,
        quote="visible quote",
    )
    binding = AnchorBinding(
        artifact_id=first.artifact_id,
        anchor=anchor,
        section_id=source.sections[0].section_id,
        block_id=block.block_id,
    )
    service.bind(binding)
    moved = anchor.model_copy(update={"id": uuid4(), "line_start": 20})
    assert service.locate(first.artifact_id, moved).block_id == block.block_id
    changed = source.patch_block(block.block_id, {"text": "changed"})
    second_path = project / "paper-v2.md"
    second_path.write_text("v2", encoding="utf-8")
    second = service.register(changed, second_path, parent=first, previous_document=source)
    assert second.version == 2 and second.changed_block_ids == [block.block_id]
    calls: list[list[object]] = []

    def renderer(_document, blocks):
        calls.append(blocks)
        return second_path

    assert service.local_rerender(changed, [block.block_id], renderer) == second_path
    assert calls == [[block.block_id]]
    service.close()
