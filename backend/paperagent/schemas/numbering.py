from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator


class NumberingOwner(StrEnum):
    AUTHOR = "author"
    TEMPLATE = "template"
    RENDERER = "renderer"
    NONE = "none"


class NumberingFormat(StrEnum):
    DECIMAL = "decimal"
    CHINESE = "chinese"
    ROMAN = "roman"
    ALPHABETIC = "alphabetic"
    BULLET = "bullet"
    NONE = "none"


class DiagnosticSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class NumberingDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    severity: DiagnosticSeverity = DiagnosticSeverity.WARNING
    category: str | None = None
    node_id: str | None = None
    original: str | None = None
    normalized: str | None = None
    repair_node: str | None = None


class LabelPrefix(BaseModel):
    model_config = ConfigDict(extra="forbid")

    family: str
    levels: list[str] = Field(default_factory=list)
    raw: str
    span_start: int = Field(ge=0)
    span_end: int = Field(ge=0)
    confidence: float = Field(default=1.0, ge=0, le=1)


class LabelMap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str | None = None
    node_kind: str
    original: str
    semantic: str
    prefixes: list[LabelPrefix] = Field(default_factory=list)
    protected_reason: str | None = None


class NormalizedLabel(LabelMap):
    changed: bool = False


class NumberingScheme(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner: NumberingOwner = NumberingOwner.RENDERER
    format: NumberingFormat = NumberingFormat.DECIMAL
    pattern: str = "%1"
    separator: str = "."
    start: int = Field(default=1, ge=1)
    restart_at_level: int | None = Field(default=None, ge=1, le=9)
    max_depth: int = Field(default=6, ge=1, le=9)
    source: str = "safe-default"

    @model_validator(mode="after")
    def owner_and_format_are_consistent(self) -> NumberingScheme:
        if self.owner is NumberingOwner.NONE and self.format is not NumberingFormat.NONE:
            raise ValueError("numbering owner 'none' requires format 'none'")
        if self.owner is not NumberingOwner.NONE and self.format is NumberingFormat.NONE:
            raise ValueError("active numbering owner requires an active format")
        return self


class NumberingContract(BaseModel):
    """Canonical single-owner numbering policy shared by every renderer."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    headings: NumberingScheme = Field(
        default_factory=lambda: NumberingScheme(
            pattern="%1.%2.%3.%4.%5.%6",
            restart_at_level=1,
            max_depth=6,
        )
    )
    lists: NumberingScheme = Field(
        default_factory=lambda: NumberingScheme(pattern="%1", max_depth=9)
    )
    figures: NumberingScheme = Field(
        default_factory=lambda: NumberingScheme(pattern="图 %1", max_depth=1)
    )
    tables: NumberingScheme = Field(
        default_factory=lambda: NumberingScheme(pattern="表 %1", max_depth=1)
    )
    equations: NumberingScheme = Field(
        default_factory=lambda: NumberingScheme(pattern="\uff08%1\uff09", max_depth=1)
    )
    appendices: NumberingScheme = Field(
        default_factory=lambda: NumberingScheme(
            format=NumberingFormat.ALPHABETIC,
            pattern="附录 %1",
            max_depth=1,
        )
    )
    page_numbers: NumberingScheme = Field(
        default_factory=lambda: NumberingScheme(pattern="%1", max_depth=1)
    )
    label_map: list[LabelMap] = Field(default_factory=list)
    normalized_label_hash: str = ""
    diagnostics: list[NumberingDiagnostic] = Field(default_factory=list)
    decision_source: str = "safe-default"

    @property
    def contract_hash(self) -> str:
        payload = self.model_dump(mode="json", exclude={"diagnostics"})
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def default_numbering_contract() -> NumberingContract:
    return NumberingContract()


class NumberingNormalizer:
    """Conservative fixed-point parser for structural labels.

    The parser intentionally operates only on known structural node kinds. It records
    every consumed prefix so normalization remains inspectable and reversible.
    """

    STRUCTURAL_KINDS: ClassVar[frozenset[str]] = frozenset(
        {"heading", "list_label", "caption_label", "appendix_label"}
    )
    MAX_PREFIXES: ClassVar[int] = 8
    _SPACE = r"[\s\u3000]*"
    _PROTECTIONS: ClassVar[tuple[tuple[str, re.Pattern[str]], ...]] = (
        ("year", re.compile(r"^\s*(?:19|20)\d{2}\s*年")),
        ("standard", re.compile(r"^\s*(?:GB/T|ISO)\s*\d", re.I)),
        ("unit", re.compile(r"^\s*\d+(?:\.\d+)?\s*(?:GHz|mol/L|mm)\b", re.I)),
        ("equation-reference", re.compile(r"^\s*式\s*[\uff08(]?\d")),
        ("figure-reference", re.compile(r"^\s*图\s*\d")),
        ("table-reference", re.compile(r"^\s*表\s*\d")),
        ("version", re.compile(r"^\s*(?:Python\s+\d|v\d+(?:\.\d+)+)", re.I)),
        ("product-name", re.compile(r"^\s*Windows\s+\d", re.I)),
        ("sentence", re.compile(r"^\s*Chapter\s+\d+\s+(?:is|was|will)\b", re.I)),
        ("sample-name", re.compile(r"^\s*No\.\s*\d", re.I)),
        (
            "filename",
            re.compile(r"^\s*\d+(?:\.[A-Za-z][A-Za-z0-9]{0,7}|_[^\s]+\.[A-Za-z0-9]+)\s+"),
        ),
        ("percentage", re.compile(r"^\s*\d+(?:\.\d+)?%")),
        ("ratio", re.compile(r"^\s*\d+\s*:\s*\d+")),
        ("expression", re.compile(r"^\s*\d+\s*[\u00d7x]\s*\d+", re.I)),
        (
            "term",
            re.compile(
                r"^\s*(?:3D|5G|HTTP/2|A/B|R2-D2|C\+\+17|H2O|4K|6\u03c3|100BASE-T)"
                r"(?:\s|\b)",
                re.I,
            ),
        ),
    )
    _PREFIX_PATTERNS: ClassVar[tuple[tuple[str, re.Pattern[str], float], ...]] = (
        (
            "appendix",
            re.compile(
                r"^[\s\u3000]*(?:附录|Appendix)\s*"
                r"([A-Za-z0-9一二三四五六七八九十]+)[\s\u3000]+",
                re.I,
            ),
            0.99,
        ),
        (
            "chinese-chapter",
            re.compile(r"^[\s\u3000]*第\s*([一二三四五六七八九十百千万\d]+)\s*章[\s\u3000]*"),
            0.99,
        ),
        (
            "chinese-section",
            re.compile(r"^[\s\u3000]*第\s*([一二三四五六七八九十百千万\d]+)\s*节[\s\u3000]*"),
            0.99,
        ),
        (
            "chinese-part",
            re.compile(r"^[\s\u3000]*第\s*([一二三四五六七八九十百千万\d]+)\s*(?:部分|篇)[\s\u3000]*"),
            0.99,
        ),
        (
            "chinese-parenthesis",
            re.compile(
                r"^[\s\u3000]*[\uff08(]([一二三四五六七八九十百千万]+)"
                r"[\uff09)][\s\u3000]*"
            ),
            0.99,
        ),
        (
            "roman-unicode",
            re.compile(
                r"^[\s\u3000]*[\uff08(]([ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ]+)"
                r"[\uff09)][\s\u3000]*"
            ),
            0.99,
        ),
        (
            "arabic-parenthesis",
            re.compile(
                r"^[\s\u3000]*(?:[\uff08(](\d+)[\uff09)]|(\d+)[\uff09)])"
                r"[\s\u3000]*"
            ),
            0.99,
        ),
        (
            "roman-lower",
            re.compile(r"^[\s\u3000]*[\uff08(]([ivxlcdm]+)[\uff09)][\s\u3000]*"),
            0.98,
        ),
        (
            "arabic-hierarchy",
            re.compile(
                r"^[\s\u3000]*(\d+(?:\s*\.\s*\d+)+)"
                r"(?:[\u3001\uff0e\uff1a:]|[\s\u3000]+)"
                r"[\s\u3000]*"
            ),
            0.98,
        ),
        (
            "arabic",
            re.compile(r"^[\s\u3000]*(\d+)[\.\u3001\uff0e\uff1a:][\s\u3000]*"),
            0.98,
        ),
        (
            "roman-upper",
            re.compile(
                r"^[\s\u3000]*([IVXLCDM]+)[\.\u3001\uff0e\uff1a:][\s\u3000]*"
            ),
            0.97,
        ),
        (
            "chinese",
            re.compile(
                r"^[\s\u3000]*([一二三四五六七八九十百千万]+)"
                r"[\u3001\.\uff0e\uff1a:\uff09)][\s\u3000]*"
            ),
            0.98,
        ),
        (
            "alphabetic",
            re.compile(
                r"^[\s\u3000]*([A-Za-z])[\.\u3001\uff0e\uff1a:\uff09)]"
                r"[\s\u3000]*"
            ),
            0.96,
        ),
    )
    _CAPTION_PATTERNS: ClassVar[tuple[tuple[str, re.Pattern[str], float], ...]] = (
        (
            "figure-caption",
            re.compile(
                r"^[\s\u3000]*图\s*(\d+(?:[.-]\d+)*)"
                r"(?:[\uff1a:.\uff0e\u3001]|[\s\u3000]+)[\s\u3000]*"
            ),
            0.99,
        ),
        (
            "table-caption",
            re.compile(
                r"^[\s\u3000]*表\s*(\d+(?:[.-]\d+)*)"
                r"(?:[\uff1a:.\uff0e\u3001]|[\s\u3000]+)[\s\u3000]*"
            ),
            0.99,
        ),
    )

    def normalize(
        self,
        value: str,
        *,
        node_kind: str = "heading",
        node_id: str | None = None,
        preserve_exact: bool = False,
    ) -> NormalizedLabel:
        original = value
        stripped = value.strip()
        if preserve_exact:
            return NormalizedLabel(
                node_id=node_id,
                node_kind=node_kind,
                original=original,
                semantic=stripped,
                protected_reason="preserve-exact",
            )
        if node_kind not in self.STRUCTURAL_KINDS:
            return NormalizedLabel(
                node_id=node_id,
                node_kind=node_kind,
                original=original,
                semantic=stripped,
                protected_reason="non-structural-node",
            )
        protected_reason = self.protected_reason(stripped, node_kind=node_kind)
        if protected_reason:
            return NormalizedLabel(
                node_id=node_id,
                node_kind=node_kind,
                original=original,
                semantic=stripped,
                protected_reason=protected_reason,
            )

        remaining = value
        consumed = 0
        prefixes: list[LabelPrefix] = []
        for _ in range(self.MAX_PREFIXES):
            parsed = self._parse_one(remaining, consumed, node_kind=node_kind)
            if parsed is None:
                break
            prefix, next_value = parsed
            prefixes.append(prefix)
            consumed = prefix.span_end
            remaining = next_value

        semantic = remaining.strip()
        if not semantic:
            return NormalizedLabel(
                node_id=node_id,
                node_kind=node_kind,
                original=original,
                semantic=stripped,
                protected_reason="empty-semantic-label",
            )
        return NormalizedLabel(
            node_id=node_id,
            node_kind=node_kind,
            original=original,
            semantic=semantic,
            prefixes=prefixes,
            changed=bool(prefixes) and semantic != stripped,
        )

    def dry_run(self, value: str, *, node_kind: str = "heading") -> NormalizedLabel:
        return self.normalize(value, node_kind=node_kind)

    def protected_reason(self, value: str, *, node_kind: str = "heading") -> str | None:
        for reason, pattern in self._PROTECTIONS:
            if node_kind == "caption_label" and reason in {
                "equation-reference",
                "figure-reference",
                "table-reference",
            }:
                continue
            if pattern.search(value):
                return reason
        return None

    def _parse_one(
        self, value: str, absolute_start: int, *, node_kind: str
    ) -> tuple[LabelPrefix, str] | None:
        patterns = (
            self._CAPTION_PATTERNS + self._PREFIX_PATTERNS
            if node_kind == "caption_label"
            else self._PREFIX_PATTERNS
        )
        for family, pattern, confidence in patterns:
            match = pattern.match(value)
            if match is None:
                continue
            raw = match.group(0)
            token = next((group for group in match.groups() if group is not None), "")
            levels = (
                [part.strip() for part in re.split(r"\s*\.\s*", token)]
                if family in {"arabic-hierarchy", "figure-caption", "table-caption"}
                else [token]
            )
            return (
                LabelPrefix(
                    family=family if family != "arabic-hierarchy" else "arabic-decimal",
                    levels=levels,
                    raw=raw,
                    span_start=absolute_start,
                    span_end=absolute_start + len(raw),
                    confidence=confidence,
                ),
                value[match.end() :],
            )
        return None


class NumberingOwnerResolver:
    """Resolve one active owner per numbering category using explicit precedence."""

    def resolve(
        self,
        *,
        preference: str | None = None,
        template_has_heading_numbering: bool = False,
        source: str = "safe-default",
    ) -> NumberingContract:
        normalized = (preference or "auto").strip().casefold()
        contract = default_numbering_contract()
        diagnostics: list[NumberingDiagnostic] = []
        if normalized in {"none", "off", "取消编号", "无编号"}:
            heading = NumberingScheme(
                owner=NumberingOwner.NONE,
                format=NumberingFormat.NONE,
                pattern="",
                source="user-explicit",
            )
            decision_source = "user-explicit"
        elif normalized in {"author", "preserve", "保留原编号"}:
            heading = contract.headings.model_copy(
                update={"owner": NumberingOwner.AUTHOR, "source": "user-explicit"}
            )
            decision_source = "user-explicit"
        elif normalized in {"template", "保留模板"}:
            decision_source = "user-explicit"
            if template_has_heading_numbering:
                heading = contract.headings.model_copy(
                    update={"owner": NumberingOwner.TEMPLATE, "source": "template"}
                )
            else:
                heading = contract.headings.model_copy(update={"source": "safe-fallback"})
                diagnostics.append(
                    NumberingDiagnostic(
                        code="NUMBERING_TEMPLATE_UNAVAILABLE",
                        message="模板未声明标题编号, 已回退到 Renderer 编号。",
                        category="headings",
                        repair_node="numbering.owner.resolve",
                    )
                )
        elif template_has_heading_numbering:
            heading = contract.headings.model_copy(
                update={"owner": NumberingOwner.TEMPLATE, "source": "template"}
            )
            decision_source = "template"
        else:
            heading = contract.headings.model_copy(update={"source": source})
            decision_source = source
        return contract.model_copy(
            update={
                "headings": heading,
                "diagnostics": diagnostics,
                "decision_source": decision_source,
            }
        )
