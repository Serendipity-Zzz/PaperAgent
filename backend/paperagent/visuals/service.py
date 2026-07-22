from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

import httpx
import matplotlib

matplotlib.use("Agg")
from matplotlib import pyplot as plt
from pydantic import BaseModel, Field

from paperagent.agents.state import RequirementSpec, RequirementStatus
from paperagent.experiments import CapabilityReport, ExperimentResultPackage


class ChartSpec(BaseModel):
    title: str
    chart_type: str = Field(pattern=r"^(line|bar|scatter)$")
    x: list[float | str]
    y: list[float]
    y_error: list[float] | None = None
    x_label: str
    y_label: str
    unit: str
    simulated_data: bool = False
    data_file: str
    run_id: UUID


class FigureProvenance(BaseModel):
    source_type: str
    provider: str | None = None
    model: str | None = None
    prompt: str | None = None
    run_id: UUID | None = None
    data_file: str | None = None
    script: str | None = None
    sha256: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    real_experiment_evidence: bool = False


class ImageArtifact(BaseModel):
    image_id: UUID = Field(default_factory=uuid4)
    path: str
    width: int
    height: int
    provenance: FigureProvenance


class ChartRenderer:
    def render(self, spec: ChartSpec, output: Path) -> ImageArtifact:
        if not spec.x or not spec.y or len(spec.x) != len(spec.y):
            raise ValueError("chart x/y data must be non-empty and aligned")
        if not spec.unit.strip():
            raise ValueError("chart unit is required")
        if spec.y_error is not None and len(spec.y_error) != len(spec.y):
            raise ValueError("error bars must align with y values")
        figure, axis = plt.subplots(figsize=(8, 5), constrained_layout=True)
        if spec.chart_type == "bar":
            axis.bar(spec.x, spec.y, yerr=spec.y_error)
        elif spec.chart_type == "scatter":
            axis.scatter(spec.x, spec.y)
        else:
            axis.errorbar(spec.x, spec.y, yerr=spec.y_error, marker="o")
        axis.set(title=spec.title, xlabel=spec.x_label, ylabel=f"{spec.y_label} ({spec.unit})")
        if spec.simulated_data:
            axis.text(
                0.99, 0.01, "SIMULATED DATA", transform=axis.transAxes, ha="right", va="bottom"
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(output, dpi=180)
        plt.close(figure)
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        return ImageArtifact(
            path=str(output),
            width=1440,
            height=900,
            provenance=FigureProvenance(
                source_type="simulated_chart" if spec.simulated_data else "experiment_chart",
                run_id=spec.run_id,
                data_file=spec.data_file,
                script="paperagent.visuals.ChartRenderer",
                sha256=digest,
                real_experiment_evidence=not spec.simulated_data,
            ),
        )


class ImageRequest(BaseModel):
    prompt: str
    width: int = 1024
    height: int = 1024
    sensitive: bool = False
    approved: bool = False


class ImageProvider(Protocol):
    name: str
    model: str

    def generate(self, request: ImageRequest, output: Path) -> ImageArtifact: ...


class MockImageProvider:
    name = "mock"
    model = "mock-image-v1"

    def generate(self, request: ImageRequest, output: Path) -> ImageArtifact:
        from PIL import Image, ImageDraw

        output.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (request.width, request.height), "white")
        ImageDraw.Draw(image).text((30, 30), "MOCK GENERATED IMAGE", fill="black")
        image.save(output)
        return image_artifact(output, request, self.name, self.model)


class HttpImageProvider:
    name = "http"

    def __init__(
        self, base_url: str, api_key: str | None, model: str, client: httpx.Client | None = None
    ):
        self.base_url, self.api_key, self.model = base_url.rstrip("/"), api_key, model
        self.client = client or httpx.Client(timeout=120)

    def payload(self, request: ImageRequest) -> dict[str, object]:
        return {
            "model": self.model,
            "prompt": request.prompt,
            "size": f"{request.width}x{request.height}",
        }

    def generate(self, request: ImageRequest, output: Path) -> ImageArtifact:
        if not self.api_key:
            raise PermissionError(f"{self.name} image API key is missing")
        if not request.approved:
            raise PermissionError("image generation approval is required")
        response = self.client.post(
            f"{self.base_url}/images/generations",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=self.payload(request),
        )
        response.raise_for_status()
        body = response.json()
        encoded = body.get("data", [{}])[0].get("b64_json")
        if not encoded:
            raise RuntimeError("image provider returned no b64_json")
        import base64

        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(base64.b64decode(encoded))
        return image_artifact(output, request, self.name, self.model)


class OpenAIImageProvider(HttpImageProvider):
    name = "openai"


class SeedreamImageProvider(HttpImageProvider):
    name = "seedream"

    def payload(self, request: ImageRequest) -> dict[str, object]:
        return {
            "model": self.model,
            "prompt": request.prompt,
            "width": request.width,
            "height": request.height,
            "response_format": "b64_json",
        }


def image_artifact(output: Path, request: ImageRequest, provider: str, model: str) -> ImageArtifact:
    return ImageArtifact(
        path=str(output),
        width=request.width,
        height=request.height,
        provenance=FigureProvenance(
            source_type="ai_generated_illustration",
            provider=provider,
            model=model,
            prompt=request.prompt,
            sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
            real_experiment_evidence=False,
        ),
    )


class ImageProviderRouter:
    def __init__(self, providers: dict[str, ImageProvider]) -> None:
        self.providers = providers

    def select(self, configured_provider: str, *, api_key_present: bool) -> ImageProvider:
        if not api_key_present:
            return self.providers["mock"]
        if configured_provider not in self.providers:
            raise KeyError(configured_provider)
        return self.providers[configured_provider]


class VisualRoute(BaseModel):
    experiment: bool
    data_chart: bool
    deterministic_diagram: bool
    generated_image: bool
    approvals: list[str]
    reasons: list[str]


class VisualAgentRouter:
    def route(
        self,
        requirement: RequirementSpec,
        capability: CapabilityReport | None = None,
        *,
        user_rejected_experiment: bool = False,
    ) -> VisualRoute:
        if requirement.status is not RequirementStatus.CONFIRMED:
            raise PermissionError("visual routing requires confirmed requirements")
        confirmed = requirement.confirmed_requirement
        assert confirmed is not None
        experiment = confirmed.requires_experiment and not user_rejected_experiment
        reasons: list[str] = []
        approvals: list[str] = []
        if experiment:
            approvals.append("execute_experiment")
        if capability and capability.verdict == "unreasonable":
            experiment = False
            reasons.extend(capability.reasons)
        if confirmed.requires_generated_image:
            approvals.append("generate_image")
        return VisualRoute(
            experiment=experiment,
            data_chart=confirmed.requires_data_chart,
            deterministic_diagram=any("流程图" in item for item in confirmed.constraints),
            generated_image=confirmed.requires_generated_image,
            approvals=approvals,
            reasons=reasons or ["Route derived from confirmed Requirement Spec"],
        )


def result_as_evidence(package: ExperimentResultPackage) -> dict[str, object]:
    return {
        "source_type": "experiment_result"
        if package.eligible_as_experiment_evidence
        else "simulation_or_partial",
        "run_id": str(package.run_id),
        "metrics": package.metrics if package.eligible_as_experiment_evidence else {},
        "eligible": package.eligible_as_experiment_evidence,
        "payload_hash": hashlib.sha256(package.model_dump_json().encode()).hexdigest(),
    }
