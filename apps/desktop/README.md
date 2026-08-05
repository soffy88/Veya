# Veya Desktop — Tauri 2.0 桌面壳

极致的 Layer 4 桌面工程：Svelte 5 Runes（`$state`/`$derived`/`$effect`）驱动 UI，无
Virtual DOM；通过 SSE/HTTP 直连 Veya Layer 4 Python 网关（3O 装配后端）。

UI 组件与 SSE 客户端 **100% 复用**自 monorepo 共享包 `@veya/ui`（`packages/ui`），
与网页端（`apps/web`）共用同一份 `DecisionTrailView.svelte` 与 `stream.ts`。

## 技术栈

| 层 | 选型 |
|----|------|
| 桌面壳 | Tauri 2.0（Rust，`src-tauri/`） |
| 构建 | Vite 6（`vite.config.ts`，集成 `@tailwindcss/vite`） |
| UI | Svelte 5（Runes 响应式）+ TypeScript（strict） |
| 样式 | Tailwind CSS v4（主题来自 `@veya/ui/theme.css`） |
| 图标 | lucide-svelte |
| 共享代码 | `@veya/ui`（workspace 链接） |

## 目录

```
src/
├── app.css                        # 仅 @import "@veya/ui/theme.css"
└── routes/
    └── +page.svelte               # 控制台壳（任务输入/运行/停止/清空）
```

共享组件与客户端位于 `packages/ui/src/`（`DecisionTrailView.svelte`、
`stream.ts`、`index.ts`、`theme.css`）。

## 开发

```bash
# 1) 启动 Veya Layer 4 网关（默认 127.0.0.1:8765）
cd ../../ && ./venv/bin/python -m veya.server.app
#    若 8765 被占用：./venv/bin/python -m veya.server.app --port 8767

# 2) 桌面开发（桌面壳 + Live Reload，依赖 Rust + webkit2gtk-4.1）
pnpm tauri dev
#    或仅前端（无 Rust 环境时）：
VITE_VEYA_ENDPOINT="http://127.0.0.1:8767/api/v1/agent/stream" pnpm dev
```

`VITE_VEYA_ENDPOINT` 可覆盖网关地址（生产默认
`http://127.0.0.1:8765/api/v1/agent/stream`）。

## 校验

```bash
pnpm check    # svelte-check：0 errors / 0 warnings
pnpm build    # adapter-static 产物 → build/
```

## 环境说明（本沙箱）

本机缺少 Rust 工具链（rustc/cargo）与 `webkit2gtk-4.1`/`rsvg2` 系统库，且 8765 端口
被其他服务占用，故完整 `pnpm tauri dev`（原生窗口编译）无法在本沙箱执行；前端全链路
（Vite Live Reload、SSE 决策轨迹渲染、run/stop/clear 交互）已通过 headless Chromium
端到端验证。在具备上述依赖的开发机上运行 `pnpm tauri dev` 即可打开原生桌面窗口。
