import base64
from pathlib import Path

import httpx

from paperagent.visuals.service import (
    ImageProviderRouter,
    ImageRequest,
    MockImageProvider,
    OpenAIImageProvider,
    SeedreamImageProvider,
)


def test_openai_seedream_mock_routing_payload_and_provenance(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        assert "model" in body and "prompt" in body
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(b"image-bytes").decode()}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    providers = {
        "mock": MockImageProvider(),
        "openai": OpenAIImageProvider("https://api.test/v1", "key", "gpt-image", client),
        "seedream": SeedreamImageProvider("https://seedream.test/v1", "key", "seedream-4", client),
    }
    router = ImageProviderRouter(providers)
    request = ImageRequest(prompt="scientific workflow", approved=True, width=64, height=64)
    mock = router.select("seedream", api_key_present=False).generate(request, tmp_path / "mock.png")
    assert mock.provenance.provider == "mock" and not mock.provenance.real_experiment_evidence
    openai = router.select("openai", api_key_present=True).generate(
        request, tmp_path / "openai.bin"
    )
    seedream = router.select("seedream", api_key_present=True).generate(
        request, tmp_path / "seedream.bin"
    )
    assert openai.provenance.model == "gpt-image"
    assert seedream.provenance.model == "seedream-4"
    assert openai.provenance.prompt == request.prompt
