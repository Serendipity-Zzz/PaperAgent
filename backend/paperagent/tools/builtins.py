from __future__ import annotations

from paperagent.tools.contracts import (
    ConcurrencyPolicy,
    PermissionPolicy,
    SideEffect,
    ToolSpec,
)


def builtin_tool_specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="knowledge.search",
            version="1.0.0",
            description="Search project evidence with source and locator metadata.",
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "required": ["project_id", "query"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            capabilities={"retrieval", "evidence", "read"},
            search_hints=["论文 文献 证据 引用 检索 search literature evidence citation"],
            allowed_agents={"requirement_agent", "evidence_agent", "writer_agent", "review_agent"},
            side_effect=SideEffect.NONE,
            concurrency_policy=ConcurrencyPolicy.SAFE,
        ),
        ToolSpec(
            name="file.read",
            version="1.0.0",
            description="Read an approved project-relative text file range.",
            input_schema={
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string"},
                    "start": {"type": "integer", "minimum": 0},
                    "max_chars": {"type": "integer", "minimum": 1, "maximum": 120000},
                },
                "required": ["relative_path"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            capabilities={"filesystem", "read"},
            search_hints=["读取 文件 源码 日志 read file source log"],
            side_effect=SideEffect.NONE,
            concurrency_policy=ConcurrencyPolicy.SAFE,
        ),
        ToolSpec(
            name="artifact.lookup",
            version="1.0.0",
            description="Locate exact existing source, data, figure or document artifacts.",
            input_schema={
                "type": "object",
                "properties": {
                    "relation": {
                        "type": ["string", "null"],
                        "enum": [
                            "source",
                            "data",
                            "figure",
                            "log",
                            "output",
                            None,
                        ],
                    },
                    "run_id": {"type": ["string", "null"]},
                },
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            capabilities={"artifact", "lookup", "read"},
            search_hints=["刚才 源码 数据 文件 下载 previous source artifact"],
            allowed_agents={"artifact_agent", "requirement_agent", "supervisor"},
            side_effect=SideEffect.NONE,
            concurrency_policy=ConcurrencyPolicy.SAFE,
        ),
        ToolSpec(
            name="experiment.assess",
            version="1.0.0",
            description="Assess repository, Python, CUDA, RAM, VRAM and disk feasibility.",
            input_schema={
                "type": "object",
                "properties": {
                    "repository": {"type": "string"},
                    "gpu_name": {"type": ["string", "null"]},
                    "vram_gb": {"type": ["number", "null"]},
                    "disk_free_gb": {"type": ["number", "null"]},
                },
                "required": ["repository"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            capabilities={"experiment", "hardware", "repository", "read"},
            search_hints=["实验 CUDA GPU 环境 可行性 experiment hardware feasibility"],
            allowed_agents={"experiment_agent", "supervisor"},
            side_effect=SideEffect.NONE,
            concurrency_policy=ConcurrencyPolicy.SAFE,
            deferred=True,
        ),
        ToolSpec(
            name="document.render",
            version="1.0.0",
            description=(
                "Render approved Document IR to Markdown, a portable Markdown bundle, "
                "DOCX, Typst, LaTeX or PDF."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "document_ir": {"type": "object"},
                    "format": {"type": "string"},
                    "output_path": {"type": "string"},
                    "pdf_mode": {
                        "type": "string",
                        "enum": ["auto", "xelatex", "word_parity"],
                    },
                },
                "required": ["document_ir", "format", "output_path"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            capabilities={"document", "render", "local_write"},
            search_hints=["生成 PDF Word DOCX Markdown 排版 render document"],
            allowed_agents={"render_agent", "repair_planner"},
            side_effect=SideEffect.LOCAL_WRITE,
            concurrency_policy=ConcurrencyPolicy.EXCLUSIVE,
            permission_policy=PermissionPolicy.DETERMINISTIC,
            deferred=True,
        ),
        ToolSpec(
            name="visual.generate",
            version="1.0.0",
            description="Generate an approved non-data illustration using the configured provider.",
            input_schema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "width": {"type": "integer"},
                    "height": {"type": "integer"},
                    "provider": {"type": "string"},
                },
                "required": ["prompt", "provider"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            capabilities={"image", "external", "paid"},
            required_provider_capabilities={"tools", "vision"},
            search_hints=["生图 图片 插图 现场图 image illustration generate"],
            allowed_agents={"visual_agent"},
            side_effect=SideEffect.PAID,
            concurrency_policy=ConcurrencyPolicy.EXCLUSIVE,
            permission_policy=PermissionPolicy.REQUIRE_APPROVAL,
            deferred=True,
        ),
    ]
