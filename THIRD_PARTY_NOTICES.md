# Third-party notices

PaperAgent 本体使用 PolyForm Noncommercial 1.0.0。下列依赖保持各自许可证；本文件不改变其条款。

| Component | License / boundary | Use |
|---|---|---|
| FastAPI, Starlette, Uvicorn, Pydantic | MIT / BSD-3-Clause | 本地 API 与数据契约 |
| SQLAlchemy, Alembic | MIT | SQLite 数据层与迁移 |
| LangGraph | MIT | Agent 图编排与 checkpoint |
| React, Vite, Zustand | MIT | 本地浏览器前端 |
| python-docx, python-pptx, openpyxl | MIT | Office 文件处理 |
| PyMuPDF | AGPL-3.0 or commercial | 可选文档渲染依赖；发行前须按其适用许可复核 |
| LanceDB | Apache-2.0 | 可选本地向量索引 |
| Nature Skills (`nature-figure`, `nature-shared`) | Apache-2.0 | 固定上游 commit 的 Skill；完整 notice 见 `third_party/nature-skills/NOTICE.md` |
| PDFMathTranslate-next | AGPL-3.0 | 仅作为用户选择安装/启动的外部进程，不复制源码或二进制进核心包 |
| pdf2htmlEX | GPL-3.0 | 不进入核心发行包；仅作为独立外部工具候选 |
| TeX Live, Typst, Pandoc, Microsoft Word | 各自许可证 | 用户选择的外部渲染工具，不随核心包捆绑 |
| uv | Apache-2.0 / MIT | 外部环境管理器；可由用户单独安装 |

最终发行清单和机器可读依赖见 `docs/release/sbom.cdx.json`。PyMuPDF 的双许可证边界是正式公开分发前必须再次确认的发行事项；当前个人本地构建用于开发与验收。
