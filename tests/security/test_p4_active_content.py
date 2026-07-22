import hashlib
import zipfile
from pathlib import Path

from paperagent.preview.renderers import ArchiveRenderer, HtmlSvgRenderer
from paperagent.preview.service import PreviewService


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_xss_svg_external_content_and_event_handlers_are_removed(tmp_path: Path) -> None:
    corpus = [
        '<svg onload="alert(1)"><script>alert(2)</script><text>Safe SVG</text></svg>',
        '<iframe src="https://evil.test"></iframe><p>Safe iframe text</p>',
        '<a href="javascript:alert(1)">Safe link text</a>',
        '<img src="data:text/html;base64,PHNjcmlwdD4=" onerror="steal()"><p>Safe data</p>',
        '<link rel="stylesheet" href="https://evil.test/x.css"><p>Safe external</p>',
    ]
    renderer = HtmlSvgRenderer()
    for index, sample in enumerate(corpus):
        path = tmp_path / f"attack-{index}.html"
        path.write_text(sample, encoding="utf-8")
        rendered = renderer.render(path, file_id=str(index), source_hash=sha(path))
        assert rendered.payload["process_isolated"] is True
        html = str(rendered.parts[0].payload["html"]).lower()
        assert not any(
            dangerous in html
            for dangerous in (
                "<script",
                "<iframe",
                "javascript:",
                "data:",
                "onerror",
                "onload",
                "https://",
            )
        )
        assert "safe" in str(rendered.parts[0].payload["text"]).lower()


def test_archive_traversal_is_failed_metadata_and_never_extracted(tmp_path: Path) -> None:
    archive = tmp_path / "traversal.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../../outside.txt", "owned")
    service = PreviewService(tmp_path, renderers=(ArchiveRenderer(),))
    artifact = service.render(
        archive, file_id="zip", source_hash=sha(archive), source_name=archive.name
    )
    service.close()
    assert artifact.status == "failed"
    assert artifact.error_code == "PREVIEW_ARCHIVE_TREE_FAILED"
    assert not (tmp_path.parent / "outside.txt").exists()
