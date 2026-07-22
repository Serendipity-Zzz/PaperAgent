from __future__ import annotations

import json
from pathlib import Path

from paperagent.rendering.markdown_parser import strip_author_heading_number

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "document_typography"


def _corpus() -> dict[str, object]:
    return json.loads((FIXTURE_ROOT / "numbering-corpus.json").read_text(encoding="utf-8"))


def _expanded_positive_cases() -> list[tuple[str, str]]:
    corpus = _corpus()
    titles = corpus["semantic_titles"]
    prefixes = corpus["prefix_cases"]
    assert isinstance(titles, list)
    assert isinstance(prefixes, list)
    cases: list[tuple[str, str]] = []
    for item in prefixes:
        assert isinstance(item, dict)
        prefix = item["prefix"]
        assert isinstance(prefix, str)
        for title in titles:
            assert isinstance(title, str)
            cases.append((prefix + title, title))
    compounds = corpus["compound_cases"]
    assert isinstance(compounds, list)
    for item in compounds:
        assert isinstance(item, dict)
        source = item["input"]
        semantic = item["semantic"]
        assert isinstance(source, str)
        assert isinstance(semantic, str)
        cases.append((source, semantic))
    return cases


def test_p13_numbering_corpus_expands_to_at_least_one_hundred_cases() -> None:
    corpus = _corpus()
    positives = _expanded_positive_cases()
    protected = corpus["protected_cases"]
    assert isinstance(protected, list)

    assert len(positives) >= 100
    assert len(protected) >= 30
    assert {item["family"] for item in corpus["prefix_cases"]} >= {
        "arabic",
        "arabic-decimal",
        "chinese",
        "chinese-parenthesis",
        "roman-upper",
        "alphabetic",
        "appendix",
    }


def test_current_heading_cleaner_supports_its_existing_narrow_contract() -> None:
    assert strip_author_heading_number("1. 实验目的") == "实验目的"
    assert strip_author_heading_number("1.2 实验方法") == "实验方法"
    assert strip_author_heading_number("第一章 绪论") == "绪论"


def test_normalizer_removes_every_supported_structural_prefix() -> None:
    mismatches = [
        (source, expected, strip_author_heading_number(source))
        for source, expected in _expanded_positive_cases()
        if strip_author_heading_number(source) != expected
    ]
    assert not mismatches, mismatches[:10]


def test_normalizer_preserves_every_protected_label() -> None:
    protected = _corpus()["protected_cases"]
    assert isinstance(protected, list)
    changed = []
    for item in protected:
        assert isinstance(item, dict)
        source = item["input"]
        assert isinstance(source, str)
        normalized = strip_author_heading_number(source)
        if normalized != source:
            changed.append((source, normalized, item["reason"]))
    assert not changed, changed
