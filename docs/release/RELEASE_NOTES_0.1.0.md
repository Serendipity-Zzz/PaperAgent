# PaperAgent 0.1.0 发布说明

PaperAgent 0.1.0 是面向 Windows 个人本地部署的首个完整发布候选。它提供暗色三栏交互工作台、持久化项目/会话、多 Agent 动态闭环、实时任务事件、断点续接、影响分级 Steering、知识库、真实实验、审验修复、多格式成文，以及文本/图片 Provider 分域配置。

## 安装与启动

推荐双击 `PaperAgent-0.1.0-Setup.exe`。安装不要求管理员权限，完成后可从桌面或开始菜单启动；不需要每次打开命令行。便携使用者可解压 `PaperAgent-0.1.0-windows-x64.zip` 后运行 `PaperAgent.exe`。

默认用户数据位于 `%LOCALAPPDATA%\PaperAgent\data`。卸载程序默认只移除应用，保留项目、会话、记忆和凭据；删除数据必须由用户显式选择并承担后果。

## 首次运行

首次运行向导会检查磁盘、GPU、uv、Typst、Pandoc 和 TeX Live。基础文本功能不依赖全部可选工具；安装任何外部工具前都会展示计划、安装位置和影响，并等待用户确认。文本 Provider 与图片 Provider 分开配置，API Key 进入 Windows Credential Manager，不写入项目文件或日志。

## 升级与恢复

用户级安装脚本会在升级前快照应用和数据，执行迁移与 smoke；失败时恢复旧版本。发布包还包含回滚、状态、日志和卸载入口。运行中的 Agent 以持久化 Run/Event/checkpoint 为真源，重启后进入可审计恢复流程，不盲目重放有费用或副作用不明的调用。

## 校验

| 文件 | SHA-256 |
|---|---|
| `PaperAgent-0.1.0-Setup.exe` | `ebe73c255b69416356875d58c92c1dd8bc74fbf44806af0e6b326c72d0e7d410` |
| `PaperAgent-0.1.0-windows-x64.zip` | `cc23ab6213e6faeca07f0bc7f99d658cc35a10e49b196a2f7c859d9c89811c5a` |

## 已知限制

- 安装包尚未代码签名，Windows 可能出现 SmartScreen 提示；请核对 SHA-256。
- 当前验收机没有图片生成 Key，未执行真实付费生图；文本 MiMo Provider 已从打包程序真实调用通过。
- PyMuPDF 的 AGPL/商业许可边界需在公开分发前再次复核。
- 当前首版优先支持 Windows 11；其他系统不在 0.1.0 发布门禁内。

PaperAgent 本体采用 PolyForm Noncommercial 1.0.0，未经授权禁止第三方商业使用。第三方依赖继续适用各自许可证。

