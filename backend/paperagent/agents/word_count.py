from __future__ import annotations

import re

from pydantic import BaseModel, Field

from paperagent.agents.document_ir import BlockKind, DocumentIR

CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
ENGLISH_PATTERN = re.compile(r"(?<![\w'-])[A-Za-z]+(?:['-][A-Za-z]+)*(?![\w'-])")


class CountPolicy(BaseModel):
    exclude_references: bool = True
    exclude_tables: bool = True
    exclude_code: bool = True
    exclude_equations: bool = True
    exclude_appendices: bool = True
    include_captions: bool = False


class SectionCount(BaseModel):
    section_id: str
    title: str
    chinese_chars: int = Field(ge=0)
    english_words: int = Field(ge=0)
    mixed_score: int = Field(ge=0)


class WordCountReport(BaseModel):
    chinese_chars: int
    english_words: int
    mixed_score: int
    by_section: list[SectionCount]
    excluded_blocks: int


class WordCountTool:
    def count(self, document: DocumentIR, policy: CountPolicy | None = None) -> WordCountReport:
        rules = policy or CountPolicy()
        sections: list[SectionCount] = []
        excluded = 0
        for section in document.sections:
            title_key = section.title.casefold()
            skip_section = (
                rules.exclude_references and title_key in {"参考文献", "references"}
            ) or (
                rules.exclude_appendices
                and (title_key.startswith("附录") or title_key.startswith("appendix"))
            )
            texts: list[str] = []
            for block in section.blocks:
                skip = (
                    skip_section
                    or (rules.exclude_tables and block.kind is BlockKind.TABLE)
                    or (rules.exclude_code and block.kind is BlockKind.CODE)
                    or (rules.exclude_equations and block.kind is BlockKind.EQUATION)
                )
                if skip:
                    excluded += 1
                    continue
                texts.append(block.text)
                if rules.include_captions and block.caption:
                    texts.append(block.caption)
            text = "\n".join(texts)
            chinese = len(CJK_PATTERN.findall(text))
            english = len(ENGLISH_PATTERN.findall(text))
            sections.append(
                SectionCount(
                    section_id=str(section.section_id),
                    title=section.title,
                    chinese_chars=chinese,
                    english_words=english,
                    mixed_score=chinese + english,
                )
            )
        return WordCountReport(
            chinese_chars=sum(item.chinese_chars for item in sections),
            english_words=sum(item.english_words for item in sections),
            mixed_score=sum(item.mixed_score for item in sections),
            by_section=sections,
            excluded_blocks=excluded,
        )
