from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from paperagent.agents.change_intent import ChangeScope
from paperagent.agents.document_ir import (
    BlockKind,
    DocumentBlock,
    DocumentIR,
    DocumentSection,
    FigureSpec,
    Provenance,
)
from paperagent.rendering.revision_store import DocumentRevisionStore
from paperagent.rendering.revision_workflow import (
    RevisionOperation,
    RevisionResolver,
    RevisionWorkflow,
    TargetKind,
    TargetResolver,
)


def document(title: str = "驻波报告", conversation: str = "chat-a") -> DocumentIR:
    return DocumentIR(
        requirement_id=uuid4(),
        requirement_version=1,
        outline_id=uuid4(),
        title=title,
        language="zh",
        metadata={"source_conversation_id": conversation},
        sections=[
            DocumentSection(
                title="项目背景",
                goal="背景",
                blocks=[
                    DocumentBlock(
                        kind=BlockKind.PARAGRAPH,
                        text="原始正文",
                        provenance=Provenance(agent="test"),
                    ),
                    DocumentBlock(
                        kind=BlockKind.FIGURE,
                        caption="图 1 驻波",
                        figure=FigureSpec(path="figure.png", width_ratio=0.8),
                        provenance=Provenance(agent="test"),
                    ),
                ],
            ),
            DocumentSection(
                title="方案设计",
                goal="设计",
                blocks=[
                    DocumentBlock(
                        kind=BlockKind.PARAGRAPH,
                        text="设计正文",
                        provenance=Provenance(agent="test"),
                    )
                ],
            ),
        ],
    )


def test_revision_resolution_supports_conversation_previous_and_project_ambiguity(
    tmp_path: Path,
) -> None:
    store = DocumentRevisionStore(tmp_path)
    first = document()
    store.save(first, source_conversation_id="chat-a")
    second = first.restyle(first.typography.model_copy(update={"body_font": "宋体"}))
    store.save(second, source_conversation_id="chat-a")
    store.save(document("另一份报告", "chat-b"), source_conversation_id="chat-b")
    resolver = RevisionResolver(store)

    previous = resolver.resolve("把上一版改一下", conversation_id="chat-a")
    assert previous.document is not None and previous.document.revision == 1
    ambiguous = resolver.resolve("重新排版")
    assert ambiguous.requires_confirmation and len(ambiguous.candidates) == 2
    latest = resolver.resolve("把刚才的报告改一下")
    assert latest.document is not None and latest.document.title == "另一份报告"


def test_target_resolver_uses_stable_ids_and_refuses_ambiguous_figures() -> None:
    source = document()
    resolver = TargetResolver()
    section = resolver.resolve(source, "只改第二章行距")
    assert section.targets[0].kind is TargetKind.SECTION
    assert section.targets[0].target_id == source.sections[1].section_id
    figure = resolver.resolve(source, "把第一张图缩小")
    assert figure.targets[0].target_id == source.sections[0].blocks[1].block_id
    duplicated = source.model_copy(deep=True)
    duplicated.sections[1].blocks.append(
        DocumentBlock(
            kind=BlockKind.FIGURE,
            caption="图 2",
            figure=FigureSpec(path="figure2.png"),
            provenance=Provenance(agent="test"),
        )
    )
    assert TargetResolver().resolve(duplicated, "把图缩小").requires_confirmation


def test_style_revision_preserves_semantic_hashes_and_rollback_is_new_revision(
    tmp_path: Path,
) -> None:
    store = DocumentRevisionStore(tmp_path)
    source = document()
    store.save(source, source_conversation_id="chat-a")
    result = RevisionWorkflow(store).apply(
        source,
        RevisionOperation(
            kind="typography",
            patch={
                "scope": ChangeScope.GLOBAL.value,
                "typography_patch": {"body_font": "宋体", "body_size_pt": 11},
            },
        ),
    )
    before, after = source.hashes(), result.document.hashes()
    assert before.content_hash == after.content_hash
    assert before.structure_hash == after.structure_hash
    assert before.asset_set_hash == after.asset_set_hash
    assert before.citation_set_hash == after.citation_set_hash
    assert before.style_hash != after.style_hash
    assert result.diff.style_changed and not result.diff.content_changed

    restored = store.rollback(source.document_id, 1)
    assert restored.revision == 3
    assert restored.hashes() == source.hashes()
    assert [item.revision for item in store.list_lineage(source.document_id)] == [1, 2, 3]


def test_local_figure_and_structure_revisions_only_change_targeted_nodes(tmp_path: Path) -> None:
    store = DocumentRevisionStore(tmp_path)
    source = document()
    store.save(source)
    workflow = RevisionWorkflow(store)
    figure_id = source.sections[0].blocks[1].block_id
    resized = workflow.resize_figure(source, figure_id, 0.45)
    assert resized.diff.changed_blocks == [figure_id]
    assert resized.diff.style_changed and not resized.diff.content_changed
    section_id = resized.document.sections[0].section_id
    broken = workflow.apply(
        resized.document,
        RevisionOperation(
            kind="insert_break", target_ids=[section_id], break_kind=BlockKind.PAGE_BREAK
        ),
    )
    assert broken.diff.structure_changed
