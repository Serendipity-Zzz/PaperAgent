from paperagent.api.app import _restore_rendered_markdown_source


def test_restore_rendered_markdown_source_reverses_only_renderer_escapes() -> None:
    title, content = _restore_rendered_markdown_source(
        '---\ntitle: "驻波实验报告"\nlanguage: mixed\n---\n\n'
        "## 正文\n\n"
        r"\## 驻波实验报告" "\n\n"
        r"\**实验目的**: 保留 $\sin(kx)$ 和 `standing\_wave.csv`。"
    )
    assert title == "驻波实验报告"
    assert content.startswith("## 驻波实验报告")
    assert "**实验目的**" in content
    assert r"$\sin(kx)$" in content
    assert "`standing_wave.csv`" in content
