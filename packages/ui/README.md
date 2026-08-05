# @veya/ui — Veya Layer 4 共享 UI 套件

Svelte 5（Runes）决策轨迹组件 + POST-SSE 客户端，桌面端（Tauri）与网页端
（SvelteKit）**100% 无缝复用**的单一代码源。

## 导出

| 导出 | 说明 |
|------|------|
| `DecisionTrailView`（default + named） | Svelte 5 Runes 实时决策轨迹视图 |
| `streamAgentRun(task, opts)` | POST-SSE 客户端（fetch + ReadableStream） |
| `effectiveEndpoint()` / `DEFAULT_ENDPOINT` | 网关地址解析（`VITE_VEYA_ENDPOINT` 覆盖） |
| `SseEvent` / `StreamOptions`（type） | SSE 帧与选项类型 |
| `@veya/ui/theme.css` | Tailwind v4 深色终端主题（`@theme` + `@source`） |

## 组件 API（DecisionTrailView）

```svelte
<DecisionTrailView
  task="做一次简单的代码测试"
  bind:running
  mode="run"                        <!-- "run" | "dry_run" -->
  endpoint="/api/stream"            <!-- 默认 http://127.0.0.1:8765/api/v1/agent/stream -->
  onStatusChange={(s) => ...}       <!-- idle|connecting|streaming|done|error -->
  onSessionDone={(info) => ...}     <!-- { status, cost, sessionId } -->
/>
```

- `bind:this` 暴露 `reset()`；`bind:running` 双向控制启停（flip true 即发起 SSE，false 中止）
- 内部全部使用 Runes：`$state` 帧流/状态、`$derived` 成本统计、`$effect` 流生命周期

## 校验

```bash
pnpm check    # svelte-check：0 errors / 0 warnings
```
