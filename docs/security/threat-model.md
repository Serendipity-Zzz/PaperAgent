# PaperAgent 威胁模型与隐私回归基线

## 资产与边界

受保护资产包括 API Key、论文原稿、未公开资料、长期记忆、实验代码、最终产物和审计记录。信任边界为本地浏览器到 `127.0.0.1` API、Provider 外发、第三方 Skill/仓库、外部渲染器和实验子进程。前端输入、导入文档、网页/论文内容、Skill prompt 和模型返回一律视为不可信数据。

## 主要威胁与控制

| Threat | Control | Regression evidence |
|---|---|---|
| 本地跨站请求/未授权调用 | 随机 session token、限定 CORS、只绑定 loopback | API integration/security tests |
| Prompt Injection 覆盖用户需求 | 原始需求不可变、证据来源/置信度、附件无指令权 | `test_p5_requirement_injection.py` |
| HTML/SVG/Office 主动内容 | 隔离解析、Bleach、禁脚本/宏/外链执行 | `test_p4_active_content.py` |
| 路径穿越与越权写入 | resolve 后根目录约束、允许写路径、原子替换 | storage/preview/experiment tests |
| 恶意 Skill/代码仓库 | 固定 checksum、确定性规则、Defender/Semgrep/Bandit/OSV 适配、审批 | P7 security/E2E |
| Secret 泄漏 | 系统凭据存储、日志脱敏、仓库 secret scan | P2/P7/P8 security tests |
| 付费调用静默重复 | intent/request id/unknown、禁止自动 retry、人工决定 | P8 recovery/fault tests |
| 删除继承全局授权 | DELETE 独立审批且不自动恢复 | P8 security tests |
| 离线模式外发 | PrivacyPolicy 在 Provider 边界阻断网络 | `test_p8_privacy_recovery.py` |
| 备份损坏/版本不兼容 | SHA-256、SQLite quick_check、恢复演练、schema version | P8 backup tests |

## 结论

标准、隐私控制、完全离线三种模式均进入总回归。当前自动化结果无 blocking/high finding。残余风险：本机已被恶意软件控制时无法保护用户会话；外部 Word/TeX/第三方扫描器的自身漏洞不由 PaperAgent 消除；PyMuPDF 和 AGPL 外部组件在公开发行前须继续做许可证复核。
