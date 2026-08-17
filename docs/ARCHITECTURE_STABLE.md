# Veya 主链路冻结架构（用户确认版）

> **状态**: 用户确认稳定 (2026-08-09)，禁止未经同意改动。
> 任何主链路改动必须先向用户说明并获得同意。
> 本文记录"为什么"——防止未来把踩过的坑"优化"回去。

---

## 1. 一句话架构

**入口只有一个大模型**：用户消息 → 大模型（ReAct 循环 + 全量工具面）→
模型自主决定直答或调用哪个工具 → 模型收尾总结。

程序不做任何路由/判断/裁藏/预抓/代做。

```
用户输入 ──► [大模型 ReAct：全量工具，模型自主决策]
               ├─ 直接回答（设计/问答/写作…）
               ├─ fetch_url / browser_run（URL/网页）
               ├─ assemble_code_context（需要时代码地图 + 历史规则；非每轮预注入）
               ├─ hicode_run（长任务编码，模型自己决定调用）
               ├─ run_in_sandbox（运行验证）
               ├─ mcp_*（视频 hevi / 知识 stratum / 代码库 codebase）
               └─ vision_*（视觉取证 10 工具：glance/ground/detect/crop/trace/
                  pixel_diff/long_screenshot_ocr/extract_foreground/dominant_colors/
                  html_screenshot — 2026-08 用户批准新增, 3O 内化 dsh-vision-toolkit）
            └─► 模型收尾总结 → SSE → 前端
```

视觉工具面（`vision_*`，2026-08-16 用户明确要求加入）：纯文本主链的“眼睛”。
程序仍不做任何视觉路由/预判 — 是否看图、看哪张、问什么，全由模型自主决定；
视觉模型默认走本地 frontier 桥 gpt-5.6-luna（零配置），`VEYA_VISION_*` 可切。
详见 `docs/vision-tools/vision-tools.md`。

用户可切换 **计划模式**（只读）并对高影响工具点批准。这是用户握方向盘，不是关键词路由。
Graft 默认不预注入（`VEYA_GRAFT_CONTEXT=1` 才恢复每轮注入）；编码任务由模型调
`assemble_code_context`，或 `hicode_run` / `evolve_solution` 内部按需附带。

## 2. 已固化的设计决策（及踩过的坑）

### 2.1 零程序判断（最重要的决策）

**历史教训**：曾加过三层程序化判断，全部导致"不回复"或"乱调工具"：
- 关键词前置路由（reasonix/quick/frontier）→ 截胡，长文/URL 不回复
- 工具面分层 `_layer_tools`（关键词裁藏）→ 模型看不到需要的工具，或看到
  173 工具被带偏（设计任务去查行情）
- URL 预抓注入 → 程序替模型抓内容，且撑爆 free 池网关上下文
- reasonix 收尾兜底（`_is_code_execution_task`）→ 程序代做长任务，覆盖有意结果

**结论**：删干净。模型看到全部工具，自己判断。SOP 里的 TOOL DISCIPLINE
（设计/方案类任务零工具直答、工具失败不重试）是给模型的**行为规范**，
不是程序判断——模型自己决定听不听。

### 2.2 LLM 层 = opencode-go 直连（用户有 key）

- `provider=veya1.1`（前端默认）→ `veya/llm.py` 直接走 opencode-go：
  - endpoint: `https://opencode.ai/zen/go/v1`
  - key: `OPENCODE_API_KEY`（用户提供，容器已注入）
  - model 候选重试: `deepseek-v4-flash` → `kimi-k2.7-code`（2026-08-16 用户指示弃用 mimo）
- **禁止**重新引入 oskill `router.call_aliased`（quality-gate 升级、模型切换、
  并行分派）——它是空回复的诱因（实测裸 URL 直连 200 有内容，走路由器就空）。

### 2.3 空回复绝不静默（三层兜底，均为可靠性非判断）

1. **opencode 分支内**：候选全空 → 本地 `gpt-5.6-luna` 兜底
   （宿主桥 `192.168.16.1:10101`，Host 头重写放行；**裁剪为核心工具面**
   `_core_tool_schemas`——本地模型上下文小于云端，全量 173 工具 + 50KB
   system 会超限）。
2. **外环兜底**（`_aliased_llm_call` 末尾）：无论内部哪条路径漏出空 content
   且无 tool_calls → 最后再兜一次 gpt-5.6-luna。
3. **coordinator/SSE/前端**：空回复 → 可见中文提示 + 前端 error 态（可重试）。

### 2.4 前端交互（用户确认）

- 不显示"任务开始 / 思考…"过程徽章（master_start/master_round 不进轨迹）
- 只显示真实执行轨迹：`$ fetch_url` / `$ hicode_run` / `✗ 工具失败` / Hicode 进度

## 3. 关键部署配置（勿改）

| 配置 | 值 | 位置 |
|---|---|---|
| 默认 provider/model | `veya1.1`（= opencode-go 直连） | 前端 `settings.svelte.ts` / 容器 env |
| OPENCODE_API_KEY | 用户 key | 容器 env（deploy/.env） |
| VEYA_FRONTIER_ENDPOINT | `http://192.168.16.1:10101/v1`（容器内） | docker-compose env |
| gpt-5.6-luna 兜底 | 核心工具面（`_core_tool_schemas`） | `veya/llm.py` |
| hicode serve | `127.0.0.1:8768`（容器内, 独立 oservi） | hicode-entrypoint |
| server/ 挂载 | `../server:/app/server:ro` → 重启即生效 | docker-compose |
| 前端 | `pnpm build` + 重启 veya-web（systemd） | build/ 非挂载 |

## 4. 变更审批

**任何主链路改动 → 先向用户说明 → 获同意 → 才动手。**

- 禁止未获同意：改模型路由 / 工具面 / LLM 层 / 兜底逻辑 / 前端交互 / 默认模型
- 纯文档/测试（不改变行为）：可做，commit 后立即说明
- 线上故障：先恢复服务，其余改动仍需同意
