Here's a concise, professional `AGENTS.md` document tailored to the `veya` project structure. It focuses on **agent architecture**, clarifies module responsibilities, identifies entry points, and reflects the observed design patterns — while gracefully handling noise (e.g., `Zone.Identifier`, `.pycache`, `.venv`, `.whl`, zips) as non-source artifacts.

---

# `AGENTS.md`: Agent Architecture Overview for `veya`

> **Version**: `veya` v0.2.0  
> **Purpose**: This document describes the agent-centric architecture of `veya` — a modular, extensible AI orchestration framework designed for code-aware reasoning, research, planning, and execution.

---

## 🧭 Project Layout Summary

The repository follows a clean, domain-driven package structure:

```
veya/
├── agents/            # Core agent implementations & base abstractions
├── server/            # HTTP/SSE API layer + agent routing & coordination
├── tools/             # Reusable, composable tool functions (e.g., search, file ops, LLM calls)
├── commands/          # CLI command definitions (e.g., `veya run`, `veya serve`)
├── hooks/             # Lifecycle event handlers (pre/post dispatch, auth, redaction, etc.)
├── registries/        # Dynamic registration systems for tools, models, skills, plugins
├── config/            # Configuration loading, schema validation, permission policies
├── session/           # Session state management (context, history, persistence)
├── streaming/         # Streaming utilities (SSE, async chunking, token-level response handling)
├── cli/               # Headless CLI entry point(s)
├── tests/             # Integration & smoke tests
├── docs/              # Documentation assets (e.g., instruction packs)
└── pyproject.toml   # Build, linting, and dependency configuration
```

*Note:* Files like `*.zip`, `*.whl`, `*.env`, `Zone.Identifier`, `.pycache/`, and `.venv/` are build artifacts, environment files, or system metadata — **not part of the source architecture**.

---

## 🤖 Key Agent Modules

### `agents/` — The Agent Core
This package defines the *what* and *how* of agent behavior.

| Module | Role | Notes |
|--------|------|-------|
| `agents/_base.py` | Abstract base class (`BaseAgent`) defining required interfaces: `run()`, `validate()`, `stream()`, and lifecycle hooks. Enforces uniform input/output contracts and error handling. | Foundation for all concrete agents. |
| `agents/plan.py` | **Planning Agent**: Decomposes high-level user requests into executable sub-tasks (e.g., “Build a Flask API that scrapes news” → [research frameworks, draft routes, write tests]). Uses LLM-guided decomposition + tool selection. | Primary entry for multi-step reasoning; often first in agent chains. |
| `agents/research.py` | **Research Agent**: Executes information-gathering workflows using web search, document parsing, and knowledge synthesis tools. Integrates with `tools/search.py`, `tools/parse.py`. | Stateful — maintains context across queries; supports iterative refinement. |
| `agents/build.py` | **Build/Execution Agent**: Translates plans into actionable code, runs validations (`run_validation.py`, `run_llm_validation.py`), and manages filesystem/codegen operations. | Tightly coupled with `tools/` and `session/` for safe, auditable code execution. |

✅ All agents are **registry-aware**: discovered and instantiated via `registries/skills.py` and `registries/agents.py` (implied by pattern — though not explicitly listed, `agents/__init__.py` exports them for auto-registration).

---

## ⚙️ Agent Entry Points

Agents are invoked through **three primary interfaces**, depending on usage mode:

### 1. **HTTP API (Production / Web UI)**
- **Entry**: `server/app.py` (FastAPI app)
- **Routing**: `server/routes/agent.py` handles `/api/v1/agent/{name}` POST requests.
- **Dispatch**: `server/coordinator.py` instantiates and orchestrates agents using:
  - `session` state (via `session/`)
  - `tools` (via `registries/tools.py`)
  - `models` (via `registries/models.py`)
- **Streaming**: Responses use Server-Sent Events (`server/sse.py`) for real-time token-by-token output.

### 2. **CLI / Headless Mode**
- **Entry**: `cli/headless.py` (primary CLI runner)
- **Usage**: `veya run --agent plan --input "..."`  
  (also supports `research`, `build`, and custom agent names)
- **Orchestration**: Uses `coordinator.py` (root-level) for local, synchronous agent execution — bypassing HTTP overhead.

### 3. **Programmatic Python API**
- **Entry**: Import and instantiate directly:
  ```python
  from agents.plan import PlanAgent
  from session import Session
  
  session = Session()
  agent = PlanAgent(session=session)
  result = agent.run({"query": "Design a data pipeline"})
  ```
- **Flexibility**: Enables embedding `veya` agents into other apps or notebooks.

---

## 🔗 Cross-Cutting Dependencies

| Component | Used By | Purpose |
|----------|---------|---------|
| `registries/` | All agents, `server/`, `hooks/` | Dynamic discovery & injection of tools, models, permissions, and plugins. Enables hot-swapping LLM backends or toolsets. |
| `hooks/` | `server/coordinator.py`, `agents/_base.py` | Intercepts agent lifecycle events (e.g., `pre_dispatch`, `post_result`, `permission`). Critical for security, auditing, and observability. |
| `config/permissions.py` | `hooks/builtin/permission.py`, `server/routes/auth.py` | Enforces fine-grained access control per agent/tool — e.g., `"build"` requires `code_write` scope. |
| `streaming/` | `agents/_base.py`, `server/sse.py` | Provides unified async streaming interface across agents and transport layers. |

---

## 🧪 Validation & Testing

- **Validation logic**: Externalized into `run_validation.py` (static checks) and `run_llm_validation.py` (LLM-based correctness scoring).
- **Smoke test**: `tests/test_smoke.py` verifies end-to-end agent chain (plan → research → build) with mocked tools.
- Agents are **unit-testable** via their `run()` interface — no HTTP or session coupling required.

---

## 📌 Key Design Principles

- **Modularity**: Agents, tools, and hooks are decoupled and discoverable via registries.
- **Observability-first**: Hooks and streaming enable full traceability of agent decisions and outputs.
- **Security-by-default**: Permission hooks gate sensitive actions (e.g., file writes, external API calls).
- **Multi-mode**: Same agent logic works headlessly, over HTTP, or embedded — no duplication.

## 🧭 Agent 开发规范（Cypress CLAUDE.md 吸收，2026-08）

> 来源: cypress-io/cypress CLAUDE.md 工程规范，适用于 veya 仓库内任何代码改动。

### 1. Plan Mode Default
- 任何非平凡任务（3+ 步或架构决策）先进入 plan：写清目标/改动面/验收标准再动手。
- 跑偏立即 STOP 重新计划，不要硬推。
- 验证步骤也走 plan（不只构建）。

### 2. Subagent Strategy
- 大量使用 subagent 保持主上下文干净。
- 研究/探索/并行分析下放 subagent，一个任务一个 subagent。

### 3. Self-Improvement Loop
- 用户/评审纠正后：更新最近的 AGENTS.md/相关文档，写防再犯规则。

### 4. Verification Before Done
- 未证明能工作就不算完成：跑相关测试、看日志、演示正确性。
- 自问：“staff engineer 会 approve 这个吗？”
- 对比 main 与改动的行为差异。

### 5. Demand Elegance（平衡）
- 非平凡改动先问“有没有更优雅的方式”。
- 修复若显 hacky：知道一切后再实现优雅方案。
- 简单/明显的修复跳过——不过度工程。

---

> ✅ **Next Steps for Contributors**  
> - Add new agents: Implement `BaseAgent`, register in `agents/__init__.py`, add to `registries/skills.py`.  
> - Extend tooling: Drop into `tools/`, import in `registries/tools.py`.  
> - Customize behavior: Write hooks in `hooks/builtin/` and register in `hooks/registry.py`.

--- 

*Generated from repo structure as of `veya` v0.2.0 • Last updated: 2024*
---

## 🚀 线上部署与运维（2026-08 实战沉淀，排查优先看这里）

### 线上拓扑（veya.aiinote.com）

```
浏览器 → Cloudflare(443) → SvelteKit 前端(adapter-node, 端口 3105)
      → VEYA_GATEWAY=127.0.0.1:8767 → docker backend 容器
        (uvicorn server.app:app = 根 app, 容器内 8765)
```

### 端口表（谁占着谁，冲突是"404 假象"头号元凶）

| 端口 | 服务 | 说明 |
|---|---|---|
| 3105 | veya 前端 (build/index.js) | systemd `veya-web` |
| 8767 | 后端网关通道 | **docker 映射 8767:8765**；若被 systemd `veya-gateway`(veya.server.app) 占用 → docker bind 失败，旧进程继续 404 |
| 9120 | legacy 通道 (VEYA_LEGACY) | 同一容器 9120:8765 |
| 8765 | 本机开发机 = **helivex api-gateway（外部项目）** | 开发时 curl 8765 打到的不是 veya！ |

### 铁律与坑（每条都是踩过的）

1. **Cindy 端点（scheduler/plugin/manage/knowledge/mcp/health/skills-inject）挂在根 app**：`server/routes/cindy_compat.py`（在线 Caddy 反代打的是根 app 不是 veya L4）。veya L4 (`veya/server/app.py`) 也有同套端点——两条入口能力面必须一致，新端点两头都要有。
2. **`server/` 已挂载进 docker 容器**（deploy/docker-compose.yml `../server:/app/server:ro`）→ 线上 `git pull` 后 `docker compose -f deploy/docker-compose.yml up -d backend` 即生效，**无需 build**；但旧镜像/旧部署要先停 systemd `veya-gateway` 否则 8767 bind 失败。
3. **前端 /api/v1/* 是 SvelteKit 代理**：`apps/web/src/routes/api/v1/[...path]/+server.ts` 转发到 `VEYA_GATEWAY`（默认 127.0.0.1:8765）；`apps/web/src/lib/upstreamProbe.ts` 在 404/502 时探活 `/api/v1/mcp/health` 并给中文引导。
4. **git pull "Already up to date" ≠ 部署生效**：修复必须 commit+push 且线上重启容器/服务；镜像内 COPY 的代码不随挂载更新。
5. 本机开发：`veya start` 端口自动避让（8765 被占自动 +1）；`veya doctor --json` 自检。

### 故障排查序列（5 分钟定位）

```bash
curl -s https://veya.aiinote.com/api/v1/mcp/health        # 探活: 非 veya 或 404 → 后端没起/被占
sudo ss -tlnp | grep -E "8767|8765"                        # 端口占用者是谁
systemctl status veya-gateway veya-web                     # systemd 侧
docker ps | grep veya-backend                              # docker 侧
# 验证端点矩阵: scheduler / plugin/manage / knowledge / agent/stream
```

详细版运维手册: `docs/ops/ONLINE_DEPLOYMENT.md`；工具链用户级安装（typst/xelatex/drawio/pdftoppm，免 root）: `docs/ops/TOOLCHAIN_SETUP.md`
