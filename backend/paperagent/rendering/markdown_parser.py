from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.token import Token

from paperagent.agents.document_ir import (
    BlockKind,
    DocumentBlock,
    DocumentSection,
    EquationSpec,
    FigureSpec,
    InlineKind,
    InlineNode,
    ListItem,
    ListKind,
    ListSpec,
    Provenance,
    TableCell,
    TableRow,
    TableSpec,
)
from paperagent.rendering.math_markup import canonicalize_math_delimiters
from paperagent.schemas.numbering import NumberingNormalizer

_NUMBERING_NORMALIZER = NumberingNormalizer()


def parse_markdown_sections(
    source: str,
    *,
    title: str,
    agent: str,
) -> list[DocumentSection]:
    """Promote Markdown headings to semantic sections with renderer-owned numbering."""

    blocks = parse_markdown_blocks(source, agent=agent)
    filtered: list[DocumentBlock] = []
    title_key = re.sub(r"\s+", "", title).casefold()
    for block in blocks:
        if block.kind is not BlockKind.HEADING:
            filtered.append(block)
            continue
        heading_key = re.sub(r"\s+", "", block.text).casefold()
        level = int(str(block.data.get("level", 1)))
        if level == 1 and heading_key == title_key:
            continue
        block.text = strip_author_heading_number(block.text)
        filtered.append(block)

    heading_levels = [
        int(str(block.data.get("level", 1)))
        for block in filtered
        if block.kind is BlockKind.HEADING
    ]
    if not heading_levels:
        return [
            DocumentSection(
                title="正文",
                goal="canonical composition",
                blocks=filtered,
                level=1,
            )
        ]

    base_level = min(heading_levels)
    roots: list[DocumentSection] = []
    stack: list[tuple[int, DocumentSection]] = []
    lead: list[DocumentBlock] = []
    for block in filtered:
        if block.kind is not BlockKind.HEADING:
            if stack:
                stack[-1][1].blocks.append(block)
            else:
                lead.append(block)
            continue
        level = max(1, min(6, int(str(block.data.get("level", 1))) - base_level + 1))
        section = DocumentSection(
            title=block.text,
            goal=block.text,
            level=level,
        )
        while stack and stack[-1][0] >= level:
            stack.pop()
        if stack:
            stack[-1][1].children.append(section)
        else:
            roots.append(section)
        stack.append((level, section))
    if lead:
        roots.insert(
            0,
            DocumentSection(
                title="摘要",
                goal="lead content",
                blocks=lead,
                level=1,
            ),
        )
    return roots


def strip_author_heading_number(value: str) -> str:
    """Compatibility facade for the canonical fixed-point label normalizer."""

    return _NUMBERING_NORMALIZER.normalize(value, node_kind="heading").semantic


def parse_markdown_blocks(source: str, *, agent: str) -> list[DocumentBlock]:
    """Parse trusted Markdown text into semantic blocks without preserving a blob paragraph."""

    source = canonicalize_math_delimiters(source)
    source = re.sub(
        r"(!\[[^\]]*\]\()([^)\n]+)(\))",
        lambda match: match.group(1) + match.group(2).replace(" ", "%20") + match.group(3),
        source,
    )
    parser = MarkdownIt("commonmark").enable("table")
    tokens = parser.parse(source)
    blocks: list[DocumentBlock] = []
    index = 0
    provenance = Provenance(agent=agent)
    while index < len(tokens):
        token = tokens[index]
        if token.type == "heading_open" and index + 1 < len(tokens):
            inline = tokens[index + 1]
            blocks.append(
                DocumentBlock(
                    kind=BlockKind.HEADING,
                    text=inline.content,
                    inlines=_inline_nodes(inline.children or []),
                    data={"level": int(token.tag.removeprefix("h") or 1)},
                    provenance=provenance,
                )
            )
            index += 3
            continue
        if token.type == "paragraph_open" and index + 1 < len(tokens):
            inline = tokens[index + 1]
            images = [item for item in inline.children or [] if item.type == "image"]
            if images:
                paragraph_text = re.sub(
                    r"!\[[^\]]*\]\([^)\n]+\)", "", inline.content
                ).strip()
                paragraph_inlines = _inline_nodes(
                    item for item in inline.children or [] if item.type != "image"
                )
                if paragraph_text:
                    blocks.append(
                        DocumentBlock(
                            kind=BlockKind.PARAGRAPH,
                            text=paragraph_text,
                            inlines=paragraph_inlines,
                            provenance=provenance,
                        )
                    )
                for image in images:
                    image_alt = str(image.content or image.attrGet("alt") or "Figure")
                    image_source = image.attrGet("src")
                    blocks.append(
                        DocumentBlock(
                            kind=BlockKind.FIGURE,
                            caption=image_alt,
                            figure=FigureSpec(
                                path=str(image_source) if image_source is not None else None,
                                alt_text=image_alt,
                            ),
                            provenance=provenance,
                        )
                    )
            elif inline.content.strip().startswith("$$") and inline.content.strip().endswith("$$"):
                latex = inline.content.strip()[2:-2].strip()
                blocks.append(
                    DocumentBlock(
                        kind=BlockKind.EQUATION,
                        text=latex,
                        equation=EquationSpec(latex=latex),
                        provenance=provenance,
                    )
                )
            else:
                blocks.append(
                    DocumentBlock(
                        kind=BlockKind.PARAGRAPH,
                        text=inline.content,
                        inlines=_inline_nodes(inline.children or []),
                        provenance=provenance,
                    )
                )
            index += 3
            continue
        if token.type == "fence":
            blocks.append(
                DocumentBlock(
                    kind=BlockKind.CODE,
                    text=token.content.rstrip("\n"),
                    data={"language": token.info.strip()},
                    provenance=provenance,
                )
            )
            index += 1
            continue
        if token.type in {"bullet_list_open", "ordered_list_open"}:
            end = _matching_close(tokens, index, token.type.replace("open", "close"))
            items = _list_items(tokens[index + 1 : end])
            blocks.append(
                DocumentBlock(
                    kind=BlockKind.LIST,
                    text="\n".join(item.text for item in items),
                    list_spec=ListSpec(
                        kind=ListKind.ORDERED
                        if token.type == "ordered_list_open"
                        else ListKind.BULLET,
                        start=int(token.attrGet("start") or 1),
                        items=items,
                    ),
                    provenance=provenance,
                )
            )
            index = end + 1
            continue
        if token.type == "table_open":
            end = _matching_close(tokens, index, "table_close")
            rows = _table_rows(tokens[index + 1 : end])
            if rows:
                blocks.append(
                    DocumentBlock(
                        kind=BlockKind.TABLE,
                        table=TableSpec(rows=rows),
                        provenance=provenance,
                    )
                )
            index = end + 1
            continue
        if token.type == "blockquote_open":
            end = _matching_close(tokens, index, "blockquote_close")
            text = "\n".join(item.content for item in tokens[index:end] if item.type == "inline")
            blocks.append(DocumentBlock(kind=BlockKind.QUOTE, text=text, provenance=provenance))
            index = end + 1
            continue
        index += 1
    return blocks


def math_aware_inline_nodes(source: str, nodes: list[InlineNode]) -> list[InlineNode]:
    """Reparse legacy inline nodes when CommonMark consumed TeX delimiters.

    Documents produced before math canonicalization retain the correct raw block
    text but may contain inline nodes where ``\\(...\\)`` became plain parentheses.
    Renderers call this compatibility helper so those revisions remain repairable.
    """

    canonical = canonicalize_math_delimiters(source)
    if canonical == source:
        return nodes
    inline = MarkdownIt("commonmark").parseInline(canonical)
    if not inline:
        return nodes
    return _inline_nodes(inline[0].children or [])


def _inline_nodes(tokens: Iterable[Token]) -> list[InlineNode]:
    nodes: list[InlineNode] = []
    stack: list[tuple[InlineKind, list[InlineNode], dict[str, Any]]] = []

    def append(node: InlineNode) -> None:
        target = stack[-1][1] if stack else nodes
        if node.kind is InlineKind.TEXT and target and target[-1].kind is InlineKind.TEXT:
            target[-1].text += node.text
            return
        target.append(node)

    for token in tokens:
        opening = {
            "strong_open": InlineKind.STRONG,
            "em_open": InlineKind.EMPHASIS,
            "link_open": InlineKind.LINK,
        }.get(token.type)
        if opening is not None:
            stack.append((opening, [], {"href": token.attrGet("href")}))
            continue
        if token.type in {"strong_close", "em_close", "link_close"} and stack:
            kind, children, values = stack.pop()
            append(
                InlineNode(
                    kind=kind,
                    text="".join(item.text for item in children),
                    children=children,
                    **values,
                )
            )
            continue
        if token.type == "code_inline":
            append(InlineNode(kind=InlineKind.CODE, text=token.content))
        elif token.type in {"text", "softbreak", "hardbreak"}:
            append(
                InlineNode(
                    kind=InlineKind.TEXT,
                    text="\n" if token.type.endswith("break") else token.content,
                )
            )
    return nodes


def _matching_close(tokens: list[Token], start: int, close_type: str) -> int:
    level = tokens[start].level
    for index in range(start + 1, len(tokens)):
        if tokens[index].type == close_type and tokens[index].level == level:
            return index
    return len(tokens) - 1


def _list_items(tokens: list[Token]) -> list[ListItem]:
    items: list[ListItem] = []
    current: list[Token] = []
    depth = 0
    for token in tokens:
        if token.type == "list_item_open":
            if depth == 0:
                current = []
            depth += 1
        elif token.type == "list_item_close":
            depth -= 1
            if depth == 0:
                inline = next((item for item in current if item.type == "inline"), None)
                text = inline.content if inline else ""
                items.append(
                    ListItem(
                        text=text,
                        inlines=_inline_nodes(inline.children or []) if inline else [],
                    )
                )
        elif depth == 1:
            current.append(token)
    return items or [ListItem(text="")]


def _table_rows(tokens: list[Token]) -> list[TableRow]:
    rows: list[TableRow] = []
    cells: list[TableCell] = []
    cell_header = False
    for token in tokens:
        if token.type == "tr_open":
            cells = []
        elif token.type in {"th_open", "td_open"}:
            cell_header = token.type == "th_open"
        elif token.type == "inline":
            cells.append(
                TableCell(
                    text=token.content,
                    inlines=_inline_nodes(token.children or []),
                    header=cell_header,
                )
            )
        elif token.type == "tr_close" and cells:
            rows.append(TableRow(cells=cells))
    return rows
