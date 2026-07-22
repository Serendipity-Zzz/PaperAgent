# ADR 0002：专业文档排版参考实现与许可证边界

## 状态

Accepted for P13 planning baseline on 2026-07-22.

## 背景

P13 需要解决标题与列表多重编号、中文字体和段落节奏单调、模板编号冲突、视觉验收不足和文档 Skill 未进入生产运行时的问题。产品所有者要求研究 `songhahaha66/PaperAgent` 的排版实现，并将可取设计用于本项目。

分析基于该仓库 master 快照 `4d64bd59d7a16d1126ce0935634e9802633925c4`。参考实现的重要行为包括：

- MainAgent 将 Word/Markdown 成文委派给 WriterAgent；
- 无模板 Word 使用 docx-js 原生对象、样式和 numbering；
- 有模板时保存原始模板，抽取模板契约并按结构填充；
- ReviewAgent 检查标题、表格、占位和媒体；
- DOCX 生成后执行 OOXML Schema 验证并在浏览器预览。

## 决策

PaperAgent 只吸收以下设计原则：

1. 内容创作与文档排版工具解耦；
2. 专业文档使用专门 Skill，Skill 规定流程、反模式和验收；
3. 标题、列表、图表和公式使用格式原生编号；
4. 上传模板先生成可执行契约，并保存不可变 source hash；
5. 结构验收先使用确定性解析，再按类别进入 Repair；
6. Skill 渐进式加载，确定性 OOXML/字体/页面操作由工具完成。

PaperAgent 不采用以下实现：

1. 不复制参考仓库 DOCX Skill 文本或源代码；
2. 不让 LLM 在生产环境生成并执行任意 docx-js/JavaScript；
3. 不采用 Arial、US Letter 或单一 Word 样式作为全局默认；
4. 不用纯文本锚点作为模板填充的唯一定位；
5. 不在缺图时自动另起“附图”页；
6. 不把 OOXML Schema 通过等同于视觉通过；
7. 不用参考项目尚未落地的 LaTeX 模式替换本项目 XeLaTeX Renderer。

## 许可证判断

参考仓库 DOCX Skill 的 front matter 标记为 `Proprietary`，并引用 `LICENSE.txt`；在分析时使用 GitHub Contents API 未在仓库根目录或该 Skill 目录找到对应许可证正文。本项目许可证为 `PolyForm-Noncommercial-1.0.0`，存在公开/分发可能，因此不得将该 Skill 或大段等价文本复制进仓库。

P13 的 `professional-document-layout` Skill、Numbering Contract、Template Contract V2、主题和 QA 由本项目原创实现。提交时仅保留本 ADR 中的来源说明和公开链接。

## 安全后果

- 生产 Renderer 保持受控 Python/OOXML/XeLaTeX 实现；
- Skill 不获得任意 shell、网络或项目外写入权限；
- 模板宏、外部 relationship 和嵌入脚本不会被执行；
- 字体安装和远程资源访问继续走用户审批；
- 第三方 fixture 不进入仓库，测试只使用合成文件。

## 参考

- <https://github.com/songhahaha66/PaperAgent/tree/4d64bd59d7a16d1126ce0935634e9802633925c4>
- <https://github.com/songhahaha66/PaperAgent/blob/4d64bd59d7a16d1126ce0935634e9802633925c4/backend/ai_system/docx_skill/SKILL.md>
- [P13 最终落地方案](../architecture/DOCUMENT_TYPOGRAPHY_NUMBERING_SKILL_PLAN.md)
