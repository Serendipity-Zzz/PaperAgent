from __future__ import annotations

import re

from pydantic import BaseModel, Field


class TypographySpec(BaseModel):
    body_font: str | None = Field(default=None, min_length=1, max_length=128)
    heading_font: str | None = Field(default=None, min_length=1, max_length=128)
    table_font: str | None = Field(
        default=None, min_length=1, max_length=128, exclude_if=lambda value: value is None
    )
    code_font: str | None = Field(default=None, min_length=1, max_length=128)
    equation_font: str | None = Field(
        default=None, min_length=1, max_length=128, exclude_if=lambda value: value is None
    )
    body_size_pt: float | None = Field(default=None, ge=5, le=96)
    heading_size_pt: float | None = Field(default=None, ge=5, le=120)
    table_size_pt: float | None = Field(
        default=None, ge=5, le=96, exclude_if=lambda value: value is None
    )
    code_size_pt: float | None = Field(
        default=None, ge=5, le=96, exclude_if=lambda value: value is None
    )
    equation_size_pt: float | None = Field(
        default=None, ge=5, le=96, exclude_if=lambda value: value is None
    )
    line_spacing: float | None = Field(default=None, ge=0.8, le=4)
    first_line_indent_chars: float | None = Field(default=None, ge=0, le=10)

    @property
    def configured(self) -> bool:
        return any(value is not None for value in self.model_dump().values())


CHINESE_FONT_SIZES = {
    "初号": 42.0,
    "小初": 36.0,
    "一号": 26.0,
    "小一": 24.0,
    "二号": 22.0,
    "小二": 18.0,
    "三号": 16.0,
    "小三": 15.0,
    "四号": 14.0,
    "小四": 12.0,
    "五号": 10.5,
    "小五": 9.0,
    "六号": 7.5,
    "小六": 6.5,
    "七号": 5.5,
    "八号": 5.0,
}


def extract_typography(text: str) -> tuple[TypographySpec, set[str]]:
    """Deterministic fallback; the LLM path can populate the same schema more flexibly."""

    values: dict[str, object] = {}
    matched: set[str] = set()
    for field, value in (
        ("body_font", _font_after(text, r"(?:正文|body)\s*(?:字体|font)?")),
        ("heading_font", _font_after(text, r"(?:标题|heading)\s*(?:字体|font)?")),
        ("table_font", _font_after(text, r"(?:表格|table)\s*(?:字体|font)?")),
        ("code_font", _font_after(text, r"(?:代码|code)\s*(?:字体|font)?")),
        ("equation_font", _font_after(text, r"(?:公式|equation)\s*(?:字体|font)?")),
    ):
        if value:
            values[field] = value
            matched.add(field)
    if "body_font" not in values:
        global_font = _font_after(text, r"(?:字体|font)")
        if global_font:
            values["body_font"] = global_font
            matched.add("body_font")
    body_size = _size_after(text, r"(?:正文|body)")
    heading_size = _size_after(text, r"(?:标题|heading)")
    table_size = _size_after(text, r"(?:表格|table)")
    code_size = _size_after(text, r"(?:代码|code)")
    equation_size = _size_after(text, r"(?:公式|equation)")
    if body_size is None:
        body_size = _size_after(text, r"(?:字号|font\s*size)")
    if body_size is not None:
        values["body_size_pt"] = body_size
        matched.add("body_size_pt")
    if heading_size is not None:
        values["heading_size_pt"] = heading_size
        matched.add("heading_size_pt")
    for field, size_value in (
        ("table_size_pt", table_size),
        ("code_size_pt", code_size),
        ("equation_size_pt", equation_size),
    ):
        if size_value is not None:
            values[field] = size_value
            matched.add(field)
    line_spacing = re.search(
        r"(?:行距|line\s*spacing)\s*(?:为|是|[:\uff1a=])?\s*"
        r"(\d+(?:\.\d+)?)\s*(?:倍|x)?",
        text,
        re.I,
    )
    if line_spacing:
        values["line_spacing"] = float(line_spacing.group(1))
        matched.add("line_spacing")
    indent = re.search(
        r"(?:首行缩进|first[- ]line\s*indent)\s*(?:为|是|[:\uff1a=])?\s*"
        r"(\d+(?:\.\d+)?)\s*(?:字符|字|ch(?:ar)?s?)?",
        text,
        re.I,
    )
    if indent:
        values["first_line_indent_chars"] = float(indent.group(1))
        matched.add("first_line_indent_chars")
    return TypographySpec.model_validate(values), matched


def _font_after(text: str, prefix: str) -> str | None:
    match = re.search(
        prefix
        + r"\s*(?:使用|采用|设置为|设为|改为|改成|换为|换成|为|是|[:\uff1a=])?\s*[\"“]?"
        + r"([A-Za-z][A-Za-z0-9 _-]{1,50}|[\u3400-\u9fff]{2,12})"
        + r"[\"”]?(?=\s*(?:字体|font)?(?:[\uff0c,\u3002;\uff1b]|字号|大小|\d|$))",
        text,
        re.I,
    )
    if not match:
        return None
    value = match.group(1).strip()
    # Region names such as ``正文`` and ``标题`` also occur in ordinary layout
    # constraints (for example ``正文不要每节另起一页``).  The fallback parser
    # must not treat the following prose as a font name merely because it is a
    # short run of CJK characters.  Require an explicit font marker or an
    # assignment/change verb before accepting an open-ended family name.  The
    # model-backed requirement path can still populate arbitrary font names;
    # this guard only makes the deterministic fallback fail closed.
    binding = match.group(0)[: match.start(1) - match.start(0)]
    value = re.sub(r"(?:字体|font)$", "", value, flags=re.I).strip()
    if value in CHINESE_FONT_SIZES:
        return None
    if any(marker in value for marker in ("字号", "大小", "行距", "缩进")):
        return None
    for size_name in sorted(CHINESE_FONT_SIZES, key=len, reverse=True):
        if value.endswith(size_name) and len(value) > len(size_name):
            value = value[: -len(size_name)].strip()
            break
    strong_binding = re.search(
        r"(?:字体|font|使用|采用|设置为|设为|改为|改成|换为|换成)", binding, re.I
    )
    if not strong_binding and not _looks_like_font_family(value):
        return None
    return value


def _looks_like_font_family(value: str) -> bool:
    normalized = value.casefold().strip()
    return bool(
        re.search(
            r"(?:体|宋|黑|楷|仿宋|sans|serif|mono|roman|arial|calibri|cambria|consolas|simsun)$",
            normalized,
            re.I,
        )
    )


def _size_after(text: str, prefix: str) -> float | None:
    names = "|".join(sorted(CHINESE_FONT_SIZES, key=len, reverse=True))
    match = re.search(
        prefix
        + r".{0,30}?(?:字号|大小|font\s*size|设为|为|是|[:\uff1a=])?\s*"
        + rf"({names}|\d+(?:\.\d+)?)\s*(pt|磅|号)?",
        text,
        re.I,
    )
    if not match:
        return None
    value, unit = match.group(1), (match.group(2) or "").lower()
    if value in CHINESE_FONT_SIZES:
        return CHINESE_FONT_SIZES[value]
    if unit in {"pt", "磅"} or re.search(r"字号|font\s*size", match.group(0), re.I):
        return float(value)
    return None
