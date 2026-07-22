# ADR 0001：PaperAgent 系统边界

- 状态：Accepted
- 日期：2026-07-16
- 基线：PRD v1.0

## 决策

### LangGraph

Agent 编排采用 LangGraph。Agent 只承担需要非固定规则判断、工具路由或重新规划的职责；确定性转换保留为 Tool/Service。LangGraph 状态必须可序列化并可 checkpoint。

### SQLite 与 LanceDB

SQLite 保存全局和项目事务数据，启用 WAL、外键和单写队列。LanceDB 只保存向量索引。大文件进入项目文件存储，数据库只保存元数据、相对路径、哈希和版本。

### Document IR

写作、翻译、审验和渲染共享有版本的 Document IR。DOCX、Markdown、Typst、LaTeX 和 PDF 均从 IR 派生，禁止通过反复格式互转维护正文真源。

### Provider Adapter

LLM 与图片模型通过能力声明和统一错误模型接入。领域层不导入具体供应商 SDK；真实调用和 Mock Server 共用合约测试。

### Preview Artifact

预览服务以 PreviewArtifact、PreviewAnchor 和 Annotation 为契约。原始文件与预览缓存分离，未知格式降级到安全元数据视图，不执行宏、脚本或嵌入程序。

### Windows Launcher

Launcher 负责单实例、动态端口、本地 session token、健康检查、浏览器打开和系统托盘。后端仅绑定 `127.0.0.1`，前端写请求必须带本地会话凭据。

## 依赖方向

`schemas/domain -> services -> adapters -> api/launcher`

底层 schema 和领域模型不得反向依赖具体 Provider、UI 或外部工具实现。该约束由 `scripts/check_architecture.py` 检查。
