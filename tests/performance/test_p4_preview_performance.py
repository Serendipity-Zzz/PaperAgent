import hashlib
import time
from pathlib import Path

from paperagent.preview.service import PreviewService


def test_cached_preview_first_shell_under_500ms(tmp_path: Path) -> None:
    source = tmp_path / "large.py"
    source.write_text(
        "\n".join(f"value_{index} = {index}" for index in range(10_000)), encoding="utf-8"
    )
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    service = PreviewService(tmp_path)
    service.render(source, file_id="large", source_hash=source_hash, source_name=source.name)
    started = time.perf_counter()
    artifact = service.render(
        source, file_id="large", source_hash=source_hash, source_name=source.name
    )
    elapsed = time.perf_counter() - started
    first_page = service.parts(str(artifact.id), limit=100)
    service.close()
    assert elapsed < 0.5
    assert len(first_page) == 100
