from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from paperagent.preview.renderers import (
    DEFAULT_RENDERERS,
    MetadataRenderer,
    PreviewRenderer,
)
from paperagent.preview.schemas import (
    Annotation,
    PreviewArtifact,
    PreviewFidelity,
    PreviewPart,
    PreviewStatus,
)
from paperagent.preview.store import PreviewStore


class PreviewService:
    """Render safe, cacheable previews without coupling annotations to cache lifetime."""

    def __init__(
        self,
        project_root: Path,
        renderers: tuple[PreviewRenderer, ...] = DEFAULT_RENDERERS,
    ) -> None:
        self.project_root = project_root.resolve()
        self.store = PreviewStore(self.project_root / "preview.db")
        self.renderers = renderers
        self.fallback = MetadataRenderer()

    def close(self) -> None:
        self.store.close()

    def _renderer_for(self, path: Path) -> PreviewRenderer:
        extension = path.suffix.lower()
        return next(
            (renderer for renderer in self.renderers if extension in renderer.extensions),
            self.fallback,
        )

    @staticmethod
    def _cache_key(
        *, source_hash: str, renderer: PreviewRenderer, options: dict[str, object]
    ) -> str:
        material = json.dumps(
            {
                "source_hash": source_hash,
                "renderer": renderer.name,
                "renderer_version": renderer.version,
                "options": options,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(material).hexdigest()

    def render(
        self,
        path: Path,
        *,
        file_id: str,
        source_hash: str,
        source_name: str,
        options: dict[str, object] | None = None,
    ) -> PreviewArtifact:
        resolved = path.resolve()
        if self.project_root not in resolved.parents or not resolved.is_file():
            raise ValueError("Preview source must be an existing project file")
        renderer = self._renderer_for(resolved)
        render_options = options or {}
        cache_key = self._cache_key(
            source_hash=source_hash, renderer=renderer, options=render_options
        )
        cached = self.store.find(cache_key)
        if cached is not None and cached.status in {PreviewStatus.READY, PreviewStatus.FAILED}:
            return cached

        now = datetime.now(UTC)
        artifact = cached or PreviewArtifact(
            source_file_id=file_id,
            source_hash=source_hash,
            source_name=source_name,
            media_type="application/octet-stream",
            status=PreviewStatus.QUEUED,
            fidelity=PreviewFidelity.METADATA,
            renderer=renderer.name,
            renderer_version=renderer.version,
            cache_key=cache_key,
            payload={"checkpoint": "queued", "options": render_options},
            created_at=now,
            updated_at=now,
        )
        self.store.save(artifact)
        artifact.status = PreviewStatus.RENDERING
        artifact.payload["checkpoint"] = "rendering"
        artifact.updated_at = datetime.now(UTC)
        self.store.save(artifact)
        try:
            result = renderer.render(resolved, file_id=file_id, source_hash=source_hash)
            artifact.media_type = result.media_type
            artifact.fidelity = result.fidelity
            artifact.capabilities = result.capabilities
            artifact.payload = result.payload | {
                "checkpoint": "complete",
                "options": render_options,
            }
            artifact.part_count = len(result.parts)
            artifact.status = PreviewStatus.READY
            artifact.updated_at = datetime.now(UTC)
            self.store.save(artifact, result.parts)
        except Exception as error:  # renderer boundaries must not break the project session
            fallback = self.fallback.render(resolved, file_id=file_id, source_hash=source_hash)
            artifact.media_type = fallback.media_type
            artifact.fidelity = fallback.fidelity
            artifact.capabilities = fallback.capabilities
            artifact.payload = fallback.payload | {
                "checkpoint": "failed",
                "options": render_options,
            }
            artifact.status = PreviewStatus.FAILED
            artifact.error_code = f"PREVIEW_{renderer.name.upper().replace('-', '_')}_FAILED"
            artifact.error_message = str(error)[:500]
            artifact.updated_at = datetime.now(UTC)
            self.store.save(artifact, [])
        return artifact

    def artifact(self, artifact_id: str) -> PreviewArtifact:
        artifact = self.store.get(artifact_id)
        if artifact is None:
            raise KeyError("preview artifact not found")
        return artifact

    def parts(self, artifact_id: str, *, offset: int = 0, limit: int = 100) -> list[PreviewPart]:
        self.artifact(artifact_id)
        return self.store.parts(artifact_id, offset=max(offset, 0), limit=min(max(limit, 1), 500))

    def annotate(self, annotation: Annotation) -> Annotation:
        artifact = self.artifact(str(annotation.artifact_id))
        if annotation.project_id != self.project_root.name:
            raise ValueError("annotation project does not match preview project")
        if annotation.anchor.source_file_id != artifact.source_file_id:
            raise ValueError("annotation source does not match preview artifact")
        if not annotation.anchor.valid_for_hash(artifact.source_hash):
            annotation.status = "orphaned"
        self.store.annotate(annotation)
        return annotation

    def annotations(self, source_file_id: str, current_hash: str) -> list[Annotation]:
        annotations = self.store.annotations(source_file_id)
        for annotation in annotations:
            if not annotation.anchor.valid_for_hash(current_hash):
                annotation.status = "orphaned"
        return annotations

    def clear_cache(self) -> int:
        return self.store.clear_cache()
