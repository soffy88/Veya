# veya

> **AI 编码 Agent，下载就能干活** —— 模块化 AI 编排框架：代码感知推理、研究、规划与执行。
> 3O 架构（obase/oprim/oskill/omodul）+ 沙箱执行 + 多入口（CLI / Web / TUI / VSCode）。
> 因果闭环控制基板见 [Veya Loop](veya_loop/)（独立包 `veya-loop`）。

---

## 快速开始（3 步）

### 1. 安装

```bash
# Linux / macOS — 一键安装
curl -fsSL https://raw.githubusercontent.com/soffy88/Veya/main/install.sh | bash

# Windows PowerShell
irm https://raw.githubusercontent.com/soffy88/Veya/main/install.ps1 | iex

# 或 pip（含 veya start 用到的本地服务 + 可视化端点）
pip install "veya[server]"

# 只用 CLI（veya / veya-headless），不跑本地服务，可以更轻量：
# pip install veya
```

### 2. 接入模型（30 秒向导）

```bash
veya init
```

选择 OpenAI / Anthropic / DashScope / DeepSeek，或本地 Ollama（自动探测）。
没有 Key 也可以先体验（离线 stub 模式）。

### 3. 直接派任务

```bash
veya start                        # 启动本地服务 + 浏览器（可选）
veya "帮我审查当前目录的代码"       # 交互式任务
veya-headless --agent plan --text "设计一个数据管道"   # 无头模式
```

---

## 模型接入

| 提供商 | 环境变量 | 说明 |
|---|---|---|
| OpenAI | `OPENAI_API_KEY` | 默认模型 `gpt-4o-mini` |
| Anthropic | `ANTHROPIC_API_KEY` | 默认模型 `claude-haiku-4-5-20251001` |
| DashScope | `DASHSCOPE_API_KEY` | 默认模型 `qwen-plus` |
| DeepSeek | `DEEPSEEK_API_KEY` | 默认模型 `deepseek-chat` |
| Ollama（本地） | — | `veya init` 自动探测 `localhost:11434`，免 Key |
| 任意 OpenAI 兼容端点 | `VEYA_LLM_ENDPOINT` | NIM / vLLM / 代理网关等 |

`veya init` 会把 Key 写入 `~/.veya/.env`（chmod 600）与工作区 `.env`。
没有 Key 时自动降级为离线 stub 响应（可先体验完整流程）。

## 入口一览

| 入口 | 命令 | 场景 |
|---|---|---|
| 交互式 | `veya` | TUI / readline 对话，默认 persona=build |
| 无头 | `veya-headless --agent plan --text "..."` | 脚本 / CI 单次任务 |
| 本地服务 | `veya start` | HTTP + SSE（:8765），Web UI 与 VSCode 扩展后端 |
| 轻量交互 | `veya-simple` | 最小依赖交互（含权限确认） |
| 向导 / 自检 | `veya init` / `veya doctor` | 首次配置 / 环境诊断 |

## 文档

- [5 分钟快速开始](docs/quickstart.md) — 从安装到完成第一个真实任务
- [落地页](docs/index.md) — 能力总览
- [产品路线图](docs/roadmap.md) — P0–P3
- [架构说明](docs/architecture.md) — 开发者文档
- [Veya Loop](veya_loop/README.md) — 因果闭环控制基板（独立包）

## 开发

```bash
git clone --recurse-submodules https://github.com/soffy88/Veya.git
cd Veya && python -m venv venv && ./venv/bin/pip install -e ".[dev]"
./venv/bin/python -m pytest tests/ -q
```

3O 主库（obase/oprim/oskill/omodul/oservi）为 git submodule 独立主数据包。
