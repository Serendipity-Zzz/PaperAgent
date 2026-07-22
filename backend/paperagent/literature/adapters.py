from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import httpx
from defusedxml import ElementTree

DOI_PATTERN = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)


def normalize_doi(value: str) -> str:
    match = DOI_PATTERN.search(value.strip())
    if not match:
        raise ValueError("Invalid DOI")
    return match.group(0).rstrip(".,;)").lower()


@dataclass(frozen=True)
class LiteratureRecord:
    title: str
    authors: tuple[str, ...]
    year: int | None
    doi: str | None
    source: str
    source_uri: str
    abstract: str | None = None
    license: str | None = None
    open_access: bool | None = None


@dataclass(frozen=True)
class EvidencePack:
    query: str
    records: tuple[LiteratureRecord, ...]
    unverifiable: tuple[str, ...] = field(default_factory=tuple)


class LiteratureCache:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS literature_cache(
                key TEXT PRIMARY KEY, payload TEXT NOT NULL
            )
            """
        )

    def get(self, key: str) -> LiteratureRecord | None:
        row = self.connection.execute(
            "SELECT payload FROM literature_cache WHERE key=?", (key,)
        ).fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        data["authors"] = tuple(data["authors"])
        return LiteratureRecord(**data)

    def put(self, key: str, record: LiteratureRecord) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO literature_cache(key,payload) VALUES (?,?)",
                (key, json.dumps(record.__dict__, ensure_ascii=False)),
            )


class CrossrefAdapter:
    def __init__(self, client: httpx.AsyncClient, *, mailto: str | None = None) -> None:
        self.client = client
        self.mailto = mailto

    async def by_doi(self, doi: str) -> LiteratureRecord | None:
        normalized = normalize_doi(doi)
        parameters = {"mailto": self.mailto} if self.mailto else None
        response = await self.client.get(
            f"https://api.crossref.org/works/{quote(normalized, safe='')}", params=parameters
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        message = response.json()["message"]
        title = (message.get("title") or [""])[0]
        if not title:
            return None
        authors = tuple(
            " ".join(part for part in (author.get("given"), author.get("family")) if part)
            for author in message.get("author", [])
        )
        date_parts = (message.get("published") or message.get("issued") or {}).get("date-parts", [])
        year = date_parts[0][0] if date_parts and date_parts[0] else None
        links = message.get("link", [])
        return LiteratureRecord(
            title=title,
            authors=authors,
            year=year,
            doi=normalized,
            source="crossref",
            source_uri=f"https://doi.org/{normalized}",
            license=(message.get("license") or [{}])[0].get("URL"),
            open_access=any(
                "text-mining" in item.get("intended-application", "") for item in links
            ),
        )


class OpenAlexAdapter:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def by_doi(self, doi: str) -> LiteratureRecord | None:
        normalized = normalize_doi(doi)
        response = await self.client.get(
            f"https://api.openalex.org/works/https://doi.org/{normalized}"
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = response.json()
        title = data.get("display_name")
        if not title:
            return None
        authors = tuple(
            item.get("author", {}).get("display_name", "") for item in data.get("authorships", [])
        )
        oa = data.get("open_access") or {}
        return LiteratureRecord(
            title=title,
            authors=authors,
            year=data.get("publication_year"),
            doi=normalized,
            source="openalex",
            source_uri=data.get("id") or f"https://doi.org/{normalized}",
            license=oa.get("oa_status"),
            open_access=oa.get("is_oa"),
        )


class ArxivAdapter:
    ATOM = "http://www.w3.org/2005/Atom"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def search(self, query: str, *, limit: int = 10) -> list[LiteratureRecord]:
        response = await self.client.get(
            "https://export.arxiv.org/api/query",
            params={"search_query": f"all:{query}", "start": 0, "max_results": limit},
        )
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)
        records: list[LiteratureRecord] = []
        for entry in root.findall(f"{{{self.ATOM}}}entry"):
            title = " ".join((entry.findtext(f"{{{self.ATOM}}}title") or "").split())
            uri = entry.findtext(f"{{{self.ATOM}}}id") or ""
            if not title or not uri:
                continue
            authors = tuple(
                author.findtext(f"{{{self.ATOM}}}name") or ""
                for author in entry.findall(f"{{{self.ATOM}}}author")
            )
            published = entry.findtext(f"{{{self.ATOM}}}published") or ""
            records.append(
                LiteratureRecord(
                    title=title,
                    authors=authors,
                    year=int(published[:4]) if published[:4].isdigit() else None,
                    doi=None,
                    source="arxiv",
                    source_uri=uri,
                    abstract=" ".join((entry.findtext(f"{{{self.ATOM}}}summary") or "").split()),
                    license="arXiv distribution license",
                    open_access=True,
                )
            )
        return records


class LiteratureService:
    def __init__(
        self,
        crossref: CrossrefAdapter,
        openalex: OpenAlexAdapter,
        arxiv: ArxivAdapter,
        cache: LiteratureCache,
    ) -> None:
        self.crossref = crossref
        self.openalex = openalex
        self.arxiv = arxiv
        self.cache = cache

    async def verify_doi(self, doi: str, *, offline: bool = False) -> LiteratureRecord | None:
        normalized = normalize_doi(doi)
        cached = self.cache.get(f"doi:{normalized}")
        if cached or offline:
            return cached
        for adapter in (self.crossref, self.openalex):
            record = await adapter.by_doi(normalized)
            if record:
                self.cache.put(f"doi:{normalized}", record)
                return record
        return None

    async def evidence_pack(self, query: str, *, offline: bool = False) -> EvidencePack:
        if offline:
            return EvidencePack(query=query, records=())
        records = await self.arxiv.search(query)
        deduplicated: dict[str, LiteratureRecord] = {}
        for record in records:
            key = record.doi or re.sub(r"\W+", "", record.title.lower())
            deduplicated.setdefault(key, record)
        return EvidencePack(query=query, records=tuple(deduplicated.values()))
