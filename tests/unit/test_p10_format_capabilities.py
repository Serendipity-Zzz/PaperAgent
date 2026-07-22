from paperagent.rendering.capabilities import (
    FORMAT_CAPABILITIES,
    Fidelity,
    OutputFormat,
    SemanticElement,
    capability_for,
)


def test_capability_matrix_is_total_and_machine_readable() -> None:
    assert set(FORMAT_CAPABILITIES) == set(SemanticElement)
    for element, formats in FORMAT_CAPABILITIES.items():
        assert set(formats) == set(OutputFormat), element
        for capability in formats.values():
            assert capability.representation.strip()
            if capability.fidelity in {Fidelity.DEGRADED, Fidelity.UNSUPPORTED}:
                assert capability.limitation


def test_page_only_features_are_explicitly_unsupported_in_portable_markdown() -> None:
    for element in (
        SemanticElement.HEADER,
        SemanticElement.FOOTER,
        SemanticElement.PAGE_NUMBER,
    ):
        assert capability_for(element, OutputFormat.MARKDOWN).fidelity is Fidelity.UNSUPPORTED


def test_paginated_formats_have_native_figures_and_page_numbers() -> None:
    for output in (
        OutputFormat.DOCX,
        OutputFormat.XELATEX_PDF,
        OutputFormat.WORD_PDF,
    ):
        assert capability_for(SemanticElement.FIGURE, output).fidelity is Fidelity.EXACT
        assert capability_for(SemanticElement.PAGE_NUMBER, output).fidelity is Fidelity.EXACT
