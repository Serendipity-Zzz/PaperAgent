from __future__ import annotations

from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class CitationStyle(StrEnum):
    GB_T_7714 = "gb-t-7714"
    APA = "apa"
    IEEE = "ieee"


class BibliographicItem(BaseModel):
    citation_id: UUID = Field(default_factory=uuid4)
    title: str
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    publisher: str | None = None
    container_title: str | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    doi: str | None = None
    url: str | None = None
    item_type: str = "article"
    verified: bool = False
    source_evidence_id: UUID | None = None


class FormattedCitation(BaseModel):
    citation_id: UUID
    inline: str
    bibliography: str
    verified: bool


class CitationStyleService:
    def format(
        self,
        item: BibliographicItem,
        style: CitationStyle,
        *,
        sequence: int,
        locator: str | None = None,
    ) -> FormattedCitation:
        authors = self._authors(item.authors)
        year = str(item.year) if item.year else "n.d."
        source = item.container_title or item.publisher or ""
        tail = self._identifier(item)
        if style is CitationStyle.APA:
            inline = f"({authors or item.title}, {year}{', ' + locator if locator else ''})"
            bibliography = f"{authors or 'Unknown'}. ({year}). {item.title}."
            if source:
                bibliography += f" {source}."
        elif style is CitationStyle.IEEE:
            inline = f"[{sequence}{', ' + locator if locator else ''}]"
            bibliography = f"[{sequence}] {authors + ', ' if authors else ''}“{item.title},”"
            if source:
                bibliography += f" {source},"
            bibliography += f" {year}."
        else:
            inline = f"[{sequence}{', ' + locator if locator else ''}]"
            bibliography = f"[{sequence}] {authors}. {item.title}[{self._type_code(item)}]."
            if source:
                bibliography += f" {source},"
            bibliography += f" {year}."
        if tail:
            bibliography += f" {tail}"
        return FormattedCitation(
            citation_id=item.citation_id,
            inline=inline,
            bibliography=bibliography.strip(),
            verified=item.verified,
        )

    @staticmethod
    def _authors(authors: list[str]) -> str:
        if len(authors) <= 3:
            return ", ".join(authors)
        return f"{', '.join(authors[:3])}, et al."

    @staticmethod
    def _identifier(item: BibliographicItem) -> str:
        if item.doi:
            return f"https://doi.org/{item.doi.removeprefix('https://doi.org/')}"
        return item.url or ""

    @staticmethod
    def _type_code(item: BibliographicItem) -> str:
        return {
            "article": "J",
            "book": "M",
            "thesis": "D",
            "report": "R",
            "web": "EB/OL",
        }.get(item.item_type, "Z")
