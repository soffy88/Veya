# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。未发布变更见 `Unreleased`。

## [Unreleased]

### 新增：视觉工具链 (3O 内化 dsh-vision-toolkit)

- **10 个 `vision_*` 工具入主脑工具面**（`VEYA_VISION_TOOLS=0` 可关）：glance / ground / detect / crop / trace / pixel_diff / long_screenshot_ocr / extract_foreground / dominant_colors / html_screenshot — 纯文本大模型获得“眼睛”
- **3O 分层内化**（算法源自 Anionex/agent-vision-toolkit, MIT）：L1 `veya/oprim/vision_ops.py` 原子操作（能量切分/像素差分/主色/连通分量报图/边界描摹/OCR 重叠合并）、L2 `veya/oskill/vision_toolkit.py` 组合管线（视觉 LLM 客户端 + glance/ground/detect/聊天结构化 OCR）、L3 `veya/omodul/vision_toolkit.py` 端到端特性（会话工件/并发闸/路径白名单/断点续跑）、L4 `server/vision_toolkit_tools.py` 装配注册
- **零配置视觉模型**：默认复用本地 frontier 桥 gpt-5.6-luna（免 key），`VEYA_VISION_BASE_URL/MODEL/API_KEY` 可切任意 OpenAI 兼容端点
- **工件全落 `~/.veya/vision-artifacts/<session>/`**：裁切 PNG/SVG/热力图/差分 JSON/OCR Markdown + manifest + 边界审计，路径越界拒绝
- **长截图 OCR 安全化**：行能量低内容带切分 → 逐块 OCR → 重叠行去重合并 → chat 模式结构化消息合并，支持 `resume` 断点续跑
- **主脑 SOP 增 `# VISION` 段**；主脑当“眼睛”、hicode 当“手”的配合模式见 `docs/vision-tools/vision-tools.md`

## [0.6.0] - 2026-08-05

### 产品化 (P0 — 能装、能跑)

- **`veya init` 首次运行向导**：接模型（OpenAI / Anthropic / DashScope / DeepSeek / Ollama 本地）→ 选工作目录 → 生成开箱配置（`~/.veya/config.json` + 工作区安全策略），支持全 flags 非交互（`--yes --provider --key --workspace`）
- **`veya start` 一键启动**：本地 HTTP + SSE 服务（:8765）+ 自动打开浏览器
- **`veya doctor` 环境自检**：版本 / Key / 本地模型 / 工作区 / 端口，可脚本化（`--json`）
- **一键安装脚本**：`install.sh`（curl | bash，Linux/macOS）+ `install.ps1`（Windows），pipx / venv 自动选择
- **本地模型免 Key**：`VEYA_LLM_ENDPOINT` 指向 localhost 时不再误走离线 stub（Ollama 开箱可用）
- **安全策略可配置化**：`hooks/builtin/security.py` 现在按 用户级 → 工作区级 → 仓库默认 顺序加载（`veya init` 产物生效）

### 仓库卫生

- 删除主分支根目录全部噪音：`*.log`、`tui_v2.zip`、`*Zone.Identifier`（Windows 元数据）
- 内部阶段报告 / 验证脚本 / 旧 TUI 归档至 `docs/dev/archive/`
- `.gitignore` 新增 `*.log / *.zip / *Zone.Identifier / .coverage` 等规则

### 文档

- `README.md` 重写为产品向（定位 / 3 步安装 / 模型接入 / 文档导航）
- `docs/quickstart.md` 重写为 5 分钟跑通第一个真实任务
- `docs/index.md` 落地页（下载 / 快速开始 / 模型 / 文档）
- `docs/roadmap.md` 产品化路线图（P0–P3）

### 版本

- `0.5.1 → 0.6.0`（pyproject / CLI --version / FastAPI title 同步）

## [0.5.1] - 2026-08-04

- fix(llm): network retry + master brain rounds/execution fixes
- fix(master): route frontend-supplied provider/model/config into master brain
- feat(llm): support `VEYA_LLM_ENDPOINT` env for default provider endpoint
- refactor: retire legacy engine layer — all endpoints on Agent OS master brain

[Unreleased]: https://github.com/soffy88/Veya/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/soffy88/Veya/compare/v0.5.1...v0.6.0
[0.5.1]: https://github.com/soffy88/Veya/releases/tag/v0.5.1
