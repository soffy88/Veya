# Security Policy

## 报告安全问题

**不要**通过公开 GitHub Issue 报告安全漏洞。

请使用 GitHub 的私密漏洞报告功能：打开
[Security → Advisories → Report a vulnerability](https://github.com/soffy88/Veya/security/advisories/new)
提交，报告内容只对仓库维护者可见，直到修复发布。

请尽量包含：

- 复现步骤 / PoC
- 受影响的版本
- 潜在影响评估（信息泄露 / 权限提升 / 远程执行 / 拒绝服务等）

## 支持的版本

Veya 目前处于 0.x 阶段（`docs/VEYA_10_OF_10_PLAN.md` §21 路线图），尚未建立正式的多版本
安全支持窗口——只有最新的 `main` 分支 / 最新发布版本会收到安全修复。1.0 发布后会在这里
更新明确的支持版本表。

## 已知的安全边界

- 代码执行走统一沙箱（`run_in_sandbox` 工具 / `veya.sandbox`），默认 profile 见
  `VEYA_SANDBOX_PROFILE`（`local` / `hosted`）。
- 敏感凭据不直接暴露给模型——委托金库机制（`system_secure_exec`），需要人工在 UI 批准。
- 更完整的安全模型（capability token、sandbox 权限分级、对抗性测试套件）是
  `docs/VEYA_10_OF_10_PLAN.md` §11 的在建目标，尚未全部落地。
