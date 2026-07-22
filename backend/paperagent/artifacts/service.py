from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar
from uuid import uuid4
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader
from sqlalchemy import select

from paperagent.db.manager import DatabaseManager
from paperagent.db.models import ArtifactLinkRecord, ArtifactRecord, ExecutionRecordRow
from paperagent.execution.contracts import ArtifactRelation, CompletionClaim


class ArtifactIntegrityError(ValueError):
    pass


class ArtifactService:
    MANAGED_FOLDERS: ClassVar[frozenset[str]] = frozenset(
        {
            "artifacts",
            "runs",
            "sources",
            "versions",
            "workspaces",
            "previews",
        }
    )

    def __init__(
        self,
        databases: DatabaseManager,
        project_id: str,
        event_sink: Callable[[str, dict[str, object]], None] | None = None,
    ) -> None:
        self.databases = databases
        self.project_id = project_id
        self.project_root = databases.project_root(project_id).resolve()
        self.event_sink = event_sink

    def register(
        self,
        path: Path,
        *,
        kind: str,
        producer_tool: str,
        producer_version: str = "1.0.0",
        run_id: str | None = None,
        source_artifact_ids: list[str] | None = None,
        environment_ref: str | None = None,
        document_id: str | None = None,
        revision_id: str | None = None,
        derived_from_artifact_id: str | None = None,
        delivery_status: str = "not_applicable",
        validation_status: str = "valid",
        renderer_version: str | None = None,
        lineage: dict[str, object] | None = None,
    ) -> ArtifactRecord:
        target = self._managed_file(path)
        digest, size_bytes, prefix = self._hash_file(target)
        relative = target.relative_to(self.project_root).as_posix()
        mime_type = self._sniff_mime(target, prefix)
        with self.databases.project_session(self.project_id) as session:
            existing = session.scalar(
                select(ArtifactRecord).where(ArtifactRecord.relative_path == relative)
            )
            if existing is not None:
                if existing.sha256 != digest or existing.size_bytes != size_bytes:
                    raise ArtifactIntegrityError(
                        "an immutable artifact path was modified after registration"
                    )
                session.expunge(existing)
                return existing
            row = ArtifactRecord(
                id=str(uuid4()),
                kind=kind,
                mime_type=mime_type,
                original_name=self._safe_name(target.name),
                relative_path=relative,
                sha256=digest,
                size_bytes=size_bytes,
                producer_tool=producer_tool,
                producer_version=producer_version,
                run_id=run_id,
                document_id=document_id,
                revision_id=revision_id,
                derived_from_artifact_id=derived_from_artifact_id,
                source_artifact_ids_json=json.dumps(source_artifact_ids or []),
                environment_ref=environment_ref,
                preview_status="pending",
                validation_status=validation_status,
                delivery_status=delivery_status,
                renderer_version=renderer_version,
                lineage_json=json.dumps(lineage or {}, ensure_ascii=False, sort_keys=True),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            session.expunge(row)
            self._emit(
                "artifact.created",
                {"artifact_id": row.id, "kind": row.kind, "sha256": row.sha256},
            )
            self._emit(
                "artifact.validated",
                {"artifact_id": row.id, "validation_status": validation_status},
            )
            return row

    def link(
        self,
        artifact_id: str,
        *,
        relation: str,
        conversation_id: str | None = None,
        message_id: str | None = None,
        run_id: str | None = None,
        label: str = "",
        display_order: int = 0,
    ) -> ArtifactLinkRecord:
        if not any((conversation_id, message_id, run_id)):
            raise ValueError("artifact link requires an owner")
        with self.databases.project_session(self.project_id) as session:
            if session.get(ArtifactRecord, artifact_id) is None:
                raise KeyError(artifact_id)
            existing = session.scalar(
                select(ArtifactLinkRecord).where(
                    ArtifactLinkRecord.artifact_id == artifact_id,
                    ArtifactLinkRecord.conversation_id == conversation_id,
                    ArtifactLinkRecord.message_id == message_id,
                    ArtifactLinkRecord.run_id == run_id,
                    ArtifactLinkRecord.relation == relation,
                )
            )
            if existing is not None:
                session.expunge(existing)
                return existing
            row = ArtifactLinkRecord(
                id=str(uuid4()),
                artifact_id=artifact_id,
                conversation_id=conversation_id,
                message_id=message_id,
                run_id=run_id,
                relation=relation,
                label=label,
                display_order=display_order,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            session.expunge(row)
            self._emit(
                "artifact.linked",
                {
                    "artifact_id": artifact_id,
                    "message_id": message_id,
                    "run_id": run_id,
                    "relation": relation,
                },
            )
            return row

    def link_run_to_message(self, run_id: str, conversation_id: str, message_id: str) -> None:
        for index, artifact in enumerate(self.for_run(run_id)):
            self.link(
                artifact.id,
                relation=self._relation(artifact.kind),
                conversation_id=conversation_id,
                message_id=message_id,
                run_id=run_id,
                label=artifact.original_name,
                display_order=index,
            )

    def link_verified_to_message(
        self,
        artifact_ids: list[str],
        *,
        conversation_id: str,
        message_id: str,
    ) -> None:
        """Attach existing verified artifacts while preserving original run provenance."""
        start = len(self.links_for_message(message_id))
        for offset, artifact_id in enumerate(dict.fromkeys(artifact_ids)):
            artifact = self.get(artifact_id, verify=True)
            self.link(
                artifact.id,
                relation=self._relation(artifact.kind),
                conversation_id=conversation_id,
                message_id=message_id,
                run_id=artifact.run_id,
                label=artifact.original_name,
                display_order=start + offset,
            )

    def get(self, artifact_id: str, *, verify: bool = True) -> ArtifactRecord:
        with self.databases.project_session(self.project_id) as session:
            row = session.get(ArtifactRecord, artifact_id)
            if row is None:
                raise KeyError(artifact_id)
            session.expunge(row)
        if verify:
            self.verify(row)
        return row

    def verify(self, artifact: ArtifactRecord) -> Path:
        target = self._managed_file(self.project_root / artifact.relative_path)
        digest, size_bytes, _prefix = self._hash_file(target)
        if digest != artifact.sha256 or size_bytes != artifact.size_bytes:
            with self.databases.project_session(self.project_id) as session:
                current = session.get(ArtifactRecord, artifact.id)
                if current is not None:
                    current.validation_status = "invalid"
                    session.commit()
            self._emit(
                "artifact.validation_failed",
                {"artifact_id": artifact.id, "reason": "hash_mismatch"},
            )
            raise ArtifactIntegrityError("artifact content does not match its registered hash")
        if artifact.validation_status != "valid":
            raise ArtifactIntegrityError("artifact is not in a valid delivery state")
        return target

    def for_run(self, run_id: str) -> list[ArtifactRecord]:
        with self.databases.project_session(self.project_id) as session:
            rows = list(
                session.scalars(
                    select(ArtifactRecord)
                    .where(ArtifactRecord.run_id == run_id)
                    .order_by(ArtifactRecord.created_at, ArtifactRecord.id)
                )
            )
            for row in rows:
                session.expunge(row)
            return rows

    def links_for_message(self, message_id: str) -> list[dict[str, object]]:
        with self.databases.project_session(self.project_id) as session:
            rows = session.execute(
                select(ArtifactLinkRecord, ArtifactRecord)
                .join(ArtifactRecord, ArtifactRecord.id == ArtifactLinkRecord.artifact_id)
                .where(ArtifactLinkRecord.message_id == message_id)
                .where(ArtifactRecord.delivery_status != "rejected")
                .order_by(ArtifactLinkRecord.display_order, ArtifactLinkRecord.created_at)
            ).all()
            return [self.payload(artifact, link=link) for link, artifact in rows]

    def lookup(
        self,
        *,
        conversation_id: str,
        relation: str | None = None,
        run_id: str | None = None,
    ) -> list[dict[str, object]]:
        with self.databases.project_session(self.project_id) as session:
            query = (
                select(ArtifactLinkRecord, ArtifactRecord)
                .join(ArtifactRecord, ArtifactRecord.id == ArtifactLinkRecord.artifact_id)
                .where(ArtifactLinkRecord.conversation_id == conversation_id)
                .where(ArtifactRecord.delivery_status != "rejected")
            )
            if relation is not None:
                query = query.where(ArtifactLinkRecord.relation == relation)
            if run_id is not None:
                query = query.where(ArtifactLinkRecord.run_id == run_id)
            rows = session.execute(
                query.order_by(ArtifactLinkRecord.created_at.desc()).limit(50)
            ).all()
            return [self.payload(artifact, link=link) for link, artifact in rows]

    def record_execution(self, **values: object) -> None:
        with self.databases.project_session(self.project_id) as session:
            session.add(ExecutionRecordRow(**values))
            session.commit()

    @staticmethod
    def payload(
        artifact: ArtifactRecord, *, link: ArtifactLinkRecord | None = None
    ) -> dict[str, object]:
        value: dict[str, object] = {
            "id": artifact.id,
            "kind": artifact.kind,
            "mime_type": artifact.mime_type,
            "original_name": artifact.original_name,
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
            "run_id": artifact.run_id,
            "document_id": artifact.document_id,
            "revision_id": artifact.revision_id,
            "derived_from_artifact_id": artifact.derived_from_artifact_id,
            "validation_status": artifact.validation_status,
            "delivery_status": artifact.delivery_status,
            "renderer_version": artifact.renderer_version,
            "lineage": json.loads(artifact.lineage_json or "{}"),
            "created_at": artifact.created_at.isoformat(),
        }
        if link is not None:
            value.update(
                {
                    "link_id": link.id,
                    "relation": link.relation,
                    "label": link.label,
                    "display_order": link.display_order,
                }
            )
        return value

    def _managed_file(self, path: Path) -> Path:
        target = path.resolve(strict=False)
        if self.project_root not in target.parents:
            raise PermissionError("artifact path is outside the owning project")
        relative = target.relative_to(self.project_root)
        if not relative.parts or relative.parts[0] not in self.MANAGED_FOLDERS:
            raise PermissionError("artifact path is outside managed artifact roots")
        if target.is_symlink() or any(parent.is_symlink() for parent in target.parents):
            raise PermissionError("artifact path contains a symbolic link")
        if not target.is_file():
            raise FileNotFoundError(target)
        return target

    @staticmethod
    def _safe_name(name: str) -> str:
        if Path(name).name != name or any(ord(character) < 32 for character in name):
            raise ValueError("unsafe artifact filename")
        return name[:255]

    @staticmethod
    def _hash_file(path: Path) -> tuple[str, int, bytes]:
        digest = hashlib.sha256()
        size_bytes = 0
        prefix = b""
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                if not prefix:
                    prefix = chunk[:32]
                size_bytes += len(chunk)
                digest.update(chunk)
        return digest.hexdigest(), size_bytes, prefix

    @staticmethod
    def _sniff_mime(path: Path, prefix: bytes) -> str:
        suffix = path.suffix.casefold()
        signatures = {
            ".pdf": (b"%PDF-", "application/pdf"),
            ".png": (b"\x89PNG\r\n\x1a\n", "image/png"),
            ".jpg": (b"\xff\xd8\xff", "image/jpeg"),
            ".jpeg": (b"\xff\xd8\xff", "image/jpeg"),
            ".docx": (
                b"PK\x03\x04",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
        }
        if suffix in signatures:
            signature, mime_type = signatures[suffix]
            if not prefix.startswith(signature):
                raise ArtifactIntegrityError(f"{suffix} artifact has an invalid signature")
            if suffix in {".png", ".jpg", ".jpeg"}:
                try:
                    with Image.open(path) as image:
                        image.verify()
                        if image.width < 1 or image.height < 1:
                            raise ArtifactIntegrityError("image dimensions must be positive")
                except (OSError, UnidentifiedImageError) as error:
                    raise ArtifactIntegrityError("image payload cannot be decoded") from error
            return mime_type
        if suffix == ".svg":
            try:
                root = ElementTree.fromstring(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ElementTree.ParseError) as error:
                raise ArtifactIntegrityError("SVG payload is not well-formed XML") from error
            if root.tag.rsplit("}", 1)[-1].casefold() != "svg":
                raise ArtifactIntegrityError("SVG payload has no svg root element")
            view_box = root.attrib.get("viewBox", "").split()
            if len(view_box) == 4:
                try:
                    if float(view_box[2]) <= 0 or float(view_box[3]) <= 0:
                        raise ArtifactIntegrityError("SVG viewBox dimensions must be positive")
                except ValueError as error:
                    raise ArtifactIntegrityError("SVG viewBox is invalid") from error
            elif not root.attrib.get("width") or not root.attrib.get("height"):
                raise ArtifactIntegrityError("SVG requires a positive viewBox or width and height")
            return "image/svg+xml"
        guessed = mimetypes.guess_type(path.name)[0]
        return guessed or "application/octet-stream"

    @staticmethod
    def _relation(kind: str) -> str:
        return kind if kind in {item.value for item in ArtifactRelation} else "output"

    def _emit(self, event_type: str, payload: dict[str, object]) -> None:
        if self.event_sink is not None:
            self.event_sink(event_type, payload)


class CompletionClaimValidator:
    CLAIM_PATTERN = re.compile(
        r"(?:已生成|已创建|已导出|generated|created|exported).{0,80}"
        r"(?:\.pdf|\.docx|\.md|\.png|\.svg|\.csv|\.py|PDF|Word|图片|图像|"
        r"数据文件|源码|代码)",
        re.I | re.S,
    )

    def __init__(self, artifacts: ArtifactService) -> None:
        self.artifacts = artifacts

    def claims_from_message(self, run_id: str, content: str) -> list[CompletionClaim]:
        return [
            CompletionClaim(run_id=run_id, statement=match.group(0), claim_type="file")
            for match in self.CLAIM_PATTERN.finditer(content)
        ]

    def validate(self, run_id: str, content: str) -> list[ArtifactRecord]:
        claims = self.claims_from_message(run_id, content)
        artifacts = self.artifacts.for_run(run_id)
        for artifact in artifacts:
            self.artifacts.verify(artifact)
        if claims and not artifacts:
            raise ArtifactIntegrityError(
                "final response claims a generated file but no verified artifact exists"
            )
        for claim in claims:
            expected = self._expected_artifact(claim.statement)
            if expected is not None and not any(expected(artifact) for artifact in artifacts):
                raise ArtifactIntegrityError(
                    "final response claims an artifact type that was not verified for this run"
                )
        for artifact in artifacts:
            if artifact.original_name.casefold().endswith((".md", ".docx", ".pdf")):
                self._validate_document_artifact(
                    artifact,
                    require_images=bool(re.search(r"图片|图像|实验图|figure|image", content, re.I)),
                )
        return artifacts

    def _validate_document_artifact(
        self, artifact: ArtifactRecord, *, require_images: bool
    ) -> None:
        path = self.artifacts.verify(artifact)
        if artifact.producer_tool == "document.render" and (
            not artifact.document_id
            or not artifact.revision_id
            or not artifact.derived_from_artifact_id
        ):
            raise ArtifactIntegrityError(
                "rendered document is missing canonical document/revision derivation evidence"
            )
        if artifact.producer_tool == "document.render":
            assert artifact.derived_from_artifact_id is not None
            canonical = self.artifacts.get(artifact.derived_from_artifact_id, verify=True)
            if (
                canonical.document_id != artifact.document_id
                or canonical.revision_id != artifact.revision_id
            ):
                raise ArtifactIntegrityError(
                    "rendered document lineage does not match its canonical revision"
                )
            try:
                canonical_payload = json.loads(
                    self.artifacts.verify(canonical).read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise ArtifactIntegrityError("canonical document evidence is unreadable") from error
            hashes = canonical_payload.get("hashes")
            if not isinstance(canonical_payload.get("typography"), dict) or not isinstance(
                hashes, dict
            ):
                raise ArtifactIntegrityError(
                    "canonical typography or layout hash evidence is missing"
                )
            if not hashes.get("style_hash") or not hashes.get("asset_set_hash"):
                raise ArtifactIntegrityError("canonical style or asset-set evidence is incomplete")
        suffix = path.suffix.casefold()
        expected_mime = {
            ".md": {"text/markdown", "text/plain"},
            ".docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
            ".pdf": {"application/pdf"},
        }.get(suffix)
        if expected_mime is not None and artifact.mime_type not in expected_mime:
            raise ArtifactIntegrityError(
                f"document MIME does not match its native format: {artifact.mime_type}"
            )
        placeholder = "Verified source content is supplied by the renderer."
        if suffix == ".md":
            text = path.read_text(encoding="utf-8")
            if placeholder in text or re.search(r"^\\(?:#|\*|-)", text, re.M):
                raise ArtifactIntegrityError("Markdown artifact contains private or escaped markup")
            if require_images and not re.search(r"!\[[^]]*]\([^)]+\)", text):
                raise ArtifactIntegrityError("required image is not embedded in Markdown")
            return
        if suffix == ".docx":
            try:
                with ZipFile(path) as archive:
                    names = set(archive.namelist())
                    if "word/document.xml" not in names:
                        raise ArtifactIntegrityError("DOCX native document structure is missing")
                    document_xml = archive.read("word/document.xml").decode(
                        "utf-8", errors="replace"
                    )
                    if placeholder in document_xml:
                        raise ArtifactIntegrityError("DOCX contains a private placeholder")
                    if require_images and not any(name.startswith("word/media/") for name in names):
                        raise ArtifactIntegrityError("required image is not embedded in DOCX")
            except BadZipFile as error:
                raise ArtifactIntegrityError("DOCX container is invalid") from error
            return
        if suffix == ".pdf":
            try:
                reader = PdfReader(path)
                text = "\n".join(page.extract_text() or "" for page in reader.pages)
            except Exception as error:
                raise ArtifactIntegrityError("PDF structure is invalid") from error
            if not reader.pages or placeholder in text:
                raise ArtifactIntegrityError("PDF is empty or contains a private placeholder")
            if require_images:
                image_count = sum(len(page.images) for page in reader.pages)
                if image_count == 0:
                    raise ArtifactIntegrityError("required image is not embedded in PDF")

    @staticmethod
    def _expected_artifact(statement: str) -> Callable[[ArtifactRecord], bool]:
        lowered = statement.casefold()
        suffixes = [
            suffix
            for suffix in (".pdf", ".docx", ".md", ".png", ".svg", ".csv", ".py")
            if suffix in lowered
        ]
        if "pdf" in lowered and ".pdf" not in suffixes:
            suffixes.append(".pdf")
        if "word" in lowered and ".docx" not in suffixes:
            suffixes.append(".docx")
        if "源码" in statement or "代码" in statement:
            suffixes.append(".py")
        image_claim = "图片" in statement or "图像" in statement
        data_claim = "数据文件" in statement

        def matches(artifact: ArtifactRecord) -> bool:
            if suffixes and artifact.original_name.casefold().endswith(tuple(suffixes)):
                return True
            if image_claim and artifact.kind == "figure":
                return True
            if data_claim and artifact.kind == "data":
                return True
            return not suffixes and not image_claim and not data_claim

        return matches
