import json
import logging
from pathlib import Path

from paperagent.core.logging import configure_logging, redact


def test_recursive_redaction() -> None:
    value = {
        "Authorization": "Bearer live-secret-value",
        "nested": {"api_key": "credential-for-redaction-test", "safe": "kept"},
    }
    result = redact(value)
    assert result["Authorization"] == "[REDACTED]"
    assert result["nested"]["api_key"] == "[REDACTED]"
    assert result["nested"]["safe"] == "kept"


def test_json_log_does_not_leak_secret(tmp_path: Path) -> None:
    log_file = tmp_path / "app.jsonl"
    configure_logging(output_format="json", log_file=log_file)
    logging.getLogger("test").warning(
        {"api-key": "credential-should-never-appear", "message": "hello"}
    )
    content = log_file.read_text(encoding="utf-8")
    payload = json.loads(content)
    assert "credential-should-never-appear" not in content
    assert "REDACTED" in payload["message"]
