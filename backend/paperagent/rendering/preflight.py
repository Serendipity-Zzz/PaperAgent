from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from urllib.parse import unquote, urlparse
from uuid import UUID
from zipfile import BadZipFile, ZipFile

from pypdf import PdfReader

from paperagent.agents.document_ir import CURRENT_DOCUMENT_IR_SCHEMA, BlockKind, DocumentIR
from paperagent.artifacts import ArtifactIntegrityError, ArtifactService
from paperagent.rendering.delivery import (
    DeliveryIssueCategory,
    DeliveryIssueSeverity,
    DeliveryValidationIssue,
    DeliveryValidationResult,
)
from paperagent.schemas.presentation import (
    PageChromeToken,
    PageChromeTokenKind,
    PresentationExpectationManifest,
)


class RenderPreflight:
    def __init__(self, artifacts: ArtifactService | None = None) -> None:
        self.artifacts = artifacts

    def validate(
        self,
        document: DocumentIR,
        *,
        format_name: str,
        presentation_expectation: PresentationExpectationManifest | None = None,
    ) -> DeliveryValidationResult:
        issues: list[DeliveryValidationIssue] = []

        def issue(
            category: DeliveryIssueCategory,
            message: str,
            *,
            block_id: UUID | None = None,
            artifact_id: str | None = None,
            repair_node: str | None = None,
        ) -> None:
            issues.append(
                DeliveryValidationIssue(
                    category=category,
                    severity=DeliveryIssueSeverity.ERROR,
                    document_id=document.document_id,
                    revision=document.revision,
                    block_id=block_id,
                    artifact_id=artifact_id,
                    message=message,
                    repair_node=repair_node,
                )
            )

        if format_name not in {"md", "md_bundle", "docx", "pdf"}:
            issue(
                DeliveryIssueCategory.STRUCTURE_ERROR,
                f"unsupported output format: {format_name}",
                repair_node="document_layout",
            )
        if document.schema_version != CURRENT_DOCUMENT_IR_SCHEMA:
            issue(
                DeliveryIssueCategory.STRUCTURE_ERROR,
                f"unsupported canonical schema: {document.schema_version}",
                repair_node="document_compose",
            )
        if not document.sections or not list(document.iter_blocks()):
            issue(
                DeliveryIssueCategory.STRUCTURE_ERROR,
                "canonical document has no renderable section blocks",
                repair_node="document_compose",
            )
        if presentation_expectation is not None:
            self._validate_presentation(
                document,
                format_name=format_name,
                expectation=presentation_expectation,
                issue=issue,
            )
        figures = [block for block in document.iter_blocks() if block.kind is BlockKind.FIGURE]
        manifest = document.asset_manifest
        if manifest is not None and (
            manifest.document_id != document.document_id or manifest.revision != document.revision
        ):
            issue(
                DeliveryIssueCategory.STRUCTURE_ERROR,
                "asset manifest identity does not match the canonical revision",
                repair_node="document_asset_barrier",
            )
        if figures and manifest is None:
            issue(
                DeliveryIssueCategory.MISSING_ASSET,
                "figure-bearing canonical document has no asset requirement manifest",
                repair_node="document_asset_barrier",
            )
        if manifest is not None and manifest.image_required and not figures:
            issue(
                DeliveryIssueCategory.MISSING_ASSET,
                "the requirement mandates images but the canonical tree has no figures",
                repair_node="document_compose",
            )
        for block in figures:
            figure = block.figure
            if figure is None or figure.artifact_id is None:
                issue(
                    DeliveryIssueCategory.MISSING_ASSET,
                    "path-only figures cannot enter production rendering",
                    block_id=block.block_id,
                    repair_node="document_asset_barrier",
                )
                continue
            if self.artifacts is None:
                continue
            artifact_id = str(figure.artifact_id)
            try:
                artifact = self.artifacts.get(artifact_id, verify=True)
                if not artifact.mime_type.startswith("image/"):
                    raise ArtifactIntegrityError("figure artifact MIME is not image/*")
            except KeyError:
                issue(
                    DeliveryIssueCategory.MISSING_ASSET,
                    "figure artifact is absent from the current project",
                    block_id=block.block_id,
                    artifact_id=artifact_id,
                    repair_node="document_asset_barrier",
                )
            except (ArtifactIntegrityError, FileNotFoundError, PermissionError) as error:
                issue(
                    DeliveryIssueCategory.INVALID_ASSET,
                    str(error),
                    block_id=block.block_id,
                    artifact_id=artifact_id,
                    repair_node="document_asset_barrier",
                )
        return DeliveryValidationResult(passed=not issues, issues=issues)

    @staticmethod
    def _validate_presentation(
        document: DocumentIR,
        *,
        format_name: str,
        expectation: PresentationExpectationManifest,
        issue: Callable[..., None],
    ) -> None:
        presentation = document.presentation
        fields = {
            item.semantic_key: item
            for item in presentation.cover.fields
            if item.visible and item.value.strip()
        }
        missing_keys = [key for key in expectation.required_cover_keys if key not in fields]
        if missing_keys or (expectation.required_cover_keys and not presentation.cover.enabled):
            issue(
                DeliveryIssueCategory.STRUCTURE_ERROR,
                "required presentation cover fields are missing: " + ", ".join(missing_keys),
                repair_node="document_presentation_resolve",
            )

        header_tokens = RenderPreflight._page_tokens(document, area="header")
        footer_tokens = RenderPreflight._page_tokens(document, area="footer")
        header_text = {
            item.value
            for item in header_tokens
            if item.kind is PageChromeTokenKind.TEXT and item.value
        }
        footer_text = {
            item.value
            for item in footer_tokens
            if item.kind is PageChromeTokenKind.TEXT and item.value
        }
        for value in expectation.expected_header_text:
            if value not in header_text:
                issue(
                    DeliveryIssueCategory.STRUCTURE_ERROR,
                    "required presentation header text is absent from canonical page chrome",
                    repair_node="document_presentation_resolve",
                )
        for value in expectation.expected_footer_text:
            if value not in footer_text:
                issue(
                    DeliveryIssueCategory.STRUCTURE_ERROR,
                    "required presentation footer text is absent from canonical page chrome",
                    repair_node="document_presentation_resolve",
                )
        if expectation.require_page_number and not any(
            item.kind is PageChromeTokenKind.PAGE_NUMBER for item in footer_tokens
        ):
            issue(
                DeliveryIssueCategory.STRUCTURE_ERROR,
                "required page-number token is absent from canonical page chrome",
                repair_node="document_presentation_resolve",
            )
        if expectation.require_total_pages and not any(
            item.kind is PageChromeTokenKind.TOTAL_PAGES for item in footer_tokens
        ):
            issue(
                DeliveryIssueCategory.STRUCTURE_ERROR,
                "required total-pages token is absent from canonical page chrome",
                repair_node="document_presentation_resolve",
            )
        if expectation.hide_on_cover and not presentation.page_chrome.different_first_page:
            issue(
                DeliveryIssueCategory.STRUCTURE_ERROR,
                "page chrome must be hidden or separately defined on the cover",
                repair_node="document_presentation_resolve",
            )

        requires_physical_page_chrome = bool(
            expectation.expected_header_text
            or expectation.expected_footer_text
            or expectation.require_page_number
            or expectation.require_total_pages
        )
        if (
            format_name in {"md", "md_bundle"}
            and requires_physical_page_chrome
            and not expectation.allow_format_degradation
        ):
            issue(
                DeliveryIssueCategory.STRUCTURE_ERROR,
                "portable Markdown cannot satisfy physical page-header, footer or page-number "
                "requirements without an accepted capability degradation",
                repair_node="document_layout",
            )

    @staticmethod
    def _page_tokens(document: DocumentIR, *, area: str) -> list[PageChromeToken]:
        chrome = document.presentation.page_chrome
        sections = [chrome.default, chrome.first_page, chrome.odd_page, chrome.even_page]
        tokens: list[PageChromeToken] = []
        for section in sections:
            if section is None:
                continue
            line = section.header if area == "header" else section.footer
            tokens.extend([*line.left, *line.center, *line.right])
        return tokens


class RenderedArtifactValidator:
    PRIVATE_MARKERS = (
        "Verified source content is supplied by the renderer.",
        "PaperAgent 排版修订",
        "PRIVATE_PLACEHOLDER",
    )

    def validate(
        self,
        path: Path,
        *,
        format_name: str,
        required_image_count: int,
        document_id: UUID,
        revision: int,
        document: DocumentIR | None = None,
        presentation_expectation: PresentationExpectationManifest | None = None,
    ) -> DeliveryValidationResult:
        issues: list[DeliveryValidationIssue] = []

        def add(category: DeliveryIssueCategory, message: str) -> None:
            issues.append(
                DeliveryValidationIssue(
                    category=category,
                    document_id=document_id,
                    revision=revision,
                    message=message,
                    repair_node="document_render",
                )
            )

        try:
            text = ""
            cover_text = ""
            header_text = ""
            footer_text = ""
            field_instructions = ""
            image_count = 0
            if format_name in {"md", "md_bundle"}:
                markdown_path = path
                bundle_names: set[str] = set()
                if format_name == "md_bundle":
                    with ZipFile(path) as archive:
                        bundle_names = set(archive.namelist())
                        markdown_names = [
                            name for name in archive.namelist() if name.casefold().endswith(".md")
                        ]
                        if not markdown_names:
                            add(DeliveryIssueCategory.STRUCTURE_ERROR, "bundle has no Markdown")
                        else:
                            text = archive.read(markdown_names[0]).decode("utf-8")
                            image_count = len(
                                [
                                    name
                                    for name in archive.namelist()
                                    if name.casefold().endswith(
                                        (".png", ".jpg", ".jpeg", ".svg", ".webp")
                                    )
                                ]
                            )
                else:
                    text = markdown_path.read_text(encoding="utf-8")
                cover_text = text
                links = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text)
                image_count = len(links) if format_name == "md" else image_count
                for link in links:
                    parsed = urlparse(link)
                    decoded = unquote(parsed.path)
                    if parsed.scheme or Path(decoded).is_absolute():
                        add(
                            DeliveryIssueCategory.VALIDATION_ERROR,
                            "Markdown image link is not portable",
                        )
                    elif format_name == "md_bundle":
                        if decoded not in bundle_names:
                            add(
                                DeliveryIssueCategory.MISSING_ASSET,
                                f"bundle image target is missing: {decoded}",
                            )
                    elif not (path.parent / decoded).is_file():
                        add(
                            DeliveryIssueCategory.MISSING_ASSET,
                            f"Markdown image target is missing: {decoded}",
                        )
            elif format_name == "docx":
                with ZipFile(path) as archive:
                    names = set(archive.namelist())
                    if "word/document.xml" not in names:
                        add(DeliveryIssueCategory.STRUCTURE_ERROR, "DOCX body is missing")
                    else:
                        body_xml = archive.read("word/document.xml")
                        text = self._xml_text(body_xml)
                        cover_text = text
                    header_names = sorted(
                        name
                        for name in names
                        if name.startswith("word/header") and name.endswith(".xml")
                    )
                    footer_names = sorted(
                        name
                        for name in names
                        if name.startswith("word/footer") and name.endswith(".xml")
                    )
                    header_xml = [archive.read(name) for name in header_names]
                    footer_xml = [archive.read(name) for name in footer_names]
                    header_text = "\n".join(self._xml_text(value) for value in header_xml)
                    footer_text = "\n".join(self._xml_text(value) for value in footer_xml)
                    field_instructions = "\n".join(
                        value.decode("utf-8", errors="replace")
                        for value in [*header_xml, *footer_xml]
                    )
                    image_count = len([name for name in names if name.startswith("word/media/")])
            elif format_name == "pdf":
                reader = PdfReader(path)
                if not reader.pages:
                    add(DeliveryIssueCategory.STRUCTURE_ERROR, "PDF has no pages")
                text = "\n".join(page.extract_text() or "" for page in reader.pages)
                cover_text = (reader.pages[0].extract_text() or "") if reader.pages else ""
                body_pages = reader.pages[1:] if len(reader.pages) > 1 else reader.pages
                header_text = footer_text = "\n".join(
                    page.extract_text() or "" for page in body_pages
                )
                image_count = sum(len(page.images) for page in reader.pages)
                for page_number, page in enumerate(reader.pages, start=1):
                    width = float(page.mediabox.width)
                    height = float(page.mediabox.height)
                    if not (abs(width - 595.28) <= 8 and abs(height - 841.89) <= 8):
                        add(
                            DeliveryIssueCategory.LAYOUT_ERROR,
                            f"PDF page {page_number} is not A4 portrait",
                        )
                    if not (page.extract_text() or "").strip() and not page.images:
                        add(
                            DeliveryIssueCategory.LAYOUT_ERROR,
                            f"PDF page {page_number} is unexpectedly blank",
                        )
            else:
                add(DeliveryIssueCategory.STRUCTURE_ERROR, "unsupported rendered format")
            if required_image_count and image_count < required_image_count:
                add(
                    DeliveryIssueCategory.MISSING_ASSET,
                    f"rendered artifact has {image_count} images; expected "
                    f"at least {required_image_count}",
                )
            if any(marker in text for marker in self.PRIVATE_MARKERS):
                add(
                    DeliveryIssueCategory.VALIDATION_ERROR,
                    "rendered artifact contains a private placeholder",
                )
            # PDF text extraction discards block/style boundaries, so a Python code
            # comment such as ``# prepare coordinates`` is indistinguishable from a
            # Markdown heading.  Canonical DocumentIR/preflight already validates
            # heading structure before rendering; retain this derivative-only guard
            # for DOCX, where XML boundaries keep the check conservative.
            if format_name == "docx" and re.search(r"^#{1,6}\s", text, re.M):
                add(
                    DeliveryIssueCategory.VALIDATION_ERROR,
                    "raw Markdown headings leaked into the rendered document",
                )
            if document is not None and presentation_expectation is not None:
                # Native renderers and PDF text extractors are free to insert line
                # breaks or collapse spaces around dynamic PAGE fields.  Compare
                # semantic text after whitespace normalization so a wrapped cover
                # value (or ``第 1 页`` extracted as ``第1页``) is not rejected.
                normalized_cover = re.sub(r"\s+", "", cover_text)
                normalized_header = re.sub(r"\s+", "", header_text)
                normalized_footer = re.sub(r"\s+", "", footer_text)
                fields = {
                    item.semantic_key: item.value
                    for item in document.presentation.cover.fields
                    if item.visible
                }
                for key in presentation_expectation.required_cover_keys:
                    value = fields.get(key)
                    if value and re.sub(r"\s+", "", value) not in normalized_cover:
                        add(
                            DeliveryIssueCategory.VALIDATION_ERROR,
                            f"rendered cover is missing required field {key}",
                        )
                if format_name not in {"md", "md_bundle"}:
                    for value in presentation_expectation.expected_header_text:
                        if re.sub(r"\s+", "", value) not in normalized_header:
                            add(
                                DeliveryIssueCategory.VALIDATION_ERROR,
                                "rendered header is missing expected text",
                            )
                    for value in presentation_expectation.expected_footer_text:
                        if re.sub(r"\s+", "", value) not in normalized_footer:
                            add(
                                DeliveryIssueCategory.VALIDATION_ERROR,
                                "rendered footer is missing expected text",
                            )
                if format_name == "docx":
                    if (
                        presentation_expectation.require_page_number
                        and " PAGE " not in field_instructions
                    ):
                        add(
                            DeliveryIssueCategory.VALIDATION_ERROR,
                            "DOCX footer has no dynamic PAGE field",
                        )
                    if (
                        presentation_expectation.require_total_pages
                        and " NUMPAGES " not in field_instructions
                    ):
                        add(
                            DeliveryIssueCategory.VALIDATION_ERROR,
                            "DOCX footer has no dynamic NUMPAGES field",
                        )
        except (OSError, UnicodeError, BadZipFile, ValueError) as error:
            add(DeliveryIssueCategory.VALIDATION_ERROR, str(error))
        return DeliveryValidationResult(passed=not issues, issues=issues)

    @staticmethod
    def _xml_text(payload: bytes) -> str:
        root = ET.fromstring(payload)
        return "".join(root.itertext())
