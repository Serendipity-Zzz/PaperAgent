from __future__ import annotations

import math
import shutil
from enum import StrEnum
from pathlib import Path
from typing import ClassVar
from uuid import UUID
from xml.etree import ElementTree

import fitz
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from paperagent.agents.document_ir import BlockKind, DocumentIR, TableSpec
from paperagent.artifacts import ArtifactIntegrityError, ArtifactService
from paperagent.rendering.delivery import AssetBarrierStatus
from paperagent.rendering.layout import PageSpec
from paperagent.rendering.revision_store import DocumentRevisionStore


class FigurePlacement(StrEnum):
    INLINE_CENTER = "inline-center"
    LEFT = "left"
    RIGHT = "right"
    FULL_WIDTH = "full-width"
    FULL_PAGE = "full-page"


class ImageSizePolicy(BaseModel):
    max_width_ratio: float = Field(default=0.85, gt=0, le=1)
    placement: FigurePlacement = FigurePlacement.INLINE_CENTER
    allow_upscale: bool = False
    minimum_dpi: int = Field(default=150, ge=72, le=1200)


class FigureRef(BaseModel):
    artifact_id: UUID
    caption: str
    alt_text: str
    placement: FigurePlacement = FigurePlacement.INLINE_CENTER
    size_policy: ImageSizePolicy = Field(default_factory=ImageSizePolicy)
    provenance: dict[str, object] = Field(default_factory=dict)


class ImageMetrics(BaseModel):
    width_px: int = Field(gt=0)
    height_px: int = Field(gt=0)
    aspect_ratio: float = Field(gt=0)
    source_dpi: float | None = None


class FigureLayout(BaseModel):
    width_pt: float = Field(gt=0)
    height_pt: float = Field(gt=0)
    placement: FigurePlacement
    upscale: bool = False
    warnings: list[str] = Field(default_factory=list)


class AssetDerivative(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_artifact_id: str
    artifact_id: str
    target_format: str
    media_type: str
    relative_path: str
    sha256: str
    width_px: int
    height_px: int
    converter: str


class AssetAssemblyError(RuntimeError):
    def __init__(self, code: str, message: str, *, artifact_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.artifact_id = artifact_id


class ImageLayoutSolver:
    def solve(
        self,
        metrics: ImageMetrics,
        page: PageSpec,
        policy: ImageSizePolicy,
    ) -> FigureLayout:
        content_width = page.content_width_pt
        ratio = 1.0 if policy.placement is FigurePlacement.FULL_WIDTH else policy.max_width_ratio
        target_width = min(content_width, content_width * ratio)
        native_width = (
            metrics.width_px * 72 / metrics.source_dpi
            if metrics.source_dpi and metrics.source_dpi > 0
            else metrics.width_px * 72 / policy.minimum_dpi
        )
        upscale = target_width > native_width
        warnings: list[str] = []
        if upscale and not policy.allow_upscale:
            target_width = native_width
            warnings.append("low-resolution image was not enlarged")
            upscale = False
        target_width = max(1, target_width)
        target_height = target_width / metrics.aspect_ratio
        return FigureLayout(
            width_pt=target_width,
            height_pt=target_height,
            placement=policy.placement,
            upscale=upscale,
            warnings=warnings,
        )


class ResolvedTableLayout(BaseModel):
    column_widths_pt: list[float]
    repeat_header: bool
    landscape_required: bool


class TableLayoutSolver:
    def solve(self, table: TableSpec, page: PageSpec) -> ResolvedTableLayout:
        columns = max(len(row.cells) for row in table.rows)
        weights = [1.0] * columns
        for column in range(columns):
            lengths = [
                max(1, len(row.cells[column].text)) for row in table.rows if column < len(row.cells)
            ]
            weights[column] = min(40, max(lengths, default=1))
        total = sum(weights)
        minimum = 36.0
        available = page.content_width_pt
        widths = [max(minimum, available * weight / total) for weight in weights]
        scale = available / sum(widths)
        if scale < 1:
            widths = [width * scale for width in widths]
        landscape = columns >= 7 or any(width < minimum * 0.8 for width in widths)
        return ResolvedTableLayout(
            column_widths_pt=widths,
            repeat_header=table.repeat_header,
            landscape_required=landscape,
        )


class AssetDerivativeService:
    TARGET_SUFFIX: ClassVar[dict[str, str]] = {
        "markdown": ".svg",
        "docx": ".png",
        "xelatex": ".pdf",
    }

    def __init__(self, artifacts: ArtifactService) -> None:
        self.artifacts = artifacts

    def derivative(
        self,
        figure: FigureRef,
        *,
        target_format: str,
        document_id: str,
        revision_id: str,
    ) -> AssetDerivative:
        if target_format not in self.TARGET_SUFFIX:
            raise AssetAssemblyError("UNSUPPORTED_DERIVATIVE", target_format)
        source = self.artifacts.get(str(figure.artifact_id), verify=True)
        if not source.mime_type.startswith("image/"):
            raise AssetAssemblyError(
                "FIGURE_MIME_INVALID",
                "figure artifact is not an image",
                artifact_id=source.id,
            )
        source_path = self.artifacts.verify(source)
        metrics = image_metrics(source_path, source.mime_type)
        suffix = self._target_suffix(source_path, source.mime_type, target_format)
        target = (
            self.artifacts.project_root
            / "artifacts"
            / "derivatives"
            / source.id
            / f"{target_format}{suffix}"
        )
        converter = self._materialize(source_path, source.mime_type, target, target_format)
        record = self.artifacts.register(
            target,
            kind="figure_derivative",
            producer_tool="asset.derivative",
            source_artifact_ids=[source.id],
            document_id=document_id,
            revision_id=revision_id,
            derived_from_artifact_id=source.id,
        )
        return AssetDerivative(
            source_artifact_id=source.id,
            artifact_id=record.id,
            target_format=target_format,
            media_type=record.mime_type,
            relative_path=record.relative_path,
            sha256=record.sha256,
            width_px=metrics.width_px,
            height_px=metrics.height_px,
            converter=converter,
        )

    @staticmethod
    def _target_suffix(source: Path, mime_type: str, target_format: str) -> str:
        if target_format == "markdown" and mime_type in {
            "image/png",
            "image/jpeg",
            "image/webp",
            "image/svg+xml",
        }:
            return source.suffix.casefold()
        if target_format == "xelatex" and mime_type != "image/svg+xml":
            return source.suffix.casefold()
        if target_format == "docx" and mime_type == "image/png":
            return ".png"
        return AssetDerivativeService.TARGET_SUFFIX[target_format]

    @staticmethod
    def _materialize(
        source: Path,
        mime_type: str,
        target: Path,
        target_format: str,
    ) -> str:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            return AssetDerivativeService._converter_name(source, mime_type, target, target_format)
        if target_format == "markdown" or (
            mime_type != "image/svg+xml"
            and (
                target_format == "xelatex"
                or (target_format == "docx" and source.suffix.casefold() == ".png")
            )
        ):
            shutil.copy2(source, target)
            return "copy"
        if mime_type == "image/svg+xml":
            svg = fitz.open(stream=source.read_bytes(), filetype="svg")
            try:
                if target.suffix.casefold() == ".pdf":
                    target.write_bytes(svg.convert_to_pdf())
                    return "pymupdf-svg-to-pdf"
                page = svg[0]
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                pixmap.save(target)
                return "pymupdf-svg-to-png"
            finally:
                svg.close()
        with Image.open(source) as image:
            image.convert("RGBA" if image.mode == "RGBA" else "RGB").save(target, "PNG")
        return "pillow-to-png"

    @staticmethod
    def _converter_name(
        source: Path,
        mime_type: str,
        target: Path,
        target_format: str,
    ) -> str:
        if target_format == "markdown":
            return "copy"
        if mime_type == "image/svg+xml":
            return (
                "pymupdf-svg-to-pdf" if target.suffix.casefold() == ".pdf" else "pymupdf-svg-to-png"
            )
        if target_format == "xelatex" or (
            target_format == "docx" and source.suffix.casefold() == ".png"
        ):
            return "copy"
        return "pillow-to-png"


class AssembledDocument(BaseModel):
    document: DocumentIR
    derivatives: list[AssetDerivative]
    figure_layouts: dict[str, FigureLayout]


class AssetAssembler:
    def __init__(self, artifacts: ArtifactService) -> None:
        self.artifacts = artifacts
        self.derivatives = AssetDerivativeService(artifacts)

    def assemble(
        self,
        document: DocumentIR,
        *,
        target_formats: list[str],
        page: PageSpec | None = None,
    ) -> AssembledDocument:
        resolved = document.model_copy(deep=True)
        page = page or PageSpec()
        revision_id = str(
            document.metadata.get("revision_id")
            or DocumentRevisionStore.revision_id(document.document_id, document.revision)
        )
        figure_blocks = [
            block for block in resolved.iter_blocks() if block.kind is BlockKind.FIGURE
        ]
        required_ids = [
            str(block.figure.artifact_id)
            for block in figure_blocks
            if block.figure is not None and block.figure.artifact_id is not None
        ]
        manifest = resolved.asset_manifest
        barrier = AssetBarrier(self.artifacts).evaluate(
            required_ids,
            image_required=manifest.image_required if manifest is not None else bool(figure_blocks),
            expected_count=(
                manifest.required_figure_count if manifest is not None else len(figure_blocks)
            ),
        )
        if not barrier.ready:
            raise AssetAssemblyError(
                barrier.repair_code or "ASSET_NOT_READY",
                "required document assets are not ready: "
                f"pending={barrier.pending}, missing={barrier.missing}, "
                f"invalid={barrier.invalid}, ambiguous={barrier.ambiguous}",
            )
        generated: list[AssetDerivative] = []
        layouts: dict[str, FigureLayout] = {}
        for block in resolved.iter_blocks():
            if block.kind is not BlockKind.FIGURE:
                continue
            if block.figure is None or block.figure.artifact_id is None:
                raise AssetAssemblyError(
                    "FIGURE_ARTIFACT_REQUIRED",
                    "a figure requires a verified artifact id",
                    artifact_id=None,
                )
            reference = FigureRef(
                artifact_id=block.figure.artifact_id,
                caption=block.caption or "Figure",
                alt_text=block.figure.alt_text or block.caption or "Figure",
                placement=FigurePlacement.INLINE_CENTER,
                provenance=block.provenance.model_dump(mode="json"),
            )
            source = self.artifacts.get(str(reference.artifact_id), verify=True)
            metrics = image_metrics(self.artifacts.verify(source), source.mime_type)
            layouts[str(block.block_id)] = ImageLayoutSolver().solve(
                metrics, page, reference.size_policy
            )
            for target_format in dict.fromkeys(target_formats):
                derivative = self.derivatives.derivative(
                    reference,
                    target_format=target_format,
                    document_id=str(document.document_id),
                    revision_id=revision_id,
                )
                generated.append(derivative)
                block.figure.derivative_artifact_ids[target_format] = UUID(derivative.artifact_id)
            block.figure.sha256 = source.sha256
            block.figure.media_type = source.mime_type
        metadata = dict(resolved.metadata)
        metadata["asset_assembly"] = [item.model_dump(mode="json") for item in generated]
        resolved.metadata = metadata
        return AssembledDocument(
            document=resolved,
            derivatives=generated,
            figure_layouts=layouts,
        )


class AssetBarrierState(BaseModel):
    ready: bool
    status: AssetBarrierStatus
    required: list[str]
    expected_count: int = Field(ge=0)
    bound_count: int = Field(ge=0)
    ready_count: int = Field(ge=0)
    pending: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    invalid: list[str] = Field(default_factory=list)
    ambiguous: list[str] = Field(default_factory=list)
    repair_code: str | None = None


class AssetBarrier:
    def __init__(self, artifacts: ArtifactService) -> None:
        self.artifacts = artifacts

    def evaluate(
        self,
        required: list[str],
        *,
        image_required: bool = False,
        expected_count: int | None = None,
        ambiguous: list[str] | None = None,
    ) -> AssetBarrierState:
        unique = list(dict.fromkeys(required))
        expected = len(unique) if expected_count is None else expected_count
        if image_required and expected == 0:
            expected = 1
        pending: list[str] = []
        missing: list[str] = []
        invalid: list[str] = []
        ready_count = 0
        for artifact_id in unique:
            try:
                artifact = self.artifacts.get(artifact_id, verify=False)
            except KeyError:
                missing.append(artifact_id)
                continue
            if artifact.validation_status == "pending":
                pending.append(artifact_id)
                continue
            try:
                self.artifacts.verify(artifact)
            except (ArtifactIntegrityError, FileNotFoundError, PermissionError):
                invalid.append(artifact_id)
                continue
            ready_count += 1
        if expected > len(unique):
            missing.extend(
                f"required-unbound-{index}"
                for index in range(len(unique) + 1, expected + 1)
            )
        ambiguous = list(dict.fromkeys(ambiguous or []))
        repair = None
        if invalid:
            repair = "ASSET_INVALID"
            status = AssetBarrierStatus.INVALID
        elif ambiguous:
            repair = "ASSET_AMBIGUOUS"
            status = AssetBarrierStatus.AMBIGUOUS
        elif missing:
            repair = "ASSET_MISSING"
            status = AssetBarrierStatus.MISSING
        elif pending:
            repair = "ASSET_PENDING"
            status = AssetBarrierStatus.PENDING
        else:
            status = AssetBarrierStatus.READY
        return AssetBarrierState(
            ready=status is AssetBarrierStatus.READY,
            status=status,
            required=unique,
            expected_count=expected,
            bound_count=len(unique),
            ready_count=ready_count,
            pending=pending,
            missing=missing,
            invalid=invalid,
            ambiguous=ambiguous,
            repair_code=repair,
        )


def image_metrics(path: Path, media_type: str) -> ImageMetrics:
    if media_type == "image/svg+xml":
        root = ElementTree.fromstring(path.read_text(encoding="utf-8"))
        view_box = root.attrib.get("viewBox", "").replace(",", " ").split()
        if len(view_box) == 4:
            width, height = float(view_box[2]), float(view_box[3])
        else:
            width = _svg_number(root.attrib.get("width", "0"))
            height = _svg_number(root.attrib.get("height", "0"))
        if width <= 0 or height <= 0:
            raise AssetAssemblyError("FIGURE_DIMENSIONS_INVALID", "SVG dimensions are invalid")
        return ImageMetrics(
            width_px=math.ceil(width),
            height_px=math.ceil(height),
            aspect_ratio=width / height,
        )
    with Image.open(path) as image:
        width, height = image.size
        dpi_value = image.info.get("dpi")
        dpi = float(dpi_value[0]) if isinstance(dpi_value, tuple) and dpi_value else None
        return ImageMetrics(
            width_px=width,
            height_px=height,
            aspect_ratio=width / height,
            source_dpi=dpi,
        )


def _svg_number(value: str) -> float:
    cleaned = "".join(character for character in value if character.isdigit() or character in ".-")
    return float(cleaned or 0)
