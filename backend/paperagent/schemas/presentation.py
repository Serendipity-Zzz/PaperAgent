from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PresentationSource(StrEnum):
    USER = "user"
    ATTACHMENT = "attachment"
    MEMORY = "memory"
    TEMPLATE = "template"
    ARCHETYPE = "archetype"
    DEFAULT = "default"


class RequirementCoverField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    semantic_key: str = Field(min_length=1, max_length=120, pattern=r"^[a-z][a-z0-9_.-]*$")
    label: str = Field(min_length=1, max_length=100)
    value: str = Field(default="", max_length=1_000)
    order: int | None = Field(default=None, ge=0, le=10_000)
    source: PresentationSource = PresentationSource.USER
    source_ref: str | None = Field(default=None, max_length=500)
    slot: bool = False
    fixed_text: bool = False

    @field_validator("label", "value")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def value_or_slot(self) -> RequirementCoverField:
        if not self.value and not self.slot:
            raise ValueError("cover field requires a value or an explicit template slot")
        return self


class RequirementCoverSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    title: str | None = Field(default=None, max_length=500)
    subtitle: str | None = Field(default=None, max_length=500)
    fields: list[RequirementCoverField] = Field(default_factory=list, max_length=50)
    layout_hint: str | None = Field(default=None, max_length=120)


class RequirementPageChromeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    header_left: str | None = Field(default=None, max_length=500)
    header_center: str | None = Field(default=None, max_length=500)
    header_right: str | None = Field(default=None, max_length=500)
    footer_left: str | None = Field(default=None, max_length=500)
    footer_center: str | None = Field(default=None, max_length=500)
    footer_right: str | None = Field(default=None, max_length=500)
    page_number: bool | None = None
    total_pages: bool | None = None
    hide_on_cover: bool | None = None
    different_odd_even: bool | None = None

    @field_validator(
        "header_left",
        "header_center",
        "header_right",
        "footer_left",
        "footer_center",
        "footer_right",
    )
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        stripped = value.strip() if value else None
        return stripped or None


class PresentationAmbiguity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=100, pattern=r"^[A-Z][A-Z0-9_]*$")
    field_path: str = Field(min_length=1, max_length=300)
    message: str = Field(min_length=1, max_length=1_000)
    candidates: list[str] = Field(default_factory=list, max_length=20)


class RequirementPresentationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cover: RequirementCoverSpec | None = None
    page_chrome: RequirementPageChromeSpec | None = None
    unresolved: list[PresentationAmbiguity] = Field(default_factory=list, max_length=20)

    def has_explicit_content(self) -> bool:
        return bool(
            (
                self.cover
                and (
                    self.cover.enabled is not None
                    or self.cover.title
                    or self.cover.subtitle
                    or self.cover.fields
                    or self.cover.layout_hint
                )
            )
            or self.page_chrome
        )


STANDARD_COVER_KEYS: dict[str, str] = {
    "姓名": "author",
    "作者": "author",
    "name": "author",
    "author": "author",
    "学号": "student_id",
    "student id": "student_id",
    "student_id": "student_id",
    "班级": "class_name",
    "class": "class_name",
    "学校": "institution",
    "单位": "institution",
    "院校": "institution",
    "institution": "institution",
    "school": "institution",
    "学院": "department",
    "院系": "department",
    "department": "department",
    "专业": "major",
    "major": "major",
    "课程": "course",
    "course": "course",
    "指导老师": "advisor",
    "指导教师": "advisor",
    "辅导老师": "advisor",
    "导师": "advisor",
    "advisor": "advisor",
    "任课老师": "instructor",
    "教师": "instructor",
    "instructor": "instructor",
    "项目名称": "project_name",
    "课题名称": "project_name",
    "project": "project_name",
    "日期": "date",
    "实验日期": "date",
    "date": "date",
}


def normalize_cover_key(label: str) -> str:
    normalized = re.sub(r"\s+", " ", label.strip()).casefold()
    if normalized in STANDARD_COVER_KEYS:
        return STANDARD_COVER_KEYS[normalized]
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    if not slug:
        slug = "field-" + label.encode("utf-8").hex()[:24]
    return f"custom.{slug}"


class PresentationFieldProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: PresentationSource = PresentationSource.USER
    source_ref: str | None = Field(default=None, max_length=500)


class CoverFieldStyleRole(StrEnum):
    PRIMARY = "primary"
    SECONDARY = "secondary"
    METADATA = "metadata"


class CoverField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_id: UUID = Field(default_factory=uuid4)
    semantic_key: str = Field(min_length=1, max_length=120, pattern=r"^[a-z][a-z0-9_.-]*$")
    label: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=1_000)
    order: int = Field(default=0, ge=0, le=10_000)
    visible: bool = True
    style_role: CoverFieldStyleRole = CoverFieldStyleRole.METADATA
    provenance: PresentationFieldProvenance = Field(default_factory=PresentationFieldProvenance)

    @field_validator("label", "value")
    @classmethod
    def strip_cover_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("cover label/value cannot be blank")
        return stripped


class CoverLayoutSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset: str = Field(default="academic-centered", max_length=120)
    vertical_anchor: str = Field(default="upper-third", max_length=80)
    alignment: str = Field(default="center", pattern=r"^(left|center|right)$")
    field_layout: str = Field(default="label-value-grid", max_length=80)
    title_spacing_after_pt: float = Field(default=72, ge=0, le=240)
    field_row_spacing_pt: float = Field(default=10, ge=0, le=72)
    max_content_width_mm: float = Field(default=120, ge=60, le=190)
    start_new_page_after: bool = True


class CoverSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    title: str | None = Field(default=None, max_length=500)
    subtitle: str | None = Field(default=None, max_length=500)
    fields: list[CoverField] = Field(default_factory=list, max_length=50)
    logo_artifact_id: UUID | None = None
    layout: CoverLayoutSpec = Field(default_factory=CoverLayoutSpec)

    @model_validator(mode="after")
    def unique_fields(self) -> CoverSpec:
        ids = [item.field_id for item in self.fields]
        keys = [item.semantic_key for item in self.fields]
        if len(ids) != len(set(ids)):
            raise ValueError("cover field IDs must be unique")
        if len(keys) != len(set(keys)):
            raise ValueError("cover semantic keys must be unique")
        return self


class PageChromeTokenKind(StrEnum):
    TEXT = "text"
    DOCUMENT_TITLE = "document_title"
    COVER_FIELD = "cover_field"
    CURRENT_HEADING = "current_heading"
    PAGE_NUMBER = "page_number"
    TOTAL_PAGES = "total_pages"
    DATE = "date"


class PageChromeToken(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: PageChromeTokenKind
    value: str | None = Field(default=None, max_length=500)
    field_key: str | None = Field(
        default=None,
        max_length=120,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    )

    @model_validator(mode="after")
    def valid_payload(self) -> PageChromeToken:
        if self.kind is PageChromeTokenKind.TEXT and not self.value:
            raise ValueError("text token requires value")
        if self.kind is PageChromeTokenKind.COVER_FIELD and not self.field_key:
            raise ValueError("cover_field token requires field_key")
        return self


class ThreeRegionLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    left: list[PageChromeToken] = Field(default_factory=list, max_length=20)
    center: list[PageChromeToken] = Field(default_factory=list, max_length=20)
    right: list[PageChromeToken] = Field(default_factory=list, max_length=20)


class HeaderFooterSectionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    header: ThreeRegionLine = Field(default_factory=ThreeRegionLine)
    footer: ThreeRegionLine = Field(default_factory=ThreeRegionLine)


class PageChromeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: HeaderFooterSectionSpec = Field(default_factory=HeaderFooterSectionSpec)
    first_page: HeaderFooterSectionSpec | None = None
    odd_page: HeaderFooterSectionSpec | None = None
    even_page: HeaderFooterSectionSpec | None = None
    different_first_page: bool = True
    different_odd_even: bool = False

    @model_validator(mode="after")
    def valid_page_variants(self) -> PageChromeSpec:
        if not self.different_odd_even and (
            self.odd_page is not None or self.even_page is not None
        ):
            raise ValueError("odd/even page chrome requires different_odd_even")
        return self


class DocumentPresentationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    cover: CoverSpec = Field(default_factory=CoverSpec)
    page_chrome: PageChromeSpec = Field(default_factory=PageChromeSpec)
    source_profile_id: str | None = Field(default=None, max_length=200)
    source_template_id: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def chrome_field_references_exist(self) -> DocumentPresentationSpec:
        keys = {item.semantic_key for item in self.cover.fields}
        variants = [
            self.page_chrome.default,
            self.page_chrome.first_page,
            self.page_chrome.odd_page,
            self.page_chrome.even_page,
        ]
        for variant in variants:
            if variant is None:
                continue
            for line in (variant.header, variant.footer):
                for token in [*line.left, *line.center, *line.right]:
                    if (
                        token.kind is PageChromeTokenKind.COVER_FIELD
                        and token.field_key not in keys
                    ):
                        raise ValueError(
                            f"page chrome references unknown cover field {token.field_key}"
                        )
        return self


class PresentationPatchKind(StrEnum):
    SET_COVER_ENABLED = "set_cover_enabled"
    SET_COVER_TITLE = "set_cover_title"
    SET_COVER_SUBTITLE = "set_cover_subtitle"
    UPSERT_COVER_FIELD = "upsert_cover_field"
    REMOVE_COVER_FIELD = "remove_cover_field"
    REORDER_COVER_FIELDS = "reorder_cover_fields"
    SET_COVER_LAYOUT = "set_cover_layout"
    SET_HEADER_REGION = "set_header_region"
    SET_FOOTER_REGION = "set_footer_region"
    SET_FIRST_PAGE_POLICY = "set_first_page_policy"
    SET_ODD_EVEN_POLICY = "set_odd_even_policy"


class PresentationPatchOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: PresentationPatchKind
    semantic_key: str | None = Field(default=None, max_length=120)
    label: str | None = Field(default=None, max_length=100)
    value: str | None = Field(default=None, max_length=1_000)
    order: int | None = Field(default=None, ge=0, le=10_000)
    ordered_keys: list[str] = Field(default_factory=list, max_length=50)
    region: str | None = Field(default=None, pattern=r"^(left|center|right)$")
    tokens: list[PageChromeToken] = Field(default_factory=list, max_length=20)
    bool_value: bool | None = None
    layout: CoverLayoutSpec | None = None

    @model_validator(mode="after")
    def required_operation_payload(self) -> PresentationPatchOperation:
        if self.kind is PresentationPatchKind.UPSERT_COVER_FIELD and not (
            self.semantic_key and self.label and self.value
        ):
            raise ValueError("upsert_cover_field requires semantic_key, label and value")
        if self.kind is PresentationPatchKind.REMOVE_COVER_FIELD and not self.semantic_key:
            raise ValueError("remove_cover_field requires semantic_key")
        if (
            self.kind
            in {
                PresentationPatchKind.SET_HEADER_REGION,
                PresentationPatchKind.SET_FOOTER_REGION,
            }
            and not self.region
        ):
            raise ValueError("page chrome region patch requires region")
        if (
            self.kind
            in {
                PresentationPatchKind.SET_COVER_ENABLED,
                PresentationPatchKind.SET_FIRST_PAGE_POLICY,
                PresentationPatchKind.SET_ODD_EVEN_POLICY,
            }
            and self.bool_value is None
        ):
            raise ValueError("boolean presentation patch requires bool_value")
        if self.kind is PresentationPatchKind.SET_COVER_LAYOUT and self.layout is None:
            raise ValueError("set_cover_layout requires layout")
        return self


class PresentationExpectationManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(default="1.0", pattern=r"^1\.0$")
    required_cover_keys: list[str] = Field(default_factory=list)
    expected_header_text: list[str] = Field(default_factory=list)
    expected_footer_text: list[str] = Field(default_factory=list)
    hide_on_cover: bool = True
    require_page_number: bool = False
    require_total_pages: bool = False
    allow_format_degradation: bool = False

    @field_validator("required_cover_keys")
    @classmethod
    def unique_required_cover_keys(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("required cover keys must be unique")
        return value

    @model_validator(mode="after")
    def total_pages_requires_page_number(self) -> PresentationExpectationManifest:
        if self.require_total_pages and not self.require_page_number:
            raise ValueError("total page expectation requires page number")
        return self

    @property
    def expectation_hash(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
