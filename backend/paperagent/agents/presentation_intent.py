# ruff: noqa: RUF001 - Chinese full-width punctuation is part of the input grammar.

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from paperagent.presentation import extract_explicit_presentation
from paperagent.schemas.presentation import (
    CoverLayoutSpec,
    PageChromeToken,
    PageChromeTokenKind,
    PresentationPatchKind,
    PresentationPatchOperation,
    normalize_cover_key,
)


def _clean_revision_value(value: str) -> str:
    """Remove natural-language quoting without changing the supplied value."""

    return value.strip().strip("“”\"'").strip()


class PresentationImpactDomain(StrEnum):
    COVER_DATA = "presentation_cover_data"
    COVER_LAYOUT = "presentation_cover_layout"
    HEADER_FOOTER = "presentation_header_footer"
    CONTENT = "content"


class PresentationChangeIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operations: list[PresentationPatchOperation] = Field(default_factory=list)
    affected_domains: list[PresentationImpactDomain] = Field(default_factory=list)
    requested_formats: list[str] = Field(default_factory=list)
    changes_content: bool = False
    clarification: str | None = None

    @model_validator(mode="after")
    def consistent_domains(self) -> PresentationChangeIntent:
        if self.changes_content and PresentationImpactDomain.CONTENT not in self.affected_domains:
            self.affected_domains.append(PresentationImpactDomain.CONTENT)
        if not self.operations and not self.changes_content and not self.clarification:
            raise ValueError("presentation change intent requires operations or clarification")
        self.affected_domains = list(dict.fromkeys(self.affected_domains))
        self.requested_formats = list(dict.fromkeys(self.requested_formats))
        return self


class PresentationChangeIntentClassifier:
    """Deterministic revision fallback; the model may supply the same strict schema."""

    _HEADER = re.compile(
        r"(?:页眉|header)\s*(?:改成|改为|设置为|设置成|[:：])\s*"
        r"(?P<value>.+?)(?=(?:[,，;；。]|页脚|footer|输出|导出|$))",
        re.I,
    )
    _FOOTER = re.compile(
        r"(?:页脚|footer)\s*(?:改成|改为|设置为|设置成|[:：])\s*"
        r"(?P<value>.+?)(?=(?:[,，;；。]|页眉|header|输出|导出|$))",
        re.I,
    )
    _FIELD_CHANGE = re.compile(
        r"(?:把\s*)?(?P<label>姓名|作者|学号|班级|学校|院系|专业|课程|"
        r"指导老师|指导教师|辅导老师|导师|name|author|student\s*id|class|"
        r"school|institution|department|major|course|advisor)\s*"
        r"(?:改成|改为|设置为|设置成)\s*(?P<value>[^,，;；。]+)",
        re.I,
    )
    _FIELD_REMOVE = re.compile(
        r"(?:删除|移除|去掉|不要)\s*(?P<label>姓名|作者|学号|班级|学校|院系|"
        r"专业|课程|指导老师|指导教师|辅导老师|导师)",
        re.I,
    )

    def classify(self, request: str) -> PresentationChangeIntent:
        text = request.strip()
        operations: list[PresentationPatchOperation] = []
        domains: list[PresentationImpactDomain] = []

        explicit = extract_explicit_presentation(text)
        if explicit.cover is not None:
            for field in explicit.cover.fields:
                if not field.value:
                    continue
                operations.append(
                    PresentationPatchOperation(
                        kind=PresentationPatchKind.UPSERT_COVER_FIELD,
                        semantic_key=field.semantic_key,
                        label=field.label,
                        value=field.value,
                        order=field.order,
                    )
                )
                domains.append(PresentationImpactDomain.COVER_DATA)

        for match in self._FIELD_CHANGE.finditer(text):
            label = re.sub(r"\s+", " ", match.group("label").strip())
            operation = PresentationPatchOperation(
                kind=PresentationPatchKind.UPSERT_COVER_FIELD,
                semantic_key=normalize_cover_key(label),
                label=label,
                value=_clean_revision_value(match.group("value")),
            )
            operations = [
                item
                for item in operations
                if not (
                    item.kind is PresentationPatchKind.UPSERT_COVER_FIELD
                    and item.semantic_key == operation.semantic_key
                )
            ]
            operations.append(operation)
            domains.append(PresentationImpactDomain.COVER_DATA)

        for match in self._FIELD_REMOVE.finditer(text):
            operations.append(
                PresentationPatchOperation(
                    kind=PresentationPatchKind.REMOVE_COVER_FIELD,
                    semantic_key=normalize_cover_key(match.group("label")),
                )
            )
            domains.append(PresentationImpactDomain.COVER_DATA)

        for pattern, kind in (
            (self._HEADER, PresentationPatchKind.SET_HEADER_REGION),
            (self._FOOTER, PresentationPatchKind.SET_FOOTER_REGION),
        ):
            chrome_match = pattern.search(text)
            if chrome_match:
                operations.append(
                    PresentationPatchOperation(
                        kind=kind,
                        region="center",
                        tokens=[
                            PageChromeToken(
                                kind=PageChromeTokenKind.TEXT,
                                value=_clean_revision_value(chrome_match.group("value")),
                            )
                        ],
                    )
                )
                domains.append(PresentationImpactDomain.HEADER_FOOTER)

        if re.search(r"(?:删除|移除|不要)(?:当前)?封面", text, re.I):
            operations.append(
                PresentationPatchOperation(
                    kind=PresentationPatchKind.SET_COVER_ENABLED,
                    bool_value=False,
                )
            )
            domains.append(PresentationImpactDomain.COVER_LAYOUT)
        if re.search(r"封面.{0,8}(?:居中|左对齐|右对齐)", text, re.I):
            alignment = "left" if "左对齐" in text else "right" if "右对齐" in text else "center"
            operations.append(
                PresentationPatchOperation(
                    kind=PresentationPatchKind.SET_COVER_LAYOUT,
                    layout=CoverLayoutSpec(alignment=alignment),
                )
            )
            domains.append(PresentationImpactDomain.COVER_LAYOUT)

        content_scope = re.sub(
            r"(?:不要|无需|禁止|不需要)\s*(?:重写|修改|改写|补充|新增|删除)(?:正文|内容)?",
            "",
            text,
            flags=re.I,
        )
        changes_content = bool(
            re.search(
                r"正文(?:内容)?\s*(?:新增|增加|删除|移除|补充|修改|改写|重写)|"
                r"内容(?:修改|改写|补充|重写)|重写(?:正文|内容)|"
                r"新增.{0,6}(?:章节|段落)",
                content_scope,
                re.I,
            )
        )
        formats = []
        if re.search(r"\bpdf\b", text, re.I):
            formats.append("pdf")
        if re.search(r"\bdocx\b|\bword\b", text, re.I):
            formats.append("docx")
        if re.search(r"\bmarkdown\b|\.md\b", text, re.I):
            formats.append("md")

        clarification = None
        if not operations and not changes_content:
            clarification = "未识别到可验证的封面、页眉或页脚修改值，请说明目标字段和新值。"
        return PresentationChangeIntent(
            operations=operations,
            affected_domains=domains,
            requested_formats=formats,
            changes_content=changes_content,
            clarification=clarification,
        )
