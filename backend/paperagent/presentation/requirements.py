from __future__ import annotations

import re
from collections.abc import Iterable

from pydantic import BaseModel, Field

from paperagent.schemas.presentation import (
    STANDARD_COVER_KEYS,
    PresentationAmbiguity,
    PresentationSource,
    RequirementCoverField,
    RequirementCoverSpec,
    RequirementPageChromeSpec,
    RequirementPresentationSpec,
    normalize_cover_key,
)

_KNOWN_LABELS = sorted(STANDARD_COVER_KEYS, key=len, reverse=True)
_LABEL_PATTERN = "|".join(re.escape(item) for item in _KNOWN_LABELS)
_PAIR = re.compile(
    rf"(?P<label>{_LABEL_PATTERN})\s*[:\uFF1A]\s*"
    rf"(?P<value>[^\uFF0C,\uFF1B;\u3002\n]+)",
    re.IGNORECASE,
)
_QUOTED_PAIR = re.compile(
    rf"(?P<label>{_LABEL_PATTERN})\s*[:\uFF1A]?\s*[\u201C\"]"
    rf"(?P<value>[^\u201D\"\n]+)[\u201D\"]",
    re.IGNORECASE,
)
_CUSTOM_PAIR = re.compile(
    r"(?P<label>[\w\u4e00-\u9fff][^:\uFF1A\uFF0C,\uFF1B;\n]{0,30})"
    r"\s*[:\uFF1A]\s*(?P<value>[^\uFF0C,\uFF1B;\u3002\n]+)"
)


class PresentationResolution(BaseModel):
    presentation: RequirementPresentationSpec
    source_map: dict[str, PresentationSource] = Field(default_factory=dict)
    diagnostics: list[str] = Field(default_factory=list)


def _clean_value(value: str) -> str:
    return value.strip().strip("。.\u201C\u201D\"")


def _clean_custom_label(value: str) -> str:
    label = value.strip()
    label = re.sub(
        r"^(?:请)?(?:在)?(?:封面|首页|扉页)(?:中|上)?(?:写|加入|添加|放上|显示|填写)?",
        "",
        label,
    ).strip()
    return label


def extract_explicit_presentation(text: str) -> RequirementPresentationSpec:
    fields: list[RequirementCoverField] = []
    seen_spans: set[tuple[int, int]] = set()
    field_matches = sorted(
        [*_PAIR.finditer(text), *_QUOTED_PAIR.finditer(text)],
        key=lambda item: (item.start(), item.end()),
    )
    seen_keys: set[str] = set()
    for order, match in enumerate(field_matches, start=1):
        value = _clean_value(match.group("value"))
        if not value:
            continue
        label = match.group("label").strip()
        key = normalize_cover_key(label)
        if key in seen_keys:
            continue
        fields.append(
            RequirementCoverField(
                semantic_key=key,
                label=label,
                value=value,
                order=order * 10,
                source=PresentationSource.USER,
                source_ref="$raw",
            )
        )
        seen_keys.add(key)
        seen_spans.add(match.span())

    cover_context = bool(re.search(r"封面|首页|扉页|个人信息", text, re.IGNORECASE))
    if cover_context:
        for match in _CUSTOM_PAIR.finditer(text):
            if any(start <= match.start() and match.end() <= end for start, end in seen_spans):
                continue
            label = _clean_custom_label(match.group("label"))
            value = _clean_value(match.group("value"))
            if not label or not value or len(label) > 20:
                continue
            if label in {"信息", "信息为", "个人信息", "封面信息", "封面信息为"}:
                continue
            if len(_QUOTED_PAIR.findall(value)) >= 2:
                continue
            if label.casefold() in {"页眉", "页脚", "header", "footer"}:
                continue
            key = normalize_cover_key(label)
            if any(item.semantic_key == key and item.value == value for item in fields):
                continue
            fields.append(
                RequirementCoverField(
                    semantic_key=key,
                    label=label,
                    value=value,
                    order=(len(fields) + 1) * 10,
                    source=PresentationSource.USER,
                    source_ref="$raw",
                )
            )

    title: str | None = None
    title_match = re.search(
        r"(?:封面|首页|扉页)(?:标题|题目)\s*(?:为|是|[:\uFF1A])\s*"
        r"[\u201C\"']?([^\uFF0C,\uFF1B;\u3002\n\u201D\"']+)",
        text,
        re.IGNORECASE,
    )
    if title_match:
        title = _clean_value(title_match.group(1))

    header: str | None = None
    header_match = re.search(
        r"(?:正文)?页眉(?:居中|左侧|右侧)?(?:内容)?\s*"
        r"(?:为|是|改成|改为|设置为|设置成|写为|[:\uFF1A])\s*"
        r"[\u201C\"']?([^\uFF0C,\uFF1B;\u3002\n\u201D\"']+)",
        text,
        re.IGNORECASE,
    )
    if header_match:
        header = _clean_value(header_match.group(1))
    hide_on_cover: bool | None = None
    if re.search(
        r"(?:封面|首页)(?:不|无需|不要)显示(?:页眉页脚|页眉和页脚|页眉(?:及|与)页脚|页眉)",
        text,
    ):
        hide_on_cover = True
    elif re.search(r"封面(?:也|需要)显示页眉|首页(?:也|需要)显示页眉", text):
        hide_on_cover = False
    page_number = bool(re.search(r"页码|第\s*[Xx{]\w*[}Xx]?\s*页", text)) or None
    total_pages = bool(re.search(r"共\s*[Yy{]\w*[}Yy]?\s*页|总页数", text)) or None
    footer: str | None = None
    footer_match = re.search(
        r"(?:正文)?页脚(?:居中|左侧|右侧)?(?:内容)?\s*"
        r"(?:为|是|改成|改为|设置为|设置成|写为|[:\uFF1A])\s*"
        r"[\u201C\"']?([^\uFF0C,\uFF1B;\u3002\n\u201D\"']+)",
        text,
        re.IGNORECASE,
    )
    if footer_match:
        candidate = _clean_value(footer_match.group(1))
        if not re.search(r"\{\s*pages?\s*\}|页码|总页数", candidate, re.I):
            footer = candidate
    page_chrome = None
    if (
        header
        or footer
        or hide_on_cover is not None
        or page_number is not None
        or total_pages is not None
    ):
        page_chrome = RequirementPageChromeSpec(
            header_center=header,
            footer_center=footer,
            hide_on_cover=hide_on_cover,
            page_number=page_number,
            total_pages=total_pages,
        )
    cover = (
        RequirementCoverSpec(enabled=True, title=title, fields=fields)
        if fields or cover_context or title
        else None
    )
    unresolved: list[PresentationAmbiguity] = []
    if cover_context and re.search(r"我的信息|个人信息", text) and not fields:
        unresolved.append(
            PresentationAmbiguity(
                code="COVER_FIELDS_MISSING",
                field_path="presentation.cover.fields",
                message="用户要求加入个人信息, 但当前请求没有提供具体字段和值。",
            )
        )
    return RequirementPresentationSpec(
        cover=cover,
        page_chrome=page_chrome,
        unresolved=unresolved,
    )


class PresentationResolver:
    """Deterministically cascade presentation layers without rewriting user values."""

    def resolve(
        self,
        *,
        defaults: RequirementPresentationSpec | None = None,
        archetype: RequirementPresentationSpec | None = None,
        template: RequirementPresentationSpec | None = None,
        current: RequirementPresentationSpec | None = None,
        latest: RequirementPresentationSpec | None = None,
    ) -> PresentationResolution:
        layers = (
            (PresentationSource.DEFAULT, defaults),
            (PresentationSource.ARCHETYPE, archetype),
            (PresentationSource.TEMPLATE, template),
            (PresentationSource.USER, current),
            (PresentationSource.USER, latest),
        )
        enabled: bool | None = None
        title: str | None = None
        subtitle: str | None = None
        layout_hint: str | None = None
        fields: dict[str, RequirementCoverField] = {}
        order: list[str] = []
        chrome: dict[str, object] = {}
        source_map: dict[str, PresentationSource] = {}
        unresolved: list[PresentationAmbiguity] = []
        diagnostics: list[str] = []

        for layer_source, layer in layers:
            if layer is None:
                continue
            unresolved.extend(layer.unresolved)
            if layer.cover is not None:
                for name in ("enabled", "title", "subtitle", "layout_hint"):
                    value = getattr(layer.cover, name)
                    if value is not None:
                        if name == "enabled":
                            enabled = bool(value)
                        elif name == "title":
                            title = str(value)
                        elif name == "subtitle":
                            subtitle = str(value)
                        else:
                            layout_hint = str(value)
                        source_map[f"cover.{name}"] = layer_source
                for item in layer.cover.fields:
                    key = item.semantic_key
                    if item.slot and not item.value:
                        source_map.setdefault(f"cover.fields.{key}", layer_source)
                        continue
                    if key in fields and fields[key].value != item.value and layer is latest:
                        diagnostics.append(f"latest user value overrides {key}")
                    if key not in order:
                        order.append(key)
                    fields[key] = item.model_copy(update={"source": layer_source})
                    source_map[f"cover.fields.{key}"] = layer_source
            if layer.page_chrome is not None:
                for name, value in layer.page_chrome.model_dump(exclude_none=True).items():
                    chrome[name] = value
                    source_map[f"page_chrome.{name}"] = layer_source

        ordered_fields = sorted(
            fields.values(),
            key=lambda item: (
                item.order if item.order is not None else (order.index(item.semantic_key) + 1) * 10,
                item.semantic_key,
            ),
        )
        cover = None
        if enabled is not None or title or subtitle or layout_hint or ordered_fields:
            cover = RequirementCoverSpec(
                enabled=enabled,
                title=title,
                subtitle=subtitle,
                fields=ordered_fields,
                layout_hint=layout_hint,
            )
        page_chrome = RequirementPageChromeSpec.model_validate(chrome) if chrome else None
        return PresentationResolution(
            presentation=RequirementPresentationSpec(
                cover=cover,
                page_chrome=page_chrome,
                unresolved=_dedupe_ambiguities(unresolved),
            ),
            source_map=source_map,
            diagnostics=diagnostics,
        )


def _dedupe_ambiguities(values: Iterable[PresentationAmbiguity]) -> list[PresentationAmbiguity]:
    result: dict[tuple[str, str], PresentationAmbiguity] = {}
    for item in values:
        result.setdefault((item.code, item.field_path), item)
    return list(result.values())


def enrich_requirement_presentation(
    model_value: RequirementPresentationSpec,
    raw_text: str,
) -> PresentationResolution:
    explicit = extract_explicit_presentation(raw_text)
    return PresentationResolver().resolve(current=model_value, latest=explicit)


def presentation_confirmation_summary(value: RequirementPresentationSpec) -> dict[str, object]:
    cover = value.cover
    chrome = value.page_chrome
    return {
        "cover_enabled": bool(cover and cover.enabled is not False),
        "cover_fields": [
            {"key": item.semantic_key, "label": item.label, "value": item.value}
            for item in (cover.fields if cover else [])
            if item.value
        ],
        "page_chrome": chrome.model_dump(exclude_none=True) if chrome else {},
        "unresolved": [item.model_dump() for item in value.unresolved],
    }
