# ruff: noqa: RUF001 - Chinese full-width punctuation is part of the input grammar.

from __future__ import annotations

import re
from uuid import UUID

from paperagent.engine.budgets import BudgetLimits
from paperagent.orchestration.plan_models import (
    ApprovalRequirement,
    CandidateEdge,
    CandidateNode,
    CandidatePlan,
)
from paperagent.rendering.delivery import (
    DocumentAction,
    DocumentActionIntent,
    DocumentFormat,
)

_FORMAT_PATTERNS: tuple[tuple[DocumentFormat, tuple[str, ...]], ...] = (
    (DocumentFormat.PDF, (r"\bpdf\b", r"打印版|可打印")),
    (DocumentFormat.DOCX, (r"\bdocx\b|\bword\b", r"word\s*版")),
    (
        DocumentFormat.MARKDOWN_BUNDLE,
        (r"markdown\s*(?:bundle|压缩包)|md\s*(?:bundle|压缩包)",),
    ),
    (DocumentFormat.MARKDOWN, (r"\bmarkdown\b|\.md\b|\bmd\b",)),
)


def _matches(text: str, *patterns: str) -> bool:
    return any(re.search(pattern, text, re.I) for pattern in patterns)


class DocumentIntentClassifier:
    """Provider-independent fallback and invariant layer for document actions."""

    def classify(self, request: str) -> DocumentActionIntent:
        folded = request.casefold().strip()
        formats: list[DocumentFormat] = []
        for format_name, patterns in _FORMAT_PATTERNS:
            if _matches(folded, *patterns):
                formats.append(format_name)
        formats = list(dict.fromkeys(formats))

        recompute_denied = _matches(
            folded,
            r"(?:不要|无需|不必|禁止|别).{0,10}(?:重新|再)(?:运行|执行|生成|计算|绘制|跑)",
            r"(?:do not|don't|without).{0,24}(?:re-?run|run again|recompute|regenerate)",
        )
        explicit_recompute = _matches(
            folded,
            r"重新(?:运行|执行|生成|计算|绘制)|再(?:跑|运行|执行)一次|更新(?:实验|数据)",
            r"re-?run|run again|recompute|regenerate",
        ) and not recompute_denied
        restyle = _matches(
            folded,
            r"字体|字号|行距|页边距|排版|重新排版|图片.{0,6}(?:大小|尺寸|位置)",
            r"font|typography|restyle|reformat|margin|line spacing",
        )
        creation = _matches(
            folded,
            r"(?:写|撰写|制作|创建|生成|编写).{0,12}(?:论文|报告|文档|方案|纪要|教程|公文)",
            r"(?:write|draft|create|compose|generate).{0,24}(?:paper|report|document|proposal)",
        )
        conversion = bool(formats) and _matches(
            folded,
            r"转换|转成|转为|导出|输出为|整理成|下载|给我.{0,8}(?:版|格式)|可打印|打印版",
            r"convert|export|save as|download|printable|give me .{0,12}(?:version|format)",
        )
        presentation_change = _matches(
            folded,
            r"(?:封面|首页).{0,20}(?:姓名|作者|学号|班级|学校|院系|专业|课程|导师|指导老师)",
            r"(?:页眉|页脚|页码).{0,20}(?:改成|改为|设置|删除|移除|不要|[:：])",
            r"(?:姓名|作者|学号|班级|学校|院系|专业|课程|导师|指导老师).{0,10}(?:改成|改为|设置为|删除|移除)",
            r"cover|header|footer|page number",
        )
        existing_revision_reference = _matches(
            folded,
            r"基于(?:刚才|之前|已有|已生成|上一版|原)(?:.{0,12})(?:报告|论文|文档|文件)",
            r"只修改(?:文档)?呈现|不要重写正文|不改正文",
            r"based on (?:the )?(?:previous|existing|generated) (?:report|paper|document)",
        )
        content_scope = re.sub(
            r"(?:不要|无需|禁止|不需要)\s*"
            r"(?:重写|修改|改写|补充|新增|删除)(?:正文|内容)?",
            "",
            folded,
            flags=re.I,
        )
        content_change = _matches(
            content_scope,
            r"正文(?:内容)?\s*(?:新增|增加|删除|移除|补充|修改|改写|重写)",
            r"内容(?:修改|改写|补充|重写)|重写(?:正文|内容)|"
            r"新增.{0,6}(?:章节|段落)",
            r"rewrite|revise content|add (?:a )?(?:section|paragraph)",
        )

        if explicit_recompute:
            action = DocumentAction.RERUN_EXPERIMENT
        elif presentation_change and content_change and not creation:
            action = DocumentAction.REVISE_CONTENT
        elif presentation_change and (not creation or existing_revision_reference):
            action = DocumentAction.REVISE_PRESENTATION
        elif restyle:
            action = DocumentAction.RESTYLE
        elif conversion and not creation:
            action = DocumentAction.CONVERT_FORMAT
        elif creation:
            action = DocumentAction.CREATE
        elif _matches(folded, r"下载|给我文件", r"download|give me the file"):
            action = DocumentAction.DOWNLOAD
        else:
            action = DocumentAction.INSPECT

        evidence = [f"deterministic:{action.value}"]
        if formats:
            evidence.append("formats:" + ",".join(item.value for item in formats))
        if recompute_denied:
            evidence.append("rerun:denied")
        return DocumentActionIntent(
            action=action,
            target_formats=formats,
            target_reference=request if action is not DocumentAction.CREATE else None,
            preserve_content=action is not DocumentAction.REVISE_CONTENT,
            preserve_assets=not explicit_recompute,
            rerun_experiment=explicit_recompute,
            confidence=0.9 if action is not DocumentAction.INSPECT else 0.6,
            evidence=evidence,
        )


class DocumentDeliverySubgraph:
    """Build the invariant delivery graph for an existing canonical revision."""

    REQUIRED_TOOLS = (
        "document.resolve_revision",
        "document.bind_assets",
        "document.render",
        "document.validate_delivery",
    )

    def build_plan(
        self,
        intent: DocumentActionIntent,
        *,
        requirement_id: UUID,
        available_tools: set[str],
    ) -> CandidatePlan:
        if intent.action is not DocumentAction.CONVERT_FORMAT:
            raise ValueError("DocumentDeliverySubgraph requires a format conversion intent")
        missing = [name for name in self.REQUIRED_TOOLS if name not in available_tools]
        if missing:
            raise ValueError(
                "document delivery capability is unavailable: " + ", ".join(missing)
            )
        format_names = [item.value for item in intent.target_formats]
        formats = ", ".join(format_names)
        nodes = [
            CandidateNode(
                node_id="document_resolve_revision",
                agent_type="render_agent",
                objective=(
                    "Resolve the existing canonical document revision referenced by the "
                    "user. Do not compose or rewrite content."
                ),
                output_keys=["document_revision"],
                required_tools=["document.resolve_revision"],
                success_criteria=[
                    "one canonical revision is resolved or user confirmation is requested"
                ],
            ),
            CandidateNode(
                node_id="document_asset_barrier",
                agent_type="render_agent",
                objective=(
                    "Verify every required figure in the resolved canonical revision before "
                    "rendering. Preserve the existing asset set."
                ),
                input_refs=["document_revision"],
                output_keys=["asset_barrier_result"],
                required_tools=["document.bind_assets"],
                approval=ApprovalRequirement(
                    action="bind_document_assets",
                    risk="creates an immutable repaired revision when legacy assets need binding",
                    consequence="source artifacts and prior revisions remain immutable",
                ),
                success_criteria=["every required figure is bound to a verified artifact"],
            ),
            CandidateNode(
                node_id="document_render",
                agent_type="render_agent",
                objective=(
                    f"Render the resolved canonical revision to {formats}. Preserve content, "
                    "structure and assets and do not run experiments."
                ),
                input_refs=["document_revision", "asset_barrier_result"],
                output_keys=["rendered_artifacts"],
                required_tools=["document.render"],
                approval=ApprovalRequirement(
                    action="render_document_artifacts",
                    risk="writes requested output formats inside managed artifacts",
                    consequence="canonical revision and existing outputs remain immutable",
                ),
                success_criteria=["each requested format has a verified artifact reference"],
            ),
            CandidateNode(
                node_id="document_validate_delivery",
                agent_type="review_agent",
                objective=(
                    "Validate structure, embedded images, layout and canonical lineage for "
                    "every rendered artifact before delivery."
                ),
                input_refs=["rendered_artifacts"],
                output_keys=["delivery_validation"],
                required_tools=["document.validate_delivery"],
                success_criteria=["all requested artifacts pass machine-verifiable QA"],
            ),
        ]
        edges = [
            CandidateEdge(source=nodes[index].node_id, target=nodes[index + 1].node_id)
            for index in range(len(nodes) - 1)
        ]
        return CandidatePlan(
            requirement_id=requirement_id,
            requirement_version=1,
            entry_node=nodes[0].node_id,
            terminal_nodes={nodes[-1].node_id},
            nodes=nodes,
            edges=edges,
            limits=BudgetLimits(max_input_tokens=32_000, max_output_tokens=8_000),
            rationale=(
                "Canonical format conversion resolves an existing revision, verifies its "
                "assets, renders requested formats and validates delivery without rewriting."
            ),
            assumptions=[
                "document_action:convert_format",
                "preserve_content:true",
                "preserve_assets:true",
                "rerun_experiment:false",
                "target_formats:" + ",".join(format_names),
            ],
        )
