# Veya 产品化路线图

目标：达到「下载就能直接干活」的开箱即用水平（对标 Cindy）。
核心不是堆功能，而是把「框架」做成「产品」。

## P0 — 能装、能跑 ✅（0.6.0 已落地）

- [x] **正式 Release 流程**：`.github/workflows/release.yml`（tag → GitHub Release wheel/sdist + PyPI 可选上传）
- [x] **一键安装**：`install.sh`（curl | bash）/ `install.ps1`（Windows），pipx/venv 自动选择
- [x] **仓库卫生**：根目录噪音（日志/zip/Zone.Identifier/临时报告）已清理归档，`.gitignore` 兜底
- [x] **首次运行向导**：`veya init`（模型 → 工作目录 → 配置落盘 → 示例任务提示）
- [x] **环境自检**：`veya doctor`（版本/Key/本地模型/工作区/端口，`--json` 可脚本化）
- [x] **一键启动**：`veya start`（HTTP+SSE + 自动开浏览器）
- [x] **最小 Quickstart**：5 分钟跑通第一个真实任务（docs/quickstart.md）
- [x] **版本与 Changelog**：0.6.0 + CHANGELOG.md
- [x] **CI 冒烟**：安装 → 启动 → 派任务 → 拿结果（见 .github/workflows/ci.yml）

## P1 — 能干活

- [x] **默认模型接入**：`veya init` 一键写入常见 Key；Ollama 自动探测；无 Key 离线 stub 引导
- [x] **本地模型免 Key**：`VEYA_LLM_ENDPOINT` 指向 localhost 时不再误走 stub
- [x] **安全策略可配置**：用户级 → 工作区级 → 仓库默认（`veya init` 产物生效）
- [ ] **统一主界面**：桌面 App（Electron/Tauri）或打磨 Web UI——当前以 `veya start` + Web 为最小可行壳，桌面壳列入后续
- [ ] **跨入口统一会话**：CLI / Web / TUI 共享同一 Session/Memory/任务历史（部分已有：session + checkpoint + `--resume`）
- [ ] **任务中心视角**：用户说「帮我做 X」而非选 Agent——内部保留 3O 多 Agent，前端隐藏复杂度

## P2 — 能养成

- [ ] **跨会话持久记忆**：用户偏好、项目约定、历史纠正（基础：session/memory_bank + checkpoint）
- [ ] **可教 Skill**：教一次，自然语言触发（基础：registries/skills.py + 3O oskill）
- [ ] **任务可恢复**：中断后继续（基础：`--resume` + checkpoint，需产品化入口）
- [ ] **多 Agent 共享任务上下文与验收标准**

## P3 — 能长期用

- [ ] 权限体验打磨（危险操作确认粒度、策略模板市场）
- [ ] 稳定性与可观测（SSE 稳定性回归、错误引导、遥测）
- [ ] 用户文档与示例库（修 bug / 写功能 / 代码审查等可复制示例）
- [ ] 持续发布（版本节奏、升级路径、release notes 自动化）
- [ ] 桌面 App 安装包（Electron/Tauri，macOS/Windows/Linux）

## 落地顺序（最短路径）

| 阶段 | 目标 | 关键产出 | 状态 |
|---|---|---|---|
| P0 | 能装、能跑 | Release 安装包 / 一键脚本 + 清理仓库 + 最小 Quickstart | ✅ 0.6.0 |
| P1 | 能干活 | 默认模型接入 + 统一主界面 + 工作区绑定 + 基础工具默认开启 | 🔄 模型/工作区/工具已完成，主界面进行中 |
| P2 | 能养成 | 持久记忆 + Skill + 任务可恢复 | ⏳ |
| P3 | 能长期用 | 权限体验、稳定性、文档与示例完善、持续发布 | ⏳ |
