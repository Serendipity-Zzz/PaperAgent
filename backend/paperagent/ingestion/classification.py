from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from paperagent.ingestion.schemas import SourceDocument


@dataclass(frozen=True)
class Classification:
    primary_type: str
    secondary_types: tuple[str, ...]
    confidence: float
    extracted: dict[str, object]
    signals: tuple[str, ...]
    overridden_from: str | None = None


@dataclass
class DocumentClassifier:
    overrides: dict[str, str] = field(default_factory=dict)
    history: list[tuple[str, str, str]] = field(default_factory=list)

    def classify(self, document: SourceDocument) -> Classification:
        combined = "\n".join(chunk.text for chunk in document.chunks[:200])
        scores: dict[str, int] = {
            "requirement": self._score(
                combined, ("需求", "目标", "必须", "禁止", "must", "shall", "验收", "约束")
            ),
            "manual": self._score(combined, ("步骤", "安装", "操作", "step", "warning", "注意")),
            "faq": self._score(combined, ("FAQ", "常见问题", "Q:", "A:", "问题", "答案")),
            "technical": self._score(combined, ("API", "版本", "配置", "architecture", "接口")),
            "meeting": self._score(combined, ("会议", "参会", "行动项", "decision", "minutes")),
        }
        ranked = sorted(scores, key=scores.get, reverse=True)  # type: ignore[arg-type]
        detected = ranked[0] if scores[ranked[0]] else "general"
        override = self.overrides.get(document.sha256)
        primary = override or detected
        secondary = tuple(item for item in ranked[1:] if scores[item] and item != primary)
        versions = re.findall(r"(?i)\bv?\d+\.\d+(?:\.\d+)?\b", combined)
        dates = re.findall(r"\b20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}\b", combined)
        extracted: dict[str, object] = {
            "versions": list(dict.fromkeys(versions)),
            "dates": list(dict.fromkeys(dates)),
            "goals": self._matching_lines(combined, ("目标", "purpose", "objective")),
            "constraints": self._matching_lines(combined, ("必须", "禁止", "must", "shall not")),
            "warnings": self._matching_lines(combined, ("警告", "注意", "warning")),
        }
        return Classification(
            primary_type=primary,
            secondary_types=secondary,
            confidence=min(0.5 + scores.get(detected, 0) * 0.1, 0.95),
            extracted=extracted,
            signals=tuple(item for item, score in scores.items() if score),
            overridden_from=detected if override and override != detected else None,
        )

    def override(self, document: SourceDocument, new_type: str) -> None:
        previous = self.classify(document).primary_type
        self.overrides[document.sha256] = new_type
        self.history.append((document.sha256, previous, new_type))

    @staticmethod
    def _score(text: str, terms: tuple[str, ...]) -> int:
        lowered = text.lower()
        return sum(lowered.count(term.lower()) for term in terms)

    @staticmethod
    def _matching_lines(text: str, terms: tuple[str, ...]) -> list[str]:
        return [
            line.strip()
            for line in text.splitlines()
            if line.strip() and any(term.lower() in line.lower() for term in terms)
        ][:20]


def is_stale(
    extracted: dict[str, object], *, today: date | None = None, max_days: int = 730
) -> bool:
    current = today or date.today()
    parsed: list[date] = []
    values = extracted.get("dates", [])
    if not isinstance(values, list):
        return False
    for value in values:
        try:
            parsed.append(date.fromisoformat(str(value).replace("/", "-").replace(".", "-")))
        except ValueError:
            continue
    return bool(parsed) and (current - max(parsed)).days > max_days
