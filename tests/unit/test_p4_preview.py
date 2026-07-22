import hashlib
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError
from pypdf import PdfWriter

from paperagent.preview.renderers import (
    CodeRenderer,
    HtmlSvgRenderer,
    ImageRenderer,
    MetadataRenderer,
    PdfRenderer,
)
from paperagent.preview.schemas import Annotation, PreviewAnchor, PreviewStatus
from paperagent.preview.service import PreviewService


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_anchor_round_trip_and_validation() -> None:
    anchor = PreviewAnchor(
        source_file_id="file-1",
        source_hash="a" * 64,
        format="pdf",
        page=2,
        bbox=(0, 0, 100, 200),
        quote="evidence",
    )
    restored = PreviewAnchor.model_validate_json(anchor.model_dump_json())
    assert restored == anchor
    assert restored.valid_for_hash("a" * 64)
    assert not restored.valid_for_hash("b" * 64)
    with pytest.raises(ValidationError, match="format-specific locator"):
        PreviewAnchor(source_file_id="x", source_hash="a" * 64, format="unknown")
    with pytest.raises(ValidationError, match="bbox requires page"):
        PreviewAnchor(
            source_file_id="x",
            source_hash="a" * 64,
            format="pdf",
            json_path="$",
            bbox=(0, 0, 1, 1),
        )


def test_preview_cache_resume_version_invalidation_and_annotations(tmp_path: Path) -> None:
    source = tmp_path / "paper.txt"
    source.write_text("first\nsecond", encoding="utf-8")
    source_hash = digest(source)
    service = PreviewService(tmp_path)
    first = service.render(
        source, file_id="file-1", source_hash=source_hash, source_name=source.name
    )
    cached = service.render(
        source, file_id="file-1", source_hash=source_hash, source_name=source.name
    )
    assert cached.id == first.id
    assert cached.status == PreviewStatus.READY
    annotation = service.annotate(
        Annotation(
            project_id=tmp_path.name,
            artifact_id=first.id,
            anchor=service.parts(str(first.id))[0].anchor,
            body="revise",
        )
    )
    assert annotation.status == "open"
    assert service.clear_cache() == 1
    assert service.annotations("file-1", "b" * 64)[0].status == "orphaned"
    service.close()

    class UpgradedText(CodeRenderer):
        version = "2.0"
        extensions = (".txt",)

    upgraded = PreviewService(tmp_path, renderers=(UpgradedText(),))
    changed = upgraded.render(
        source, file_id="file-1", source_hash=source_hash, source_name=source.name
    )
    assert changed.cache_key != first.cache_key
    upgraded.close()


def test_pdf_image_code_html_and_unknown_renderers(tmp_path: Path) -> None:
    pdf = tmp_path / "rotated.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=120, height=200)
    page.rotate(90)
    with pdf.open("wb") as stream:
        writer.write(stream)
    pdf_result = PdfRenderer().render(pdf, file_id="pdf", source_hash=digest(pdf))
    assert pdf_result.parts[0].payload["rotation"] == 90
    assert pdf_result.parts[0].anchor is not None
    assert pdf_result.parts[0].anchor.bbox == (0.0, 0.0, 120.0, 200.0)

    encrypted = tmp_path / "encrypted.pdf"
    protected = PdfWriter()
    protected.add_blank_page(width=100, height=100)
    protected.encrypt("secret")
    with encrypted.open("wb") as stream:
        protected.write(stream)
    encrypted_result = PdfRenderer().render(
        encrypted, file_id="encrypted", source_hash=digest(encrypted)
    )
    assert encrypted_result.payload["encrypted"] is True
    assert encrypted_result.capabilities == ["system_open"]

    image = tmp_path / "transparent.png"
    Image.new("RGBA", (17, 13), (0, 0, 0, 0)).save(image)
    image_result = ImageRenderer().render(image, file_id="image", source_hash=digest(image))
    assert image_result.payload["mode"] == "RGBA"
    assert image_result.parts[0].anchor is not None

    code = tmp_path / "long.py"
    code.write_text("x = '" + "中" * 10_000 + "'", encoding="utf-8")
    code_result = CodeRenderer().render(code, file_id="code", source_hash=digest(code))
    assert code_result.payload["read_only"] is True
    assert code_result.parts[0].anchor.line_start == 1

    active = tmp_path / "active.html"
    active.write_text(
        '<script>alert(1)</script><iframe src="https://evil.test"></iframe>'
        '<p onclick="steal()">Safe text<img src="data:text/html,bad"></p>',
        encoding="utf-8",
    )
    safe = HtmlSvgRenderer().render(active, file_id="html", source_hash=digest(active))
    rendered = str(safe.parts[0].payload["html"])
    assert "Safe text" in rendered
    assert all(value not in rendered.lower() for value in ("script", "iframe", "onclick", "data:"))

    unknown = tmp_path / "macro.docm"
    unknown.write_bytes(b"not executed")
    metadata = MetadataRenderer().render(unknown, file_id="unknown", source_hash=digest(unknown))
    assert metadata.capabilities == ["system_open"]
    assert metadata.payload["reason"]

    broken_pdf = tmp_path / "broken.pdf"
    broken_pdf.write_bytes(b"%PDF-1.7\nbroken")
    service = PreviewService(tmp_path)
    degraded = service.render(
        broken_pdf,
        file_id="broken",
        source_hash=digest(broken_pdf),
        source_name=broken_pdf.name,
    )
    service.close()
    assert degraded.status == "failed"
    assert degraded.fidelity == "metadata"
