# Veya Web — SvelteKit 网页前端

Veya Layer 4 的 Web 端 Agent 控制台（写代码测试工作台）。UI 组件与 SSE 客户端
**100% 复用**自 monorepo 共享包 `@veya/ui`（与 Tauri 桌面端同一份代码）。

## 架构：Caddy 同源反代

生产部署由 Caddy（`deploy/veya.caddy`）承担：

```
浏览器 ─► Caddy (veya.aiinote.com)
           ├─ /api/*        ─► L4 网关 :8765（veya.server.app）
           ├─ /api/stream*  ─► L4 网关 :8765（SSE 零缓冲）
           ├─ /mcp/* /im/* /mobile/* /ws ─► L4 网关 :8765
           └─ 默认          ─► SvelteKit Node :3105（本应用）
```

前端**全部走同源路径，不需要 CORS、不需要知道后端地址**：

| 调用 | 同源路径 | 生产 | dev（无 Caddy） |
|------|----------|------|-----------------|
| 网关能力（agent/voice/vision/browser/kanban/…） | `/api/v1/*` | Caddy → :8765 | SvelteKit 服务端 → `VEYA_GATEWAY` |
| 老服务能力（AST/语义搜索/多模态/集成/…） | `/legacy/*` | SvelteKit 服务端 → `VEYA_LEGACY` | 同上 |
| Agent SSE 对话 | `/api/stream` | Caddy → :8765 | SvelteKit 服务端 → `VEYA_GATEWAY` |

> 沙箱执行（写代码测试核心）在 **L4 网关**上：`POST /api/v1/sandbox/execute`，
> 只跑单后端也能用。老服务的 `tools/*`（文件读写/工具执行）走 `/legacy/*`，
> 老服务未启动时相应卡片会报错，不影响主流程。

环境变量（仅 dev / SvelteKit 服务端读取）：

```bash
VEYA_GATEWAY="http://127.0.0.1:8765"   # 默认 127.0.0.1:8765
VEYA_LEGACY="http://127.0.0.1:8010"    # 默认 127.0.0.1:8010
```

## 目录

```
src/
├── app.css                        # @import "@veya/ui/theme.css"
├── lib/
│   ├── api.ts                     # 客户端 fetch（gateway → /api/v1/*, legacy → /legacy/*）
│   ├── capabilities.ts            # 能力目录：全部后端端点（数据驱动）
│   └── components/
│       ├── Workspace.svelte       # 写代码测试：编辑器 + 沙箱执行 + pytest
│       ├── AgentChat.svelte       # Agent SSE 流式对话
│       ├── CapabilityRunner.svelte# 通用能力卡片（表单 → 运行 → 结果）
│       └── KanbanPanel.svelte     # 看板可视化
└── routes/
    ├── +page.svelte               # 控制台壳：侧边栏 + 7 分区
    ├── +layout.svelte
    ├── api/stream/+server.ts      # SSE 转发（dev）
    ├── api/v1/[...path]/+server.ts# /api/v1/* 转发（dev；生产由 Caddy 截获）
    └── legacy/[...path]/+server.ts# /legacy/* 转发（dev + 生产）
```

## 七个分区

1. 🛠️ **代码测试** — 编辑器 + 沙箱执行 + pytest 运行 + 文件读写 + 工具执行
2. 🤖 **Agent 引擎** — SSE 对话 + run/swarm/verify/diagnose/long_horizon/graph/
   deep-research/replay/evolve/memory/skill/scheduler/knowledge/plugin/team/setup…
3. 🔬 **代码智能** — AST 分析 / 语义搜索 / 跨语言翻译 / 缓存 / 3D 图谱（老服务）
4. 🖼️ **多模态** — 图像分析 / OCR / 文档分析 / STT / TTS / 视觉
5. 🌐 **外部世界** — 浏览器自动化 / 外部 agent 孵化 / 通知 / GitHub / Slack / 账号绑定
6. 📋 **项目协作** — 看板 / 收件箱 / 模板 / 项目 / 会话 / 研究
7. ⚙️ **系统安全** — 模型 / 密钥 / 审计 / 权限 / MCP / 工具状态

## 开发

```bash
# 1) L4 网关（含沙箱端点；8765 被占用时换端口）
cd ../../ && ./venv/bin/python -m veya.server.app --port 8767

# 2) （可选）老服务——AST/语义搜索/多模态/集成等
./venv/bin/python -m uvicorn server.app:app --port 8011

# 3) dev（两个上游可用环境变量覆盖）
VEYA_GATEWAY="http://127.0.0.1:8767" VEYA_LEGACY="http://127.0.0.1:8011" pnpm dev

# 4) 生产构建 + 运行（放 Caddy 后面，端口 3105）
pnpm build
VEYA_GATEWAY="http://127.0.0.1:8767" VEYA_LEGACY="http://127.0.0.1:8011" PORT=3105 node build/index.js
```

## 校验

```bash
pnpm check    # svelte-check：0 errors / 0 warnings
pnpm build    # adapter-node 产物 → build/
```
