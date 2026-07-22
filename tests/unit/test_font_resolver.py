from pathlib import Path

from paperagent.rendering.fonts import FontResolver


def test_font_resolver_requires_approval_instead_of_silent_substitution(
    tmp_path: Path,
) -> None:
    (tmp_path / "SimSun.ttf").write_bytes(b"fixture")
    resolver = FontResolver([tmp_path])
    exact = resolver.resolve("SimSun")
    assert exact.installed and exact.resolved == "SimSun"
    missing = resolver.resolve("Uninstalled Research Font")
    assert missing.requires_user_action and missing.resolved is None


def test_font_alias_fallback_requires_explicit_allowance(tmp_path: Path) -> None:
    (tmp_path / "Noto Serif CJK SC.otf").write_bytes(b"fixture")
    resolver = FontResolver([tmp_path])
    assert resolver.resolve("宋体").requires_user_action
    approved = resolver.resolve("宋体", allow_fallback=True)
    assert approved.fallback_used and approved.resolved == "Noto Serif CJK SC"
