# PaperAgent file-first 数据与迁移

PaperAgent 把用户可迁移内容保存为 Markdown、JSON、JSONL 和原始文件。SQLite、FTS5、LanceDB 与 LangGraph checkpoint 是事务设施或可重建索引，不是用户内容的唯一副本；日常使用不要求安装数据库 GUI。

默认数据根由 `PAPERAGENT_DATA_DIR` 或设置页指定，不依赖固定盘符。全局记忆位于 `memory/`，项目会话与状态位于 `projects/<project-id>/`，运行环境与缓存分别位于 `runtimes/` 和 `models/`。

## 迁移

1. 退出 PaperAgent，确保没有正在执行的写入、实验或渲染任务。
2. 使用可迁移备份导出整个数据根或单个项目；可不包含 `indexes/`。
3. 在目标机器安装 PaperAgent，把归档恢复到一个空目录。
4. 在首次启动中选择该数据目录。系统校验 manifest 与 SHA-256，然后从文件真源重建 FTS/向量索引。
5. 原目录已存在文件时恢复会拒绝覆盖；先选择新目录，或由用户自行完成备份和清理。

## 记忆

`MEMORY.md` 是短索引，详细条目位于 topic Markdown。每条记忆包含稳定 ID、来源指针、状态、敏感级别和内容 hash。全局长期记忆必须由用户明确授权；凭据、API Key 和原始敏感资料禁止写入记忆。

旧版 SQLite 记忆仅通过只读迁移适配器读取。迁移采用幂等键，可安全重放；成功写入文件后才推进 extraction cursor。
