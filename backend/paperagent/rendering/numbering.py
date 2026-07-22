from __future__ import annotations

from collections import defaultdict
from itertools import pairwise
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from paperagent.schemas.numbering import (
    DiagnosticSeverity,
    NumberingDiagnostic,
    NumberingNormalizer,
    NumberingOwner,
)

if TYPE_CHECKING:
    from paperagent.agents.document_ir import DocumentIR, DocumentSection


class NumberingInspectionReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    diagnostics: list[NumberingDiagnostic] = Field(default_factory=list)


class NumberingInspector:
    """Deterministic canonical QA for numbering ownership and structural labels."""

    def __init__(self) -> None:
        self.normalizer = NumberingNormalizer()

    def inspect(self, document: DocumentIR) -> NumberingInspectionReport:
        diagnostics: list[NumberingDiagnostic] = list(document.numbering.diagnostics)
        self._inspect_owners(document, diagnostics)
        self._inspect_visible_labels(document, diagnostics)
        self._inspect_levels(document, diagnostics)
        self._inspect_sequences(document, diagnostics)
        return NumberingInspectionReport(
            passed=not any(item.severity is DiagnosticSeverity.ERROR for item in diagnostics),
            diagnostics=diagnostics,
        )

    def _inspect_owners(
        self, document: DocumentIR, diagnostics: list[NumberingDiagnostic]
    ) -> None:
        template_active = bool(document.metadata.get("template_heading_numbering_active"))
        renderer_active = bool(document.metadata.get("renderer_heading_numbering_active"))
        owner = document.numbering.headings.owner
        conflict = (owner is NumberingOwner.TEMPLATE and renderer_active) or (
            owner is NumberingOwner.RENDERER and template_active
        )
        if conflict:
            diagnostics.append(
                NumberingDiagnostic(
                    code="NUMBERING_OWNER_CONFLICT",
                    message="标题编号同时由模板与 Renderer 激活。",
                    severity=DiagnosticSeverity.ERROR,
                    category="headings",
                    repair_node="numbering.owner.resolve",
                )
            )

    def _inspect_visible_labels(
        self, document: DocumentIR, diagnostics: list[NumberingDiagnostic]
    ) -> None:
        labels: list[tuple[str, str, str]] = [
            (str(section.section_id), "heading", section.title)
            for section in document.iter_sections()
        ]
        for block in document.iter_blocks():
            if block.kind.value == "heading":
                labels.append((str(block.block_id), "heading", block.text))
            if block.caption and block.kind.value in {"figure", "table", "equation"}:
                labels.append((str(block.block_id), "caption_label", block.caption))
        for node_id, node_kind, label in labels:
            result = self.normalizer.dry_run(label, node_kind=node_kind)
            if result.changed:
                diagnostics.append(
                    NumberingDiagnostic(
                        code=(
                            "DUPLICATE_CAPTION_NUMBER_RISK"
                            if node_kind == "caption_label"
                            else "DUPLICATE_HEADING_NUMBER_RISK"
                        ),
                        message="语义标签仍包含可见结构序号, 渲染时可能产生重复编号。",
                        severity=DiagnosticSeverity.ERROR,
                        category=node_kind,
                        node_id=node_id,
                        original=label,
                        normalized=result.semantic,
                        repair_node="numbering.labels.normalize",
                    )
                )
            elif result.protected_reason:
                diagnostics.append(
                    NumberingDiagnostic(
                        code="NUMBERING_LABEL_PROTECTED",
                        message=f"标签因 {result.protected_reason} 保护规则保持原样。",
                        severity=DiagnosticSeverity.INFO,
                        category=node_kind,
                        node_id=node_id,
                        original=label,
                        normalized=label,
                        repair_node="numbering.labels.clarify",
                    )
                )

    def _inspect_levels(
        self, document: DocumentIR, diagnostics: list[NumberingDiagnostic]
    ) -> None:
        previous = 0
        for section in document.iter_sections():
            if previous and section.level > previous + 1:
                diagnostics.append(
                    NumberingDiagnostic(
                        code="NUMBERING_LEVEL_JUMP",
                        message=f"标题层级从 {previous} 跳到 {section.level}。",
                        severity=DiagnosticSeverity.WARNING,
                        category="headings",
                        node_id=str(section.section_id),
                        repair_node="document.structure.plan",
                    )
                )
            previous = section.level

    def _inspect_sequences(
        self, document: DocumentIR, diagnostics: list[NumberingDiagnostic]
    ) -> None:
        by_parent: dict[str, list[DocumentSection]] = defaultdict(list)

        def collect(items: list[DocumentSection], parent: str) -> None:
            for item in items:
                by_parent[parent].append(item)
                collect(item.children, str(item.section_id))

        collect(document.sections, "root")
        original_by_node = {
            item.node_id: item
            for item in document.numbering.label_map
            if item.node_kind == "heading" and item.prefixes
        }
        for siblings in by_parent.values():
            sequence: list[tuple[int, str]] = []
            for section in siblings:
                label = original_by_node.get(str(section.section_id))
                if not label or not label.prefixes:
                    continue
                first = label.prefixes[0]
                if first.family not in {"arabic", "arabic-decimal", "arabic-parenthesis"}:
                    continue
                try:
                    sequence.append((int(first.levels[-1]), str(section.section_id)))
                except (ValueError, IndexError):
                    continue
            for (before, _), (after, node_id) in pairwise(sequence):
                if after not in {before, before + 1}:
                    diagnostics.append(
                        NumberingDiagnostic(
                            code="NUMBERING_SEQUENCE_GAP",
                            message=f"原始标题序号从 {before} 跳到 {after}。",
                            severity=DiagnosticSeverity.WARNING,
                            category="headings",
                            node_id=node_id,
                            repair_node="numbering.sequence.repair",
                        )
                    )
