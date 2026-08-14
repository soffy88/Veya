# Veya × 3O 装配图（Assembly Map）

> **Veya 是先有 3O 元素、后装配的项目。** 3O 主库（oprim / oskill / omodul /
> obase / oservi）作为独立 Git Submodule 挂在 `platform/3O/`（SPEC v3.0 §2.1：
> 5 个独立 package，各自独立 repo + SemVer）。本页定义 Veya 与主库的边界、
> 单源（§1.4）收敛状态与守卫测试。

## 1. 架构分层

```
┌─────────────────────────────────────────────────────────────┐
│ Veya 项目服务层 (SPEC §8 — 不入 3O，各项目自管)                │
│   server/ cli/ agents/ hooks/ config/ session/ registries/   │
│   commands/ streaming/ tools/ tui/ tui_v2/                   │
└──────────────┬──────────────────────────────────────────────┘
               │ 装配 (veya.platform — 唯一装配通道)
┌──────────────▼──────────────────────────────────────────────┐
│ 3O 主库 (platform/3O/ — submodules, 只读装配, 不在 Veya 内实现) │
│   obase(0.31.x)  oprim(3.2x)  oskill(4.x)  omodul(1.x)      │
│   oservi(1.x)                                                │
└─────────────────────────────────────────────────────────────┘
```

- **依赖方向**：服务层 → 主库（单向）。主库永远不 import Veya。
- **装配通道**：`veya.platform`（`sys.path` 注入 + 懒加载 + 优雅降级）。克隆时
  用 `git clone --recursive`；缺 submodule 时 `veya.platform.available()` 为
  False，访问时给出清晰错误而非裸 ImportError。

## 2. 单源收敛状态（SPEC §1.4）

审计方法：`tests/guardians/test_single_source.py` 用 AST 提取主库导出符号
（obase `__all__` + oprim/oskill/omodul 元素公开名），与 `veya/` 顶层定义求
交集。**任何新出现的同名符号 = CI 失败**（防内联复制漂移）。

| 状态 | 说明 |
|------|------|
| ✅ 适配器委托（已完成） | `veya.compat.ProviderRegistry` → `obase.ProviderRegistry`（get/register/list 路由到 obase 单例） |
| 📄 契约差异已文档 | `veya.utils.CostTracker` 为轻量累加器（server/coordinator §5.6 C1 共享对象），契约与 obase 定价表驱动版不同，**非双实现** |
| ⏳ 待逐项审计 | 39 个存量同名符号（见 §4），列入守卫测试 KNOWN_SYMBOLS，逐项判定"适配器委托 / 契约差异 / 贡献主库" |

## 3. 已收敛项明细

### 3.1 ProviderRegistry（✅ 适配器委托）

- **历史**：`veya.compat.ProviderRegistry` 是 stub shim（"replace obase.ProviderRegistry"）。
- **现在**：改为薄适配器，内部懒加载 `obase.ProviderRegistry.get()` 单例；
  `register` → `register_generic("generic", ..., replace=True)`；
  `get(name)` 依次尝试 `llm/vlm/image_gen/generic` 分类；
  `list` → `list_providers()`。
- **守卫**：`test_provider_registry_delegates_to_obase` 断言 delegate 模块
  以 `obase.` 开头（防回退 shim）。

### 3.2 CostTracker（📄 契约差异，非双实现）

- `veya.utils.CostTracker`：`add_cost / get_total_cost / reset / get_operations /
  record(tokens=, cost_usd=)` —— 轻量累加器，server/coordinator 作为 §5.6 C1
  跨分队共享可变对象（ContextVar 引用累加）。
- `obase.cost_tracker.CostTracker`：`record(category, provider, model_or_tier,
  unit, quantity)` 定价表驱动 + `check_budget/summary`。
- **结论**：同名不同契约，非同一段逻辑；保留 Veya 版为项目层资产。若主库
  未来提供轻量累加 API，再切换 re-export。

### 3.3 Event（📄 严格 3O 句柄层合同类型，阶段 1）

`veya/obase/interfaces.py` 定义统一事件类型 `Event`（topic/payload/ts/trace_id），
是 `EventBarrier`/`DaemonBus` 的标准载荷。主库 `event_bus.Event` 是另一套总线
事件（契约不同）。迁移计划（docs/3O_STRICT_MIGRATION.md 阶段 3+）将用严格
句柄层替换 `platform/3O` 主库；主库退役后清除本条目。

## 4. 待逐项审计清单（32 项）

> 每个符号出现在 `tests/guardians/test_single_source.py::KNOWN_SYMBOLS`，原因
> 均标注 "pending audit"。判定标准（§1.4）：① 同契约 → 适配器委托主库；
> ② 不同契约 → 文档化保留；③ Veya 增强 → 贡献回主库（资产积累 §1.3）。

`CheckpointData` `RunState` `SubagentDefinition`
`ServiceManifest` `assemble` `bash_exec` `build_ripgrep_args`
`cached` `compute_diff` `diff_session_state` `evaluate_hooks` `file_read`
`file_read_range` `file_write` `git_diff` `git_status`
`glob_match` `http_fetch` `llm_call` `llm_stream` `lsp_diagnostics`
`make_checkpoint` `match_permission_rule` `mcp_call_tool` `mcp_connect`
`merge_config` `parse_ripgrep_output` `plan_to_todos` `read_skill_frontmatter`
`redact_share_secrets` `resolve_memory_hierarchy` `restore_from_checkpoint`
`run_hook` `web_search`

## 5. 主库贡献清单（待办）

| 主库 | 贡献内容 | 状态 |
|------|---------|------|
| obase | `__init__.py` 懒加载容错（core 直导 + optional `__getattr__`） | ✅ 已提交（veya-lazy-import → main） |
| obase | `llm.py` streaming / 多模态消息转换（veya 增强贡献回主库） | ⏳ 待贡献 |
| oprim | `__init__.py` 懒加载（当前拉 chardet 等第三方） | ⏳ 待贡献 |

## 6. CI 集成

- **9 项 lint 套件**：`runner.py --report-only` 扫主库（主库 pre-v3.0 存量
  1900 项违规输出报告、不阻断 Veya CI）；Veya 侧 `veya/obase` 检查阻断。
- **守卫测试**：`tests/guardians/test_single_source.py`（装配可用 + 防新增
  内联重复 + 文档义务）。
- **检出**：`actions/checkout@v4` + `submodules: recursive`。
