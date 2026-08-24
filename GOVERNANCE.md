# Governance

## 现状

Veya 目前由单一维护者 [@soffy88](https://github.com/soffy88) 主导开发和决策
（BDFL 模式）。这是项目当前所处阶段（`docs/VEYA_10_OF_10_PLAN.md` §21，0.x
Consolidation/Hardening 阶段）的真实状态，不是理想化的治理蓝图——等项目进入
public beta（§21 0.9）、有稳定的外部贡献者群体后，这份文档会更新为更正式的
决策流程（如 maintainer team、RFC 投票机制）。

## 决策方式

- **日常变更**（bug 修复、文档、小功能）：直接走 PR review，维护者合并。
- **架构级变更**（新增/替换主链组件、破坏兼容性的改动）：先在
  `docs/dev/rfc-*.md` 写清楚现状调研 + 决策依据，参考 `docs/dev/rfc-01` 到
  `rfc-07` 的既有格式，维护者拍板后再动手实现。
- **架构红线**：`docs/ARCHITECTURE_STABLE.md`、`architecture/manifest.yaml`
  记录的既定原则（单一聊天主链、3O 单一来源等，见 `CONTRIBUTING.md`）不接受
  绕过型 PR——不同意某条原则的话，先开 Issue 讨论，不要直接提交违反它的代码。

## 贡献者路径

任何人都可以通过 PR 贡献代码；被合并的贡献不自动获得 commit 权限或决策权——
这是单一维护者阶段的正常状态，不代表贡献不被重视。

## 联系方式

治理相关的问题走 GitHub Issues；行为准则问题见
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)；安全问题见 [SECURITY.md](SECURITY.md)。
