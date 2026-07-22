from __future__ import annotations

from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import pytest
from docx import Document
from PIL import Image

from paperagent.agents.document_ir import (
    BlockKind,
    DocumentBlock,
    DocumentIR,
    DocumentSection,
    EquationSpec,
    FigureSpec,
    Provenance,
    TableCell,
    TableRow,
    TableSpec,
)
from paperagent.artifacts import ArtifactService
from paperagent.core.config import Settings
from paperagent.db import DatabaseManager
from paperagent.db.models import ArtifactRecord
from paperagent.execution.tool_suite import ExecutionToolSuite
from paperagent.rendering.asset_assembly import (
    AssetAssembler,
    AssetAssemblyError,
    AssetBarrier,
    FigurePlacement,
    ImageLayoutSolver,
    ImageMetrics,
    ImageSizePolicy,
    TableLayoutSolver,
)
from paperagent.rendering.asset_binding import (
    ArtifactBinder,
    AssetBarrierCheckpointStore,
)
from paperagent.rendering.citations import (
    BibliographicItem,
    CitationStyle,
    CitationStyleService,
)
from paperagent.rendering.equations import EquationService
from paperagent.rendering.layout import PageSpec


def _service(tmp_path: Path) -> tuple[ArtifactService, Path]:
    settings = Settings(
        project_root=tmp_path / "repo",
        data_dir=tmp_path / "data",
        environment="test",
    )
    databases = DatabaseManager(settings)
    databases.initialize_global()
    project_id = str(uuid4())
    root = databases.project_root(project_id)
    root.mkdir(parents=True)
    databases.project_engine(project_id).dispose()
    return ArtifactService(databases, project_id), root


def _svg(root: Path) -> Path:
    path = root / "artifacts" / "source" / "standing-wave.svg"
    path.parent.mkdir(parents=True)
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="120" '
        'viewBox="0 0 320 120"><path d="M0 60 C80 0 160 120 320 60"/></svg>',
        encoding="utf-8",
    )
    return path


def _document(artifact_id: str) -> DocumentIR:
    return DocumentIR(
        requirement_id=uuid4(),
        requirement_version=1,
        outline_id=uuid4(),
        title="Asset assembly",
        language="mixed",
        sections=[
            DocumentSection(
                title="Results",
                goal="Bind a verified figure",
                blocks=[
                    DocumentBlock(
                        kind=BlockKind.FIGURE,
                        caption="Standing wave",
                        figure=FigureSpec(
                            artifact_id=artifact_id,
                            alt_text="Standing wave amplitude",
                        ),
                        provenance=Provenance(agent="test"),
                    )
                ],
            )
        ],
    )


def test_svg_is_verified_and_assembled_for_all_renderers_with_cache(tmp_path: Path) -> None:
    service, root = _service(tmp_path)
    source = service.register(
        _svg(root),
        kind="figure",
        producer_tool="result.collect",
        run_id="run-asset",
    )
    barrier = AssetBarrier(service).evaluate([source.id])
    assert barrier.ready and barrier.repair_code is None

    assembler = AssetAssembler(service)
    first = assembler.assemble(
        _document(source.id),
        target_formats=["markdown", "docx", "xelatex"],
    )
    second = assembler.assemble(
        _document(source.id),
        target_formats=["markdown", "docx", "xelatex"],
    )
    assert len(first.derivatives) == 3
    assert {item.target_format for item in first.derivatives} == {
        "markdown",
        "docx",
        "xelatex",
    }
    assert [item.artifact_id for item in first.derivatives] == [
        item.artifact_id for item in second.derivatives
    ]
    for derivative in first.derivatives:
        path = root / derivative.relative_path
        assert path.is_file() and path.stat().st_size > 0
    assert (
        next(item for item in first.derivatives if item.target_format == "docx").media_type
        == "image/png"
    )
    assert (
        next(item for item in first.derivatives if item.target_format == "xelatex").media_type
        == "application/pdf"
    )
    figure = first.document.sections[0].blocks[0].figure
    assert figure is not None
    assert set(figure.derivative_artifact_ids) == {"markdown", "docx", "xelatex"}
    assert figure.sha256 == source.sha256


def test_document_tool_waits_for_figure_derivatives_and_embeds_docx_media(
    tmp_path: Path,
) -> None:
    service, root = _service(tmp_path)
    source = service.register(
        _svg(root),
        kind="figure",
        producer_tool="result.collect",
        run_id="run-document-asset",
    )
    document = _document(source.id)
    suite = ExecutionToolSuite(
        data_root=tmp_path / "data",
        project_root=root,
        run_id="run-document-asset",
        uv_path=None,
        artifact_service=service,
    )
    try:
        suite.document_pipeline.store.save(document, source_run_id="run-document-asset")
        markdown = suite.document_render(
            {
                "document_id": str(document.document_id),
                "revision": document.revision,
                "format": "md",
                "filename": "assembled.md",
            }
        )
        markdown_bundle = suite.document_render(
            {
                "document_id": str(document.document_id),
                "revision": document.revision,
                "format": "md_bundle",
                "filename": "assembled.zip",
            }
        )
        docx = suite.document_render(
            {
                "document_id": str(document.document_id),
                "revision": document.revision,
                "format": "docx",
                "filename": "assembled.docx",
            }
        )
    finally:
        suite.close()
    assert (root / str(markdown["relative_path"])).is_file()
    bundle_path = root / str(markdown_bundle["relative_path"])
    with ZipFile(bundle_path) as bundle:
        names = set(bundle.namelist())
    assert "report.md" in names
    assert any(name.startswith("assets/") for name in names)
    word = Document(root / str(docx["relative_path"]))
    assert len(word.inline_shapes) == 1
    output_records = [
        service.get(str(item["artifact_id"])) for item in (markdown, markdown_bundle, docx)
    ]
    assert len({item.revision_id for item in output_records}) == 1
    assert all(item.derived_from_artifact_id for item in output_records)


def test_missing_path_or_filename_cannot_replace_figure_artifact(tmp_path: Path) -> None:
    service, _root = _service(tmp_path)
    document = _document(str(uuid4()))
    with pytest.raises(AssetAssemblyError, match="required document assets"):
        AssetAssembler(service).assemble(document, target_formats=["docx"])
    document.sections[0].blocks[0].figure = FigureSpec(path="figure.png")
    with pytest.raises(AssetAssemblyError, match="required document assets"):
        AssetAssembler(service).assemble(document, target_formats=["docx"])


def test_artifact_binder_rejects_ambiguous_same_name_inside_source_run(
    tmp_path: Path,
) -> None:
    service, root = _service(tmp_path)
    run_id = "run-ambiguous"
    for folder, color in (("first", "red"), ("second", "blue")):
        path = root / "runs" / run_id / folder / "figure.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 64), color=color).save(path)
        service.register(
            path,
            kind="figure",
            producer_tool="result.collect",
            run_id=run_id,
        )
    source = _document(str(uuid4()))
    assert source.sections[0].blocks[0].figure is not None
    source.sections[0].blocks[0].figure.artifact_id = None
    source.sections[0].blocks[0].figure.path = "figure.png"
    result = ArtifactBinder(service).bind(source, source_run_id=run_id)
    assert not result.ready
    assert not result.missing
    assert len(result.ambiguous) == 1
    assert len(result.ambiguous[0].candidates) == 2


def test_artifact_binder_rejects_artifact_id_from_another_project(tmp_path: Path) -> None:
    first, first_root = _service(tmp_path / "first")
    second, _second_root = _service(tmp_path / "second")
    foreign = first.register(
        _svg(first_root),
        kind="figure",
        producer_tool="result.collect",
        run_id="foreign-run",
    )
    document = _document(foreign.id)
    result = ArtifactBinder(second).bind(document, source_run_id="foreign-run")
    assert not result.ready
    assert result.invalid == [str(document.sections[0].blocks[0].block_id)]
    assert not result.bindings


def test_asset_barrier_classifies_pending_missing_and_invalid(tmp_path: Path) -> None:
    service, root = _service(tmp_path)
    source = service.register(
        _svg(root),
        kind="figure",
        producer_tool="result.collect",
        run_id="run-barrier",
    )
    with service.databases.project_session(service.project_id) as session:
        row = session.get(ArtifactRecord, source.id)
        assert row is not None
        row.validation_status = "pending"
        session.commit()
    pending = AssetBarrier(service).evaluate([source.id])
    assert pending.repair_code == "ASSET_PENDING" and pending.pending == [source.id]

    with service.databases.project_session(service.project_id) as session:
        row = session.get(ArtifactRecord, source.id)
        assert row is not None
        row.validation_status = "invalid"
        session.commit()
    missing_id = str(uuid4())
    invalid = AssetBarrier(service).evaluate([source.id, missing_id])
    assert invalid.repair_code == "ASSET_INVALID"
    assert invalid.invalid == [source.id]
    assert invalid.missing == [missing_id]
    state = AssetBarrier(service).evaluate([str(uuid4())])
    assert not state.ready and state.repair_code == "ASSET_MISSING"


def test_pending_binding_is_checkpointed_without_becoming_invalid(tmp_path: Path) -> None:
    service, root = _service(tmp_path)
    source = service.register(
        _svg(root),
        kind="figure",
        producer_tool="result.collect",
        run_id="run-pending-binding",
    )
    with service.databases.project_session(service.project_id) as session:
        row = session.get(ArtifactRecord, source.id)
        assert row is not None
        row.validation_status = "pending"
        session.commit()
    document = _document(source.id)
    result = ArtifactBinder(service).bind(document, source_run_id="run-pending-binding")
    assert not result.ready
    assert result.pending == [str(document.sections[0].blocks[0].block_id)]
    assert not result.invalid

    checkpoints = AssetBarrierCheckpointStore(root)
    saved = checkpoints.save_pending(
        document_id=document.document_id,
        revision=document.revision,
        pending_logical_ids=result.pending,
        source_run_id="run-pending-binding",
        source_message_id=None,
    )
    restored = checkpoints.load(document.document_id, document.revision)
    assert restored == saved
    assert not restored.expired


def test_image_and_table_layout_preserve_ratio_and_page_width() -> None:
    page = PageSpec()
    layout = ImageLayoutSolver().solve(
        ImageMetrics(width_px=300, height_px=100, aspect_ratio=3),
        page,
        ImageSizePolicy(
            max_width_ratio=0.85,
            placement=FigurePlacement.INLINE_CENTER,
            allow_upscale=False,
            minimum_dpi=150,
        ),
    )
    assert layout.width_pt <= page.content_width_pt * 0.85
    assert layout.height_pt == pytest.approx(layout.width_pt / 3)
    assert not layout.upscale

    table = TableSpec(
        rows=[
            TableRow(cells=[TableCell(text="Short"), TableCell(text="Long heading")]),
            TableRow(cells=[TableCell(text="1"), TableCell(text="longer body value")]),
        ]
    )
    resolved = TableLayoutSolver().solve(table, page)
    assert sum(resolved.column_widths_pt) <= page.content_width_pt + 0.01
    assert resolved.column_widths_pt[1] > resolved.column_widths_pt[0]
    assert resolved.repeat_header


def test_equations_are_normalized_numbered_and_reject_unsafe_tex() -> None:
    equations = EquationService().resolve(
        [
            EquationSpec(latex="$$ E = mc^2 $$", number=True, label="energy"),
            EquationSpec(latex="$a+b$", display=False),
        ]
    )
    assert equations[0].latex == "E = mc^2" and equations[0].number == 1
    assert equations[1].latex == "a+b" and equations[1].number is None
    with pytest.raises(ValueError, match="unsafe"):
        EquationService().normalize(r"\input{secret.txt}")


@pytest.mark.parametrize("style", list(CitationStyle))
def test_citation_styles_preserve_identity_and_verification(style: CitationStyle) -> None:
    item = BibliographicItem(
        title="A synthetic paper",
        authors=["Ada Lovelace", "Alan Turing"],
        year=2026,
        container_title="PaperAgent Journal",
        doi="10.0000/synthetic",
        verified=True,
    )
    formatted = CitationStyleService().format(
        item,
        style,
        sequence=1,
        locator="p. 7",
    )
    assert formatted.citation_id == item.citation_id
    assert formatted.inline and formatted.bibliography
    assert formatted.verified
    assert "10.0000/synthetic" in formatted.bibliography
