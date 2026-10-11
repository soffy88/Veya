# NANO-W10 分组 B 设计：上下文 / 提示缓存 / 记忆

> 规格：`NANO_W10_SPEC.md`（主体 4/5 + ADDENDUM M/N/O/U/S/W1/W2）。
> 分组范围：`W10_GROUP_B.md`。
> 架构权威：`docs/ARCHITECTURE_STABLE.md` + `3O_RECONCILIATION.md`。
> 本文件先于代码写（SPEC HARD RULES "Finish: write docs/NANO_Wn_DESIGN.md first"）。

---

## 0. 冻结约束（本组的硬边界）

| 约束 | 本组的做法 |
|---|---|
| 无第二套调度/审批/事件存储/能力注册表 | 全部落在既有面：turn 走 `server/bot_team/turns.py`、compact 走 `server/routes/session.py` 的 `_owned_session`+既有 store、usage 走 `server/events.py` 的 `event_store`、工具走 `server/tool_registry.py` 的 `master_tools`、delegate 走 `runtime/execution/delegate_runtime.DelegateRuntime` |
| 无关键词语义路由 | prompt 组装只做**分段固定序 + 哈希**，不做任何"该不该用缓存"的程序判断（除显式 flag 关闭时的纯 no-op） |
| 新功能 flag 默认 OFF | `VEYA_PROMPT_CACHE` / `VEYA_CONTEXT_STEWARD` / `VEYA_MEMORY_REVIEW` / `VEYA_PERSISTENT_WORKERS` 全部 default OFF；`memory_search` / `read_own_chat` 这两个只读工具注册也随各自 flag |
| 新 REST 路由校验认证 + 调用者归属 | 所有新路由 `Depends(auth_mod.get_current_user)` + `_require_bot(registry, bot_id, owner_id=user["user_id"])`（复用 `server/routes/bot_team.py` 的既有归属检查，跨用户一律 404） |
| 无第二批准系统 | 记忆 review/steward 都是**纯观察+写自己的可读 Markdown**，不触发任何 side effect；`side_effect=SideEffect.LOCAL_WRITE` + `effect_capability="idempotency_key"` |
| 3O 单源 | 每个元素先 `grep veya/*` + `platform/3O/{obase,oprim,oskill,omodul,oservi}`；能 extend/re-export 就不新建（哈希复用 `obase.sha256_hash`、token 估算复用 `veya.oskill.pure.context_compress.estimate_tokens`、FTS 复用 `veya.oservi.history_store`） |
| Apache/MIT 借鉴只追加 THIRD_PARTY_NOTICES | 本组借鉴 DS Bot（Apache-2.0）、Agent-Reach（MIT）、Chat On Steroids（MIT）的**设计**，不 vendor 代码；写入 `THIRD_PARTY_NOTICES.md` |

---

## 1. 元素 → 层 → 理由（先定分层，再写实现）

### 1.1 主体 4 — prompt-cache 纪律

| 元素 | 层 | 文件 | 理由 |
|---|---|---|---|
| `section_digest` | **oprim** | `veya/oprim/prompt_cache.py` | 一个原子动作：对一段 prompt 文本求稳定 sha256 前缀。无兄弟调用、无 IO、无 trail。复用 `obase.sha256_hash.sha256_hash`（不自己写 hashlib 实现）。 |
| `cached_token_split` | **oprim** | 同上 | 一个原子动作：把一个 provider usage dict 归一成 `(cached, uncached)`。跨 provider 键名差异（`cachedContentTokenCount` / `cache_read_input_tokens` / `prompt_tokens_details.cached_tokens`）只在这一处归一。 |
| `ordered_sections` / `frozen_tool_set` / `usage_cache_rate` / `should_compact_idle` / `prewarm_due` | **oskill (pure)** | `veya/oskill/pure/prompt_cache.py` | ≥2 个 oprim 组合：分段排序+哈希、工具集冻结成不可变签名、命中率、proactive-compaction 判定、typing 预热 debounce。纯函数、无 IO、无 `user_id`、无 `time`（`now` 全部由调用方注入）。 |
| `PromptCacheBookkeeper` | **omodul** | `veya/omodul/prompt_cache_metrics.py` | 一段有明确完成/失败语义的记录事务：`record_call()` 把一次调用的 cached/uncached 记进**注入的** usage sink，返回聚合；标准 `(config, input_data, output_dir) -> dict`、`status/error` 必返、声明 `cost`/`decision_trail`/`fingerprint`。不 import `server.*`。 |
| `record_prompt_cache_usage` | **server 薄层** | `server/bot_team/prompt_cache.py` | 路由/服务层职责：把 usage 写进**既有** `server/events.py` 的 `event_store`（topic `usage.recorded`，新增到既有白名单尾部），并在 performance 路由暴露命中率。零算法。 |
| `GET /performance/prompt-cache` | **server 薄层** | `server/routes/performance.py`（追加） | 既有 performance 路由已挂 `Depends(auth_mod.require_user)`；只追加一个只读端点，不动已有端点。 |

**工具集冻结**：不新增容器类型。`frozen_tool_set(tools)` 返回 `(names_tuple, digest)`；回合内任何 tool 变更都被 digest 检出，`tool_search` 的结果**以消息形式**追加（既定的 W8 行为），本组只在 oskill 层提供"若 digest 变化 → 标记 cache_broken"的纯判定。

### 1.2 ADDENDUM M — context steward

| 元素 | 层 | 文件 | 理由 |
|---|---|---|---|
| `steward_threshold` / `build_steward_prompt` / `parse_steward_decision` | **oskill (pure)** | `veya/oskill/pure/steward_decide.py` | 一次性判断（CHECKPOINT 还是 HANDOFF）+ prompt 构建 + 解析。纯、无 IO。 |
| `context_steward` | **omodul** | `veya/omodul/context_steward.py` | 标准 omodul 契约；LLM 由注入的 `llm_caller` 提供；声明 `cost` + `decision_trail`。 |
| `steward_eval` CLI | **script** | `scripts/steward_eval.py` | 手写会话样本 + 默认 mock LLM，离线可跑。 |
| `maybe_steward_turn_end` | **server 薄层** | `server/bot_team/prompt_cache.py` | turn 末调用 omodul；flag 关时严格 no-op。 |

### 1.3 ADDENDUM N — read_own_chat

| 元素 | 层 | 文件 | 理由 |
|---|---|---|---|
| `build_anchor` / `render_snippet` | **oskill (pure)** | `veya/oskill/pure/chat_anchors.py` | 纯渲染（"part 2 #1234" 锚点格式）。 |
| `read_own_chat` 工具 | **server 薄层** | `server/bot_team/own_chat.py` | 调用**既有** `veya.oservi.history_store.default_history_store().search`（W2 FTS），不新建索引。身份来自 `runtime.bot_scope.current_bot()` + `BotRegistry` 的 `owner_id`。 |
| `ChatSearcher` | **omodul** | 不新建 | FTS 是既有能力，§1.4 单源要求不重复包装。 |

### 1.4 主体 5 — 记忆分层

| 元素 | 层 | 文件 | 理由 |
|---|---|---|---|
| `scratch_is_live` / `gc_scratch` / `daily_log_line` / `gardening_proposals` / `memory_search_hits` / `entry_allowed` / `cap_report` | **oskill (pure)** | `veya/oskill/pure/memory_layers.py` | 全部是确定性算法：TTL 判定、GC 选择、日志行格式、修剪提案、grep 式检索打分、敏感条目拒绝、大小上限判定。零 IO。 |
| `MemoryLayers` | **server 薄层** | `server/bot_team/memory.py`（追加 `LayeredMemoryStore`） | 复用既有 `BotMemoryStore` 的原子写/校验；只加 daily/ scratch/ team 三个视图 + journal。 |
| `memory_search` / `remember` / `forget` / `recall` 工具 | **server 薄层** | 同上 + `server/tool_registry.py`（追加注册） | 新模型可调用工具，全部 `current_bot()` 绑定 + `require_same_bot`。 |

### 1.5 ADDENDUM O — 记忆复审 + 团队记忆

| 元素 | 层 | 文件 | 理由 |
|---|---|---|---|
| `review_decision` / `normalize_entry` / `refuse_reason` | **oskill (pure)** | `veya/oskill/pure/memory_layers.py` | 纯解析 + 规则（拒绝 key/password/身份证形态、拒绝 web/file/tool 结果来源）。 |
| `memory_review` | **omodul** | `veya/omodul/memory_review.py` | 一轮复审事务；LLM 注入；写 journal 的 callable 注入；`cost` + `decision_trail`。 |
| `journal.jsonl` / team scope | **server 薄层** | `server/bot_team/memory.py` | 文件 IO 走既有 `atomic_write_json` 风格 + 复用 `oskill` 的 entry 格式。 |
| `GET /bots/{bot_id}/memory/summary` | **server 薄层** | `server/routes/bot_memory.py`（追加） | 既有 memory REST 路由追加一个只读端点。 |

### 1.6 W1/W2 — 持久 worker

| 元素 | 层 | 文件 | 理由 |
|---|---|---|---|
| `worker_view` / `merge_worker_briefs` / `worker_context_budget` | **oskill (pure)** | `veya/oskill/pure/worker_brief.py` | 纯：列表视图、resume brief 合并、上下文预算分摊。 |
| `WorkerRegistry` | **server 薄层** | `server/bot_team/workers.py` | 状态存**既有** bots root 下的 `workers.json`（与 `conversations.py` 同一套原子写）。不新建调度器。 |
| `resume_brief_for` | **server 薄层** | 同上 | compact/handoff 时被 `server/routes/session.py` 的 compact 路径调用（**仅追加可选钩子**，flag 关时不改变既有响应）。 |

### 1.7 U / S — dev tooling + usage 页

| 元素 | 层 | 文件 | 理由 |
|---|---|---|---|
| `llm_tap` | **script** | `scripts/llm_tap.py` | 开发工具，SPEC 明确 "may be on"。绝不落 header。 |
| `describe_image_cached` | **omodul** | `veya/omodul/image_to_text.py` | 描述调用注入（复用 `veya.oskill.vision.analyze_image` 由调用方传入），缓存按内容 sha256。无第二套视觉栈。 |
| `token_usage_report` | **omodul** | `veya/omodul/token_usage.py` | 纯聚合：把既有 usage 事件按 range/bot/model/kind 归并。 |
| `GET /usage/tokens` | **server 薄层** | `server/routes/usage.py`（追加） | 追加只读端点 + `get_current_user` 依赖。 |

---

## 2. 关键接口签名（实现必须与此一致）

```python
# veya/oprim/prompt_cache.py
def section_digest(text: str, *, length: int = 12) -> str
def cached_token_split(usage: Mapping[str, Any]) -> tuple[int, int]

# veya/oskill/pure/prompt_cache.py
def ordered_sections(sections: Sequence[tuple[str, str]]) -> list[dict[str, Any]]
def frozen_tool_set(tools: Sequence[Mapping[str, Any]]) -> tuple[tuple[str, ...], str]
def toolset_unchanged(previous_digest: str, tools: Sequence[Mapping[str, Any]]) -> bool
def usage_cache_rate(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]
def should_compact_idle(*, tokens: int, window: int, idle_seconds: float,
                        warm_seconds: float, threshold_ratio: float = 0.7) -> bool
def prewarm_due(*, last_signal_at: float | None, last_warm_at: float | None,
                now: float, debounce_seconds: float) -> bool

# veya/oskill/pure/steward_decide.py
CHECKPOINT / HANDOFF
def steward_threshold(*, tokens: int, window: int, reply_reserve: int,
                     check_ratio: float = 0.8, hard_ratio: float = 0.95) -> str
def build_steward_prompt(messages: Sequence[Mapping[str, Any]], *, limit: int = 40) -> list[dict]
def parse_steward_decision(text: str) -> dict[str, Any]   # {"action", "summary", "verbatim", "reason"}

# veya/oskill/pure/chat_anchors.py
def build_anchor(*, part: int, message_id: str) -> str    # "part 2 #1234"
def render_snippet(snippet: str, *, anchor: str, max_chars: int = 240) -> dict[str, str]

# veya/oskill/pure/memory_layers.py
def scratch_is_live(entry_ts: float, *, now: float, ttl_seconds: float) -> bool
def gc_scratch(text: str, *, now: float, ttl_seconds: float) -> tuple[str, int]
def daily_log_line(*, when: float, what: str) -> str
def gardening_proposals(text: str, *, max_chars: int, keep_lines: int = 3) -> list[dict]
def memory_search_hits(query: str, files: Mapping[str, str], *, limit: int = 10) -> list[dict]
def entry_allowed(text: str) -> tuple[bool, str]
def cap_report(text: str, *, limit: int) -> dict[str, Any]
def normalize_entry(text: str, *, source: str, added: str, bot_id: str) -> str
def review_decision(text: str) -> dict[str, Any]          # {"action", "entries", "reason"}

# veya/oskill/pure/worker_brief.py
def worker_view(workers: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]
def worker_context_budget(total_tokens: int, workers: Sequence[Mapping[str, Any]]) -> dict[str, int]
def merge_worker_briefs(session_brief: str, workers: Sequence[Mapping[str, Any]]) -> str
```

---

## 3. 数据落点（全部复用既有文件面）

| 数据 | 落点 | 复用理由 |
|---|---|---|
| 每日记忆日志 | `<bots_root>/<bot_id>/memory/daily/YYYY-MM-DD.md` | 既有 `BotMemoryStore` 目录约定 |
| scratch | `<bots_root>/<bot_id>/memory/scratch.md` | 同上 |
| 团队记忆 | `<bots_root>/team/MEMORY.md` + `<bots_root>/team/<topic>.md` | 新增一个 scope 目录，不改 per-bot 布局 |
| 记忆 journal | `<bots_root>/<bot_id>/memory/journal.jsonl` | 追加式，与 `messages.json` 同风格 |
| worker 状态 | `<bots_root>/<bot_id>/workers.json` | 与 `messages.json` 同风格（`atomic_write_json`） |
| usage / cache 事件 | `server/events.py` 的既有 `event_store`（`~/.veya/events.jsonl`） | **不新建事件存储** |
| token 明细来源 | 同上 usage 事件 payload | 同上 |

---

## 4. Flag 矩阵

| flag | 默认 | 守住什么 |
|---|---|---|
| `VEYA_PROMPT_CACHE` | OFF | 主体 4 的分段/冻结/usage 记录/命中率/prewarm/proactive-compact |
| `VEYA_CONTEXT_STEWARD` | OFF | ADDENDUM M |
| `VEYA_MEMORY_REVIEW` | OFF | ADDENDUM O 的复审 + 团队记忆写入 |
| `VEYA_PERSISTENT_WORKERS` | OFF | W1 + W2 |
| `VEYA_BOT_MEMORY_MD`（既有） | OFF | 主体 5 的 daily/scratch 注入 + `memory_search` |
| `VEYA_READ_OWN_CHAT` | OFF | ADDENDUM N 工具注册 |
| llm_tap / usage 页 | on（SPEC："may be on"） | U / S |

flag 注册统一追加到 `server/feature_flags.py` 的 `FLAGS` 元组尾部（不重排）。

---

## 5. 验收（Verification）

每项都要有一条**先红后绿**的测试；测试文件只建 `tests/test_nano_w10_b.py`。

1. `oprim.section_digest` / `cached_token_split`：三种 provider 键名都归一；digest 稳定、与内容变化同步变化。
2. `oskill.prompt_cache`：`ordered_sections` 定序 + 每段带 digest；`frozen_tool_set` 对工具顺序敏感；`usage_cache_rate` 命中率算术；`should_compact_idle` 边界；`prewarm_due` debounce。
3. steward：`steward_threshold` 在 check/hard 线；`parse_steward_decision` 对 CHECKPOINT/HANDOFF/垃圾文本的行为；omodul 永不 raise。
4. `read_own_chat`：返回带 `part N #id` 锚点；只搜得到调用者自己的 session（跨用户空）。
5. 记忆分层：daily 追加、scratch TTL 注入 + GC、MEMORY.md 上限、gardening 只**提议**不删、`memory_search` 命中/未命中。
6. 记忆复审：`entry_allowed` 拒绝 key/password/身份证；`normalize_entry` 尾部格式；team scope；journal before/after。
7. worker：`merge_worker_briefs` 含会话 + 每个 worker 摘要 + 未决项；上下文预算分摊不超总预算。
8. flags 默认 OFF（`server/feature_flags.snapshot()` 里全 False）。
9. 门：`scripts/check_3o_gates.py --strict` 的 VIOLATIONS 桶不新增；`tests/guardians/test_3o_migration_guards.py` 通过。

---

## 6. 明确不做（留给别的组）

- 主体 1/2/3/6/7（turn queue、wake router、auto-review、a11y、small items）→ A/C 组
- ADDENDUM P/Q/R/T/V（W1 之外的 group roles、bot secrets、DOM reader、search fallback、channel ladder）→ 其他组
- W3/W4/W5（Loop 模式、live stream、finish checkpoint）→ 其他组
- **前端一律不改**（主体 4 的 prewarm 端点与 S 的 usage 页只提供后端面，UI 由前端组接）