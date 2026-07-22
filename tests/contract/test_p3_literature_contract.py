import httpx
import pytest

from paperagent.literature.adapters import (
    ArxivAdapter,
    CrossrefAdapter,
    LiteratureCache,
    LiteratureService,
    OpenAlexAdapter,
    normalize_doi,
)


@pytest.mark.anyio
async def test_crossref_verification_cache_and_offline(tmp_path) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert "10.1234%2Fpaper" in str(request.url)
        return httpx.Response(
            200,
            json={
                "message": {
                    "title": ["Verified Paper"],
                    "author": [{"given": "A", "family": "Author"}],
                    "published": {"date-parts": [[2025]]},
                    "DOI": "10.1234/paper",
                    "license": [{"URL": "https://creativecommons.org/licenses/by/4.0/"}],
                    "link": [{"intended-application": "text-mining"}],
                }
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = LiteratureCache(tmp_path / "literature.db")
    service = LiteratureService(
        CrossrefAdapter(client), OpenAlexAdapter(client), ArxivAdapter(client), cache
    )
    first = await service.verify_doi("https://doi.org/10.1234/PAPER")
    second = await service.verify_doi("10.1234/paper", offline=True)
    assert first == second
    assert first and first.title == "Verified Paper" and first.open_access
    assert calls == 1


@pytest.mark.anyio
async def test_openalex_fallback_arxiv_dedup_and_invalid_doi(tmp_path) -> None:
    atom = b"""<?xml version='1.0'?>
    <feed xmlns='http://www.w3.org/2005/Atom'>
      <entry><id>https://arxiv.org/abs/1</id><title>Same Paper</title>
      <published>2024-01-01T00:00:00Z</published><author><name>Alice</name></author>
      <summary>Abstract</summary></entry>
      <entry><id>https://arxiv.org/abs/2</id><title>Same Paper</title>
      <published>2024-01-02T00:00:00Z</published><author><name>Alice</name></author>
      <summary>Abstract</summary></entry>
    </feed>"""

    async def handler(request: httpx.Request) -> httpx.Response:
        if "crossref" in request.url.host:
            return httpx.Response(404)
        if "openalex" in request.url.host:
            return httpx.Response(
                200,
                json={
                    "display_name": "OpenAlex Paper",
                    "publication_year": 2023,
                    "authorships": [{"author": {"display_name": "Researcher"}}],
                    "open_access": {"is_oa": True, "oa_status": "gold"},
                    "id": "https://openalex.org/W1",
                },
            )
        return httpx.Response(200, content=atom)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = LiteratureService(
        CrossrefAdapter(client),
        OpenAlexAdapter(client),
        ArxivAdapter(client),
        LiteratureCache(tmp_path / "cache.db"),
    )
    verified = await service.verify_doi("10.5555/openalex")
    assert verified and verified.source == "openalex"
    pack = await service.evidence_pack("topic")
    assert len(pack.records) == 1
    with pytest.raises(ValueError):
        normalize_doi("not a doi")
