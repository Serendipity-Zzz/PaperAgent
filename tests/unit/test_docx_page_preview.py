from __future__ import annotations

import subprocess
from pathlib import Path

from paperagent.preview.docx_pages import DocxPagePreviewService
from paperagent.rendering.pdf_modes import WordParityAdapter


def test_docx_page_preview_is_page_pdf_and_cached_by_hash(tmp_path: Path) -> None:
    source = tmp_path / "report.docx"
    source.write_bytes(b"docx fixture")
    calls = 0

    def runner(command: list[str], _cwd: Path, _timeout: int) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        Path(command[-1]).write_bytes(b"%PDF-1.7\n%%EOF")
        return subprocess.CompletedProcess(command, 0, "ok", "")

    service = DocxPagePreviewService(
        tmp_path,
        adapter=WordParityAdapter(runner=runner),
    )
    first = service.convert(source, "a" * 64)
    second = service.convert(source, "a" * 64)
    assert first.success and first.path is not None and first.path.suffix == ".pdf"
    assert second.success and second.engine == "cached"
    assert first.cache_key == second.cache_key
    assert calls == 1
