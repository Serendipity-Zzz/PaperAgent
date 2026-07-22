from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field

from paperagent.agents.change_intent import ChangeIntent, ChangeIntentAgent, TypographyImpact
from paperagent.agents.document_ir import DocumentIR
from paperagent.rendering.artifacts import ArtifactVersion, ArtifactVersionService
from paperagent.rendering.changes import (
    PdfVisualDiff,
    RenderDependencyTracker,
    RenderInvalidation,
    VisualDiffReport,
)
from paperagent.rendering.fonts import FontResolution, FontResolver
from paperagent.rendering.renderers import (
    DocxRenderer,
    LatexRenderer,
    MarkdownRenderer,
    TypstRenderer,
)
from paperagent.rendering.revision_store import DocumentRevisionStore


class MissingFontError(RuntimeError):
    def __init__(self, resolutions: list[FontResolution]) -> None:
        super().__init__("one or more requested fonts require user action")
        self.resolutions = resolutions


class TargetedTypographyResult(BaseModel):
    document: DocumentIR
    impact: TypographyImpact
    invalidation: RenderInvalidation
    artifacts: list[ArtifactVersion] = Field(default_factory=list)
    font_resolutions: list[FontResolution] = Field(default_factory=list)
    visual_diff: VisualDiffReport | None = None
    render_errors: dict[str, str] = Field(default_factory=dict)


class TargetedTypographyService:
    """Apply a style-only revision and render only requested artifact formats."""

    def __init__(
        self,
        project_root: Path,
        *,
        fonts: FontResolver | None = None,
        typst: TypstRenderer | None = None,
        latex: LatexRenderer | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.fonts = fonts or FontResolver()
        self.typst = typst or TypstRenderer()
        self.latex = latex or LatexRenderer()
        self.revisions = DocumentRevisionStore(self.project_root)
        self.dependencies = RenderDependencyTracker()

    def apply(
        self,
        document: DocumentIR,
        intent: ChangeIntent,
        *,
        formats: list[str],
        allow_fallback: bool = False,
    ) -> TargetedTypographyResult:
        patch, resolutions = self._resolve_fonts(intent.typography_patch, allow_fallback)
        normalized_intent = intent.model_copy(update={"typography_patch": patch})
        updated, impact = ChangeIntentAgent.apply(document, normalized_intent)
        invalidation = self.dependencies.plan(
            document,
            updated,
            available_formats=formats,
        )
        try:
            self.revisions.load(document.document_id, document.revision)
        except FileNotFoundError:
            self.revisions.save(document)
        self.revisions.save(updated)

        directory = (
            self.project_root
            / ".paperagent"
            / "documents"
            / str(document.document_id)
            / "artifacts"
            / f"rev-{updated.revision:06d}"
        )
        directory.mkdir(parents=True, exist_ok=True)
        produced: list[Path] = []
        errors: dict[str, str] = {}
        for output_format in invalidation.formats:
            if output_format == "md":
                produced.append(MarkdownRenderer().render(updated, directory / "paper.md"))
            elif output_format == "docx":
                produced.append(DocxRenderer().render(updated, directory / "paper.docx"))
            elif output_format == "typst":
                path = directory / "paper.typ"
                self._atomic_write(path, self.typst.source(updated))
                produced.append(path)
            elif output_format == "latex":
                path = directory / "paper.tex"
                self._atomic_write(path, self.latex.source(updated))
                produced.append(path)
            elif output_format == "pdf":
                path = directory / "paper.pdf"
                result = self.typst.render(updated, path)
                if not result.success:
                    result = self.latex.render(updated, path)
                if result.success and result.output:
                    produced.append(result.output)
                else:
                    errors["pdf"] = result.error_code or "PDF_RENDER_FAILED"

        versions = ArtifactVersionService(self.project_root)
        try:
            artifacts = [
                versions.register(updated, path, previous_document=document) for path in produced
            ]
        finally:
            versions.close()

        visual_diff = self._visual_diff(document, updated, directory)
        return TargetedTypographyResult(
            document=updated,
            impact=impact,
            invalidation=invalidation,
            artifacts=artifacts,
            font_resolutions=resolutions,
            visual_diff=visual_diff,
            render_errors=errors,
        )

    def _resolve_fonts(
        self, patch: dict[str, object], allow_fallback: bool
    ) -> tuple[dict[str, object], list[FontResolution]]:
        normalized = dict(patch)
        resolutions: list[FontResolution] = []
        for field, value in patch.items():
            if not field.endswith("_font") or not isinstance(value, str):
                continue
            resolution = self.fonts.resolve(value, allow_fallback=allow_fallback)
            resolutions.append(resolution)
            if resolution.requires_user_action:
                raise MissingFontError(resolutions)
            if resolution.resolved:
                normalized[field] = resolution.resolved
        return normalized, resolutions

    def _visual_diff(
        self, before: DocumentIR, after: DocumentIR, output_directory: Path
    ) -> VisualDiffReport | None:
        old_pdf = (
            output_directory.parent / f"rev-{before.revision:06d}" / "paper.pdf"
        )
        new_pdf = output_directory / "paper.pdf"
        if not old_pdf.is_file() or not new_pdf.is_file():
            return None
        return PdfVisualDiff().compare(old_pdf, new_pdf, output_directory / "visual-diff")

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
