from __future__ import annotations

import hashlib
import json
import re
from enum import IntEnum, StrEnum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from paperagent.rendering.fonts import FontResolution, FontResolver


class ArchetypeId(StrEnum):
    ACADEMIC_PAPER = "academic-paper"
    EXPERIMENT_REPORT = "experiment-report"
    TECHNICAL_DOCUMENT = "technical-document"
    BUSINESS_REPORT = "business-report"
    PRODUCT_PLAN = "product-plan"
    MEETING_MINUTES = "meeting-minutes"
    TUTORIAL = "tutorial"
    RESEARCH_REPORT = "research-report"
    FORMAL_DOCUMENT = "formal-document"
    PRACTICE_REPORT = "practice-report"


class DocumentArchetype(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: ArchetypeId
    display_name: str
    required_sections: tuple[str, ...]
    optional_sections: tuple[str, ...]
    default_components: tuple[str, ...]
    numbering: str
    qa_rules: tuple[str, ...]
    strict_official_standard: bool = False


ARCHETYPES: dict[ArchetypeId, DocumentArchetype] = {
    ArchetypeId.ACADEMIC_PAPER: DocumentArchetype(
        id=ArchetypeId.ACADEMIC_PAPER,
        display_name="学术论文",
        required_sections=("摘要", "正文", "参考文献"),
        optional_sections=("关键词", "致谢", "附录"),
        default_components=("cover", "abstract", "toc", "citations"),
        numbering="decimal-sections",
        qa_rules=("abstract-required", "citations-resolved", "references-required"),
    ),
    ArchetypeId.EXPERIMENT_REPORT: DocumentArchetype(
        id=ArchetypeId.EXPERIMENT_REPORT,
        display_name="实验报告",
        required_sections=("实验目的", "实验方法", "实验结果", "结论"),
        optional_sections=("理论基础", "误差分析", "参考文献"),
        default_components=("cover", "toc", "figures", "tables", "equations"),
        numbering="decimal-sections",
        qa_rules=("method-required", "result-artifacts-valid", "figure-caption-pairs"),
    ),
    ArchetypeId.TECHNICAL_DOCUMENT: DocumentArchetype(
        id=ArchetypeId.TECHNICAL_DOCUMENT,
        display_name="技术文档",
        required_sections=("概述", "架构", "实现"),
        optional_sections=("接口", "部署", "故障排查", "FAQ"),
        default_components=("toc", "code", "diagrams", "tables"),
        numbering="decimal-sections",
        qa_rules=("heading-hierarchy", "code-language-tags", "links-valid"),
    ),
    ArchetypeId.BUSINESS_REPORT: DocumentArchetype(
        id=ArchetypeId.BUSINESS_REPORT,
        display_name="商业报告",
        required_sections=("执行摘要", "关键发现", "结论"),
        optional_sections=("市场分析", "风险", "建议", "附录"),
        default_components=("cover", "toc", "figures", "tables"),
        numbering="decimal-sections",
        qa_rules=("executive-summary", "data-sources", "recommendations-actionable"),
    ),
    ArchetypeId.PRODUCT_PLAN: DocumentArchetype(
        id=ArchetypeId.PRODUCT_PLAN,
        display_name="产品方案",
        required_sections=("背景", "目标", "方案", "里程碑"),
        optional_sections=("用户故事", "流程", "风险", "验收"),
        default_components=("cover", "toc", "flows", "tables"),
        numbering="decimal-sections",
        qa_rules=("goals-measurable", "milestones-present", "acceptance-present"),
    ),
    ArchetypeId.MEETING_MINUTES: DocumentArchetype(
        id=ArchetypeId.MEETING_MINUTES,
        display_name="会议纪要",
        required_sections=("会议信息", "议题", "决定", "行动项"),
        optional_sections=("讨论摘要", "附件"),
        default_components=("metadata-table", "action-table"),
        numbering="none",
        qa_rules=("participants-present", "owners-and-dates"),
    ),
    ArchetypeId.TUTORIAL: DocumentArchetype(
        id=ArchetypeId.TUTORIAL,
        display_name="教程",
        required_sections=("目标", "前置条件", "步骤", "总结"),
        optional_sections=("示例", "注意事项", "FAQ"),
        default_components=("toc", "ordered-steps", "code", "callouts"),
        numbering="decimal-sections",
        qa_rules=("steps-ordered", "prerequisites-present", "examples-valid"),
    ),
    ArchetypeId.RESEARCH_REPORT: DocumentArchetype(
        id=ArchetypeId.RESEARCH_REPORT,
        display_name="课题/研究报告",
        required_sections=("研究背景", "研究方法", "研究结果", "结论"),
        optional_sections=("文献综述", "建议", "参考文献", "附录"),
        default_components=("cover", "abstract", "toc", "citations", "figures"),
        numbering="decimal-sections",
        qa_rules=("method-result-consistency", "sources-verified", "conclusion-supported"),
    ),
    ArchetypeId.FORMAL_DOCUMENT: DocumentArchetype(
        id=ArchetypeId.FORMAL_DOCUMENT,
        display_name="一般正式文书",
        required_sections=("标题", "正文", "落款"),
        optional_sections=("主送", "附件", "日期"),
        default_components=("formal-header", "body", "signature"),
        numbering="chinese-levels",
        qa_rules=("formal-spacing", "signature-present"),
        strict_official_standard=False,
    ),
    ArchetypeId.PRACTICE_REPORT: DocumentArchetype(
        id=ArchetypeId.PRACTICE_REPORT,
        display_name="实践报告",
        required_sections=("实践背景", "实践过程", "成果", "总结"),
        optional_sections=("现场记录", "问题与改进", "附录"),
        default_components=("cover", "toc", "figures", "timeline"),
        numbering="decimal-sections",
        qa_rules=("process-evidence", "image-provenance", "reflection-present"),
    ),
}


class ArchetypeDecision(BaseModel):
    archetype: ArchetypeId
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)
    alternatives: list[ArchetypeId] = Field(default_factory=list)
    confirmation_required: bool = False
    source: str


class DocumentArchetypeClassifier:
    KEYWORDS: ClassVar[dict[ArchetypeId, tuple[str, ...]]] = {
        ArchetypeId.ACADEMIC_PAPER: ("论文", "摘要", "参考文献", "paper", "thesis"),
        ArchetypeId.EXPERIMENT_REPORT: ("实验报告", "实验结果", "实验图", "experiment"),
        ArchetypeId.TECHNICAL_DOCUMENT: ("技术文档", "架构", "接口", "部署", "technical"),
        ArchetypeId.BUSINESS_REPORT: ("商业报告", "市场", "经营", "business"),
        ArchetypeId.PRODUCT_PLAN: ("产品方案", "用户故事", "里程碑", "prd"),
        ArchetypeId.MEETING_MINUTES: ("会议纪要", "参会", "行动项", "minutes"),
        ArchetypeId.TUTORIAL: ("教程", "步骤", "前置条件", "tutorial"),
        ArchetypeId.RESEARCH_REPORT: ("课题报告", "研究报告", "调研", "research report"),
        ArchetypeId.FORMAL_DOCUMENT: ("正式文书", "公文", "落款", "通知"),
        ArchetypeId.PRACTICE_REPORT: ("实践报告", "实习报告", "现场", "practice report"),
    }

    def classify(
        self,
        request: str,
        *,
        explicit: ArchetypeId | None = None,
        structured: dict[str, object] | None = None,
        template_uploaded: bool = False,
    ) -> ArchetypeDecision:
        if explicit is not None:
            return ArchetypeDecision(
                archetype=explicit,
                confidence=1,
                evidence=["user-explicit"],
                source="user",
            )
        if structured is not None:
            decision = ArchetypeDecision.model_validate(structured | {"source": "llm"})
            return decision.model_copy(
                update={
                    "confirmation_required": decision.confirmation_required
                    or decision.confidence < 0.72
                    or template_uploaded,
                }
            )
        lowered = request.casefold()
        scores = {
            archetype: [keyword for keyword in keywords if keyword.casefold() in lowered]
            for archetype, keywords in self.KEYWORDS.items()
        }
        ranked = sorted(scores, key=lambda item: (-len(scores[item]), item.value))
        winner = ranked[0] if scores[ranked[0]] else ArchetypeId.RESEARCH_REPORT
        matches = scores[winner]
        confidence = min(0.88, 0.45 + len(matches) * 0.16)
        alternatives = [item for item in ranked[1:3] if scores[item]]
        return ArchetypeDecision(
            archetype=winner,
            confidence=confidence,
            evidence=matches or ["deterministic-fallback"],
            alternatives=alternatives,
            confirmation_required=confidence < 0.72 or template_uploaded,
            source="fallback",
        )


class LengthUnit(StrEnum):
    PT = "pt"
    MM = "mm"
    CM = "cm"
    IN = "in"
    CH = "ch"
    EM = "em"


class LengthValue(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: float
    unit: LengthUnit

    def points(self, *, font_size_pt: float = 12) -> float:
        factors = {
            LengthUnit.PT: 1,
            LengthUnit.MM: 72 / 25.4,
            LengthUnit.CM: 72 / 2.54,
            LengthUnit.IN: 72,
            LengthUnit.CH: font_size_pt,
            LengthUnit.EM: font_size_pt,
        }
        return self.value * factors[self.unit]


def mm(value: float) -> LengthValue:
    return LengthValue(value=value, unit=LengthUnit.MM)


class Orientation(StrEnum):
    PORTRAIT = "portrait"
    LANDSCAPE = "landscape"


class PageSpec(BaseModel):
    width: LengthValue = Field(default_factory=lambda: mm(210))
    height: LengthValue = Field(default_factory=lambda: mm(297))
    orientation: Orientation = Orientation.PORTRAIT
    top_margin: LengthValue = Field(default_factory=lambda: mm(25.4))
    bottom_margin: LengthValue = Field(default_factory=lambda: mm(25.4))
    left_margin: LengthValue = Field(default_factory=lambda: mm(31.8))
    right_margin: LengthValue = Field(default_factory=lambda: mm(31.8))
    gutter: LengthValue = Field(default_factory=lambda: mm(0))
    mirror_margins: bool = False
    columns: int = Field(default=1, ge=1, le=6)
    different_first_page: bool = True
    different_odd_even: bool = False

    @property
    def content_width_pt(self) -> float:
        page_width = self.width.points()
        if self.orientation is Orientation.LANDSCAPE:
            page_width = self.height.points()
        return (
            page_width
            - self.left_margin.points()
            - self.right_margin.points()
            - self.gutter.points()
        )


class SectionPageSpec(BaseModel):
    section_id: str | None = None
    page: PageSpec = Field(default_factory=PageSpec)
    start: str = Field(default="next_page", pattern=r"^(continuous|next_page|odd_page|even_page)$")


class NamedStyle(StrEnum):
    DOCUMENT_TITLE = "DocumentTitle"
    SUBTITLE = "Subtitle"
    HEADING_1 = "Heading1"
    HEADING_2 = "Heading2"
    HEADING_3 = "Heading3"
    HEADING_4 = "Heading4"
    HEADING_5 = "Heading5"
    HEADING_6 = "Heading6"
    BODY_TEXT = "BodyText"
    LIST = "List"
    QUOTE = "Quote"
    CAPTION = "Caption"
    TABLE = "Table"
    CODE = "Code"
    EQUATION = "Equation"
    REFERENCE = "Reference"
    HEADER = "Header"
    FOOTER = "Footer"


class StyleProperties(BaseModel):
    font_family: str = "Times New Roman"
    east_asia_font: str = "宋体"
    fallback_fonts: list[str] = Field(default_factory=lambda: ["Noto Serif CJK SC"])
    font_size_pt: float = Field(default=11, ge=5, le=120)
    bold: bool = False
    italic: bool = False
    color: str = Field(default="#000000", pattern=r"^#[0-9A-Fa-f]{6}$")
    alignment: str = Field(default="justify", pattern=r"^(left|center|right|justify)$")
    line_spacing: float = Field(default=1.5, ge=0.8, le=4)
    space_before_pt: float = Field(default=0, ge=0)
    space_after_pt: float = Field(default=0, ge=0)
    first_line_indent: LengthValue = Field(
        default_factory=lambda: LengthValue(value=2, unit=LengthUnit.CH)
    )
    left_indent: LengthValue = Field(default_factory=lambda: mm(0))
    right_indent: LengthValue = Field(default_factory=lambda: mm(0))
    border: str = "none"
    shading: str | None = None
    keep_with_next: bool = False
    keep_together: bool = False
    page_break_before: bool = False
    widow_orphan: bool = True


def default_style_sheet() -> dict[NamedStyle, StyleProperties]:
    body = StyleProperties()
    styles = {style: body.model_copy(deep=True) for style in NamedStyle}
    styles[NamedStyle.DOCUMENT_TITLE] = body.model_copy(
        update={"font_size_pt": 22, "bold": True, "alignment": "center", "first_line_indent": mm(0)}
    )
    styles[NamedStyle.SUBTITLE] = body.model_copy(
        update={"font_size_pt": 14, "alignment": "center", "first_line_indent": mm(0)}
    )
    for level, name in enumerate(
        (
            NamedStyle.HEADING_1,
            NamedStyle.HEADING_2,
            NamedStyle.HEADING_3,
            NamedStyle.HEADING_4,
            NamedStyle.HEADING_5,
            NamedStyle.HEADING_6,
        ),
        start=1,
    ):
        styles[name] = body.model_copy(
            update={
                "font_size_pt": max(11, 18 - level * 1.5),
                "bold": True,
                "alignment": "left",
                "space_before_pt": max(6, 20 - level * 2),
                "space_after_pt": 6,
                "first_line_indent": mm(0),
                "keep_with_next": True,
                # Headings keep with their following paragraph, but ordinary
                # sections flow continuously. Cover/TOC and explicit page or
                # section break blocks own pagination instead of the style.
                "page_break_before": False,
            }
        )
    styles[NamedStyle.CAPTION] = body.model_copy(
        update={
            "font_size_pt": 9,
            "alignment": "center",
            "first_line_indent": mm(0),
            "keep_with_next": True,
        }
    )
    styles[NamedStyle.CODE] = body.model_copy(
        update={
            "font_family": "Cascadia Mono",
            "east_asia_font": "等线",
            "font_size_pt": 9,
            "alignment": "left",
            "first_line_indent": mm(0),
            "shading": "#F3F4F6",
        }
    )
    styles[NamedStyle.HEADER] = body.model_copy(
        update={"font_size_pt": 9, "alignment": "center", "first_line_indent": mm(0)}
    )
    styles[NamedStyle.FOOTER] = styles[NamedStyle.HEADER].model_copy(deep=True)
    return styles


class StyleSheet(BaseModel):
    styles: dict[NamedStyle, StyleProperties] = Field(default_factory=default_style_sheet)

    @model_validator(mode="after")
    def all_named_styles_are_explicit(self) -> StyleSheet:
        missing = set(NamedStyle) - set(self.styles)
        if missing:
            raise ValueError(f"missing named styles: {sorted(item.value for item in missing)}")
        return self


class CascadeLayer(IntEnum):
    SAFETY_BASE = 0
    THEME = 10
    ARCHETYPE = 10
    TEMPLATE = 20
    PROJECT = 30
    TASK = 40
    LOCAL = 50


class StyleOverride(BaseModel):
    layer: CascadeLayer
    source: str
    style: NamedStyle
    properties: dict[str, object]
    target_id: str | None = None
    locked_properties: set[str] = Field(default_factory=set)


class StyleDiagnostic(BaseModel):
    code: str
    message: str
    source: str


class ResolvedStyle(BaseModel):
    style: NamedStyle
    properties: StyleProperties
    sources: dict[str, str]
    diagnostics: list[StyleDiagnostic] = Field(default_factory=list)


class StyleCascade:
    def resolve(
        self,
        style: NamedStyle,
        overrides: list[StyleOverride],
        *,
        target_id: str | None = None,
    ) -> ResolvedStyle:
        base = StyleSheet().styles[style].model_dump()
        sources = {key: "safety-base" for key in base}
        diagnostics: list[StyleDiagnostic] = []
        locked: dict[str, str] = {}
        allowed = set(StyleProperties.model_fields)
        relevant = sorted(
            (
                item
                for item in overrides
                if item.style is style and (item.target_id is None or item.target_id == target_id)
            ),
            key=lambda item: (item.layer, item.source),
        )
        for item in relevant:
            for key, value in item.properties.items():
                if key not in allowed:
                    diagnostics.append(
                        StyleDiagnostic(
                            code="UNSUPPORTED_STYLE_PROPERTY",
                            message=f"{key} is not supported",
                            source=item.source,
                        )
                    )
                    continue
                if key in locked and item.layer < CascadeLayer.TASK:
                    diagnostics.append(
                        StyleDiagnostic(
                            code="STYLE_PROPERTY_LOCKED",
                            message=f"{key} is locked by {locked[key]}",
                            source=item.source,
                        )
                    )
                    continue
                if key in locked and item.layer >= CascadeLayer.TASK:
                    diagnostics.append(
                        StyleDiagnostic(
                            code="TEMPLATE_LOCK_OVERRIDDEN_BY_USER",
                            message=f"explicit request overrides {key} lock from {locked[key]}",
                            source=item.source,
                        )
                    )
                base[key] = value
                sources[key] = item.source
                if key in item.locked_properties:
                    locked[key] = item.source
        return ResolvedStyle(
            style=style,
            properties=StyleProperties.model_validate(base),
            sources=sources,
            diagnostics=diagnostics,
        )


class PageOverride(BaseModel):
    layer: CascadeLayer
    source: str
    properties: dict[str, object]


class ResolvedPageSpec(BaseModel):
    page: PageSpec
    sources: dict[str, str]
    diagnostics: list[StyleDiagnostic] = Field(default_factory=list)


class PageCascade:
    def resolve(self, overrides: list[PageOverride]) -> ResolvedPageSpec:
        values = PageSpec().model_dump()
        sources = {key: "safety-base:a4" for key in values}
        diagnostics: list[StyleDiagnostic] = []
        allowed = set(PageSpec.model_fields)
        for item in sorted(overrides, key=lambda value: (value.layer, value.source)):
            for key, value in item.properties.items():
                if key not in allowed:
                    diagnostics.append(
                        StyleDiagnostic(
                            code="UNSUPPORTED_PAGE_PROPERTY",
                            message=f"{key} is not supported",
                            source=item.source,
                        )
                    )
                    continue
                values[key] = value
                sources[key] = item.source
        return ResolvedPageSpec(
            page=PageSpec.model_validate(values),
            sources=sources,
            diagnostics=diagnostics,
        )


class HeaderFooterSpec(BaseModel):
    header_text: str = ""
    footer_text: str = ""
    hide_on_cover: bool = True
    different_odd_even: bool = False


class NumberingPolicy(BaseModel):
    heading: str = "decimal"
    figures: str = "chapter-sequence"
    tables: str = "chapter-sequence"
    equations: str = "chapter-sequence"
    appendices: str = "alphabetic"
    front_matter_pages: str = "roman-lower"
    body_page_start: int = Field(default=1, ge=1)


class ThemeFontRoles(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    latin: tuple[str, ...]
    east_asia: tuple[str, ...]
    math: tuple[str, ...]
    code: tuple[str, ...]
    caption: tuple[str, ...]


class ThemeComponentTokens(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    table_header_shading: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    table_border_color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    code_background: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    accent_color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    figure_max_width_ratio: float = Field(ge=0.3, le=1)
    paragraph_gap_pt: float = Field(ge=0, le=36)


class ThemeVisualRules(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    minimum_heading_scale: float = Field(ge=1, le=3)
    minimum_contrast_ratio: float = Field(ge=3, le=21)
    print_grayscale_safe: bool = True
    max_consecutive_dense_pages: int = Field(ge=1, le=20)
    target_whitespace_ratio: float = Field(ge=0.05, le=0.8)


class TypographyTheme(BaseModel):
    """Renderer-neutral professional typography tokens."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]+$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    archetypes: tuple[ArchetypeId, ...]
    languages: tuple[str, ...]
    page: PageSpec
    font_roles: ThemeFontRoles
    styles: StyleSheet
    numbering: NumberingPolicy
    component_tokens: ThemeComponentTokens
    visual_rules: ThemeVisualRules
    license: str
    source: str

    @model_validator(mode="after")
    def renderer_neutral(self) -> TypographyTheme:
        payload = json.dumps(self.model_dump(mode="json"), ensure_ascii=False)
        forbidden = ("<w:", "word/numbering.xml", "\\documentclass", "#set page")
        if any(marker in payload for marker in forbidden):
            raise ValueError("typography themes cannot contain renderer-specific code")
        return self

    @property
    def theme_hash(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ThemeDiagnostic(BaseModel):
    code: str
    message: str
    source: str


class ThemeResolution(BaseModel):
    theme: TypographyTheme | None = None
    candidates: list[str] = Field(default_factory=list)
    confirmation_required: bool = False
    source: str
    diagnostics: list[ThemeDiagnostic] = Field(default_factory=list)


def _themed_styles(
    *,
    latin: str,
    east_asia: str,
    body_size: float,
    line_spacing: float,
    accent: str,
    heading_family: str | None = None,
    heading_east_asia: str | None = None,
    heading_sizes: tuple[float, float, float] = (16, 14, 12),
    compact: bool = False,
) -> StyleSheet:
    styles = default_style_sheet()
    for name, current in styles.items():
        styles[name] = current.model_copy(
            update={
                "font_family": latin,
                "east_asia_font": east_asia,
                "font_size_pt": body_size,
                "line_spacing": line_spacing,
            }
        )
    styles[NamedStyle.BODY_TEXT] = styles[NamedStyle.BODY_TEXT].model_copy(
        update={"space_after_pt": 0 if compact else 3}
    )
    for index, name in enumerate(
        (NamedStyle.HEADING_1, NamedStyle.HEADING_2, NamedStyle.HEADING_3)
    ):
        styles[name] = styles[name].model_copy(
            update={
                "font_family": heading_family or latin,
                "east_asia_font": heading_east_asia or east_asia,
                "font_size_pt": heading_sizes[index],
                "bold": True,
                "color": accent,
                "alignment": "left",
                "space_before_pt": (10 - index * 2) if compact else (18 - index * 3),
                "space_after_pt": 4 if compact else 7,
                "first_line_indent": mm(0),
                "keep_with_next": True,
            }
        )
    for name in (NamedStyle.HEADING_4, NamedStyle.HEADING_5, NamedStyle.HEADING_6):
        styles[name] = styles[name].model_copy(
            update={
                "font_family": heading_family or latin,
                "east_asia_font": heading_east_asia or east_asia,
                "font_size_pt": body_size,
                "bold": True,
                "color": accent,
                "first_line_indent": mm(0),
                "space_before_pt": 6,
                "space_after_pt": 3,
                "keep_with_next": True,
            }
        )
    styles[NamedStyle.DOCUMENT_TITLE] = styles[NamedStyle.DOCUMENT_TITLE].model_copy(
        update={
            "font_family": heading_family or latin,
            "east_asia_font": heading_east_asia or east_asia,
            "font_size_pt": max(20, heading_sizes[0] + 5),
            "bold": True,
            "color": accent,
            "alignment": "center",
            "first_line_indent": mm(0),
        }
    )
    styles[NamedStyle.CAPTION] = styles[NamedStyle.CAPTION].model_copy(
        update={
            "font_size_pt": max(8, body_size - 1.5),
            "alignment": "center",
            "first_line_indent": mm(0),
            "color": "#333333",
        }
    )
    styles[NamedStyle.CODE] = styles[NamedStyle.CODE].model_copy(
        update={
            "font_family": "Cascadia Mono",
            "east_asia_font": "Microsoft YaHei",
            "font_size_pt": max(8, body_size - 1.5),
            "alignment": "left",
            "first_line_indent": mm(0),
            "shading": "#F3F4F6",
        }
    )
    styles[NamedStyle.QUOTE] = styles[NamedStyle.QUOTE].model_copy(
        update={
            "italic": True,
            "left_indent": mm(8),
            "right_indent": mm(4),
            "first_line_indent": mm(0),
            "color": "#374151",
        }
    )
    return StyleSheet(styles=styles)


def _theme(
    theme_id: str,
    *,
    archetypes: tuple[ArchetypeId, ...],
    languages: tuple[str, ...],
    latin: str,
    east_asia: str,
    body_size: float,
    line_spacing: float,
    accent: str,
    margins: tuple[float, float, float, float],
    heading_family: str | None = None,
    heading_east_asia: str | None = None,
    heading_sizes: tuple[float, float, float] = (16, 14, 12),
    compact: bool = False,
    heading_numbering: str = "decimal",
) -> TypographyTheme:
    top, bottom, left, right = margins
    return TypographyTheme(
        id=theme_id,
        version="1.0.0",
        archetypes=archetypes,
        languages=languages,
        page=PageSpec(
            top_margin=mm(top),
            bottom_margin=mm(bottom),
            left_margin=mm(left),
            right_margin=mm(right),
        ),
        font_roles=ThemeFontRoles(
            latin=(latin, "Arial", "sans-serif"),
            east_asia=(east_asia, "Microsoft YaHei", "Noto Sans CJK SC"),
            math=("Cambria Math", "STIX Two Math"),
            code=("Cascadia Mono", "Consolas"),
            caption=(latin, east_asia),
        ),
        styles=_themed_styles(
            latin=latin,
            east_asia=east_asia,
            body_size=body_size,
            line_spacing=line_spacing,
            accent=accent,
            heading_family=heading_family,
            heading_east_asia=heading_east_asia,
            heading_sizes=heading_sizes,
            compact=compact,
        ),
        numbering=NumberingPolicy(heading=heading_numbering),
        component_tokens=ThemeComponentTokens(
            table_header_shading="#E8EEF7" if accent != "#000000" else "#E5E5E5",
            table_border_color="#6B7280",
            code_background="#F3F4F6",
            accent_color=accent,
            figure_max_width_ratio=0.88 if compact else 0.92,
            paragraph_gap_pt=0 if compact else 3,
        ),
        visual_rules=ThemeVisualRules(
            minimum_heading_scale=1.2,
            minimum_contrast_ratio=4.5,
            max_consecutive_dense_pages=3 if compact else 5,
            target_whitespace_ratio=0.18 if compact else 0.25,
        ),
        license="PaperAgent project license",
        source="paperagent-builtin",
    )


BUILTIN_TYPOGRAPHY_THEMES: dict[str, TypographyTheme] = {
    item.id: item
    for item in (
        _theme(
            "academic-classic-zh",
            archetypes=(ArchetypeId.ACADEMIC_PAPER, ArchetypeId.RESEARCH_REPORT),
            languages=("zh", "mixed"),
            latin="Times New Roman",
            east_asia="SimSun",
            heading_east_asia="SimHei",
            body_size=10.5,
            line_spacing=1.5,
            accent="#000000",
            margins=(25.4, 25.4, 31.8, 31.8),
            heading_sizes=(16, 14, 12),
        ),
        _theme(
            "academic-classic-en",
            archetypes=(ArchetypeId.ACADEMIC_PAPER, ArchetypeId.RESEARCH_REPORT),
            languages=("en",),
            latin="Times New Roman",
            east_asia="Noto Serif CJK SC",
            body_size=11,
            line_spacing=1.5,
            accent="#000000",
            margins=(25.4, 25.4, 30, 30),
            heading_sizes=(16, 13, 11),
        ),
        _theme(
            "experiment-lab-zh",
            archetypes=(ArchetypeId.EXPERIMENT_REPORT, ArchetypeId.PRACTICE_REPORT),
            languages=("zh", "mixed"),
            latin="Arial",
            east_asia="Microsoft YaHei",
            heading_east_asia="Microsoft YaHei",
            body_size=11,
            line_spacing=1.45,
            accent="#1F4E79",
            margins=(23, 23, 27, 25),
            heading_sizes=(17, 14, 12),
        ),
        _theme(
            "technical-clean",
            archetypes=(ArchetypeId.TECHNICAL_DOCUMENT,),
            languages=("zh", "en", "mixed"),
            latin="Calibri",
            east_asia="Microsoft YaHei",
            body_size=10.5,
            line_spacing=1.4,
            accent="#0B5CAD",
            margins=(20, 22, 24, 24),
            heading_sizes=(18, 15, 12.5),
        ),
        _theme(
            "business-modern",
            archetypes=(ArchetypeId.BUSINESS_REPORT, ArchetypeId.PRODUCT_PLAN),
            languages=("zh", "en", "mixed"),
            latin="Aptos",
            east_asia="Microsoft YaHei",
            body_size=10.5,
            line_spacing=1.35,
            accent="#17365D",
            margins=(20, 20, 24, 24),
            heading_sizes=(20, 15, 12),
        ),
        _theme(
            "formal-chinese",
            archetypes=(ArchetypeId.FORMAL_DOCUMENT,),
            languages=("zh", "mixed"),
            latin="Times New Roman",
            east_asia="FangSong",
            heading_east_asia="SimHei",
            body_size=16,
            line_spacing=1.5,
            accent="#000000",
            margins=(37, 35, 28, 26),
            heading_sizes=(22, 16, 16),
            heading_numbering="chinese-levels",
        ),
        _theme(
            "tutorial-readable",
            archetypes=(ArchetypeId.TUTORIAL,),
            languages=("zh", "en", "mixed"),
            latin="Calibri",
            east_asia="Microsoft YaHei",
            body_size=11,
            line_spacing=1.65,
            accent="#285E61",
            margins=(22, 24, 26, 24),
            heading_sizes=(19, 15, 12.5),
        ),
        _theme(
            "meeting-compact",
            archetypes=(ArchetypeId.MEETING_MINUTES,),
            languages=("zh", "en", "mixed"),
            latin="Arial",
            east_asia="Microsoft YaHei",
            body_size=10,
            line_spacing=1.2,
            accent="#2F3B52",
            margins=(18, 18, 20, 20),
            heading_sizes=(14, 12, 10.5),
            compact=True,
            heading_numbering="none",
        ),
    )
}


class ThemeResolver:
    def __init__(self, catalog: dict[str, TypographyTheme] | None = None) -> None:
        self.catalog = catalog or BUILTIN_TYPOGRAPHY_THEMES

    def resolve(
        self,
        archetype: ArchetypeId,
        *,
        language: str = "zh",
        explicit_theme: str | None = None,
        project_theme: str | None = None,
        confidence: float = 1,
    ) -> ThemeResolution:
        requested = explicit_theme or project_theme
        if requested:
            theme = self.catalog.get(requested)
            if theme is not None:
                return ThemeResolution(
                    theme=theme,
                    candidates=[theme.id],
                    source="user" if explicit_theme else "project",
                )
            return ThemeResolution(
                candidates=self._candidates(archetype, language)[:3],
                confirmation_required=True,
                source="invalid-request",
                diagnostics=[
                    ThemeDiagnostic(
                        code="THEME_NOT_FOUND",
                        message=f"theme {requested} is not installed",
                        source="resolver",
                    )
                ],
            )
        candidates = self._candidates(archetype, language)
        if not candidates:
            candidates = ["academic-classic-zh"]
        if confidence < 0.72:
            compatible = [
                theme.id
                for theme in self.catalog.values()
                if language in theme.languages and theme.id not in candidates
            ]
            candidates = [*candidates, *sorted(compatible)]
            return ThemeResolution(
                candidates=candidates[:3],
                confirmation_required=True,
                source="low-confidence",
            )
        theme = self.catalog[candidates[0]]
        return ThemeResolution(theme=theme, candidates=candidates[:3], source="deterministic")

    def _candidates(self, archetype: ArchetypeId, language: str) -> list[str]:
        exact = [
            theme.id
            for theme in self.catalog.values()
            if archetype in theme.archetypes and language in theme.languages
        ]
        fallback = [
            theme.id for theme in self.catalog.values() if archetype in theme.archetypes
        ]
        return sorted(exact or fallback)


class TocPolicy(BaseModel):
    mode: str = Field(default="auto", pattern=r"^(auto|always|never)$")
    max_depth: int = Field(default=3, ge=1, le=6)
    page_threshold: int = Field(default=5, ge=1)
    section_threshold: int = Field(default=4, ge=1)

    def enabled(self, *, estimated_pages: int, section_count: int) -> bool:
        if self.mode == "always":
            return True
        if self.mode == "never":
            return False
        return estimated_pages > self.page_threshold or section_count >= self.section_threshold


class PaginationPolicy(BaseModel):
    keep_heading_with_next: bool = True
    keep_figure_with_caption: bool = True
    keep_table_title_with_table: bool = True
    repeat_table_headers: bool = True
    allow_table_row_split: bool = False
    use_landscape_for_wide_tables: bool = True
    widow_orphan_control: bool = True


class LayoutProfile(BaseModel):
    archetype: DocumentArchetype
    theme_id: str = "academic-classic-zh"
    theme_version: str = "1.0.0"
    theme_hash: str = ""
    page: PageSpec = Field(default_factory=PageSpec)
    section_pages: list[SectionPageSpec] = Field(default_factory=list)
    styles: StyleSheet = Field(default_factory=StyleSheet)
    header_footer: HeaderFooterSpec = Field(default_factory=HeaderFooterSpec)
    numbering: NumberingPolicy = Field(default_factory=NumberingPolicy)
    toc: TocPolicy = Field(default_factory=TocPolicy)
    pagination: PaginationPolicy = Field(default_factory=PaginationPolicy)
    diagnostics: list[ThemeDiagnostic] = Field(default_factory=list)


def archetype_layout_profile(
    archetype_id: ArchetypeId,
    *,
    language: str = "zh",
    explicit_theme: str | None = None,
    project_theme: str | None = None,
) -> LayoutProfile:
    resolution = ThemeResolver().resolve(
        archetype_id,
        language=language,
        explicit_theme=explicit_theme,
        project_theme=project_theme,
    )
    theme = resolution.theme or BUILTIN_TYPOGRAPHY_THEMES[resolution.candidates[0]]
    profile = LayoutProfile(
        archetype=ARCHETYPES[archetype_id],
        theme_id=theme.id,
        theme_version=theme.version,
        theme_hash=theme.theme_hash,
        page=theme.page.model_copy(deep=True),
        styles=theme.styles.model_copy(deep=True),
        numbering=theme.numbering.model_copy(deep=True),
        diagnostics=resolution.diagnostics,
    )
    if archetype_id in {ArchetypeId.MEETING_MINUTES, ArchetypeId.FORMAL_DOCUMENT}:
        profile.toc = TocPolicy(mode="never")
    elif archetype_id in {
        ArchetypeId.ACADEMIC_PAPER,
        ArchetypeId.EXPERIMENT_REPORT,
        ArchetypeId.RESEARCH_REPORT,
    }:
        profile.toc = TocPolicy(mode="auto", max_depth=3)
    return profile


class FontRole(StrEnum):
    LATIN = "latin"
    EAST_ASIA = "east_asia"
    MATH = "math"
    CODE = "code"
    CAPTION = "caption"


class FontRequest(BaseModel):
    role: FontRole
    requested: str


class FontPlan(BaseModel):
    resolutions: list[FontResolution]
    actual_families: dict[FontRole, str | None] = Field(default_factory=dict)
    diagnostics: list[StyleDiagnostic] = Field(default_factory=list)
    preview_required: bool
    installation_approval_required: bool
    accepted: bool = False


class FontPlanService:
    def __init__(self, resolver: FontResolver | None = None) -> None:
        self.resolver = resolver or FontResolver()

    def plan(self, requests: list[FontRequest], *, allow_fallback: bool = True) -> FontPlan:
        resolutions = [
            self.resolver.resolve(item.requested, allow_fallback=allow_fallback)
            for item in requests
        ]
        requires_action = any(item.requires_user_action for item in resolutions)
        used_fallback = any(item.fallback_used for item in resolutions)
        actual_families = {
            request.role: resolution.resolved
            for request, resolution in zip(requests, resolutions, strict=True)
        }
        diagnostics = [
            StyleDiagnostic(
                code="FONT_FALLBACK_USED" if item.fallback_used else "FONT_MISSING",
                message=item.message,
                source=item.requested,
            )
            for item in resolutions
            if item.fallback_used or item.requires_user_action
        ]
        return FontPlan(
            resolutions=resolutions,
            actual_families=actual_families,
            diagnostics=diagnostics,
            preview_required=requires_action or used_fallback,
            installation_approval_required=requires_action,
            accepted=not requires_action and not used_fallback,
        )

    def plan_theme(
        self, theme: TypographyTheme, *, allow_fallback: bool = True
    ) -> FontPlan:
        roles = theme.font_roles
        return self.plan(
            [
                FontRequest(role=FontRole.LATIN, requested=roles.latin[0]),
                FontRequest(role=FontRole.EAST_ASIA, requested=roles.east_asia[0]),
                FontRequest(role=FontRole.MATH, requested=roles.math[0]),
                FontRequest(role=FontRole.CODE, requested=roles.code[0]),
                FontRequest(role=FontRole.CAPTION, requested=roles.caption[0]),
            ],
            allow_fallback=allow_fallback,
        )


def semantic_style_id(template_name: str) -> NamedStyle | None:
    normalized = re.sub(r"[^a-z0-9一-龥]", "", template_name.casefold())
    mapping = {
        "title": NamedStyle.DOCUMENT_TITLE,
        "标题": NamedStyle.DOCUMENT_TITLE,
        "subtitle": NamedStyle.SUBTITLE,
        "副标题": NamedStyle.SUBTITLE,
        "normal": NamedStyle.BODY_TEXT,
        "正文": NamedStyle.BODY_TEXT,
        "caption": NamedStyle.CAPTION,
        "题注": NamedStyle.CAPTION,
        "header": NamedStyle.HEADER,
        "footer": NamedStyle.FOOTER,
    }
    if normalized.startswith("heading") and normalized[-1:].isdigit():
        level = min(6, max(1, int(normalized[-1])))
        return NamedStyle(f"Heading{level}")
    return mapping.get(normalized)
