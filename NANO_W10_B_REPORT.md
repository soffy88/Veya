# NANO-W10 分组 B 报告：上下文 / 提示缓存 / 记忆

范围：`W10_GROUP_B.md`（主体 4、M、N、5、O、W1、W2、U、S）。
设计文档：`docs/NANO_W10_B_DESIGN.md`（先于代码写）。
测试文件：`tests/test_nano_w10_b.py`（本组唯一新建测试文件）。
基线：`91bae70f`（W10 规格提交，本组任何代码之前）。

---

## 1. 每项状态

| 条目 | 状态 | 落在哪 | 备注 |
|---|---|---|---|
| **主体 4** 提示缓存纪律 | ✅ 完成 | `veya/oprim/prompt_cache.py`、`veya/oskill/pure/prompt_cache.py`、`veya/omodul/prompt_cache_metrics.py`、`server/bot_team/prompt_cache.py`、`server/routes/performance.py` | 固定序哈希分段、回合内工具集冻结、cached/uncached 记入**既有** usage 事件、performance 路由暴露命中率、typing 预热 debounce、空闲窗口内的主动 compact |
| **M** context steward | ✅ 完成 | `veya/oskill/pure/steward_decide.py`、`veya/omodul/context_steward.py`、`server/bot_team/turn_end.py`、`scripts/steward_eval.py` | 0.8 check / 0.95 hard 线；一次小模型调用判 CHECKPOINT / HANDOFF；flag 关时严格 no-op |
| **N** `read_own_chat` | ✅ 完成 | `server/bot_team/own_chat.py`、`veya/oskill/pure/chat_anchors.py` | 复用**既有** W2 session FTS，未新建索引；身份取自 `current_bot()` + `BotRegistry.owner_id`，模型无法传 user id |
| **主体 5** 记忆分层 | ✅ 完成 | `veya/oskill/pure/memory_layers.py`、`server/bot_team/memory_layers.py` | daily / scratch(TTL) / core(封顶) 三层 + 修剪**提议**（绝不删用户行） |
| **O** 记忆复审 + 团队记忆 | ✅ 完成 | `veya/omodul/memory_review.py`、`server/bot_team/memory_review_bridge.py`、`server/routes/bot_memory.py` | 拒绝 key/password/身份证形态、拒绝 web/file/tool 来源、journal 记 before/after、per-bot 与 TEAM 两个 scope |
| **W1** persistent workers | ✅ 完成 | `server/bot_team/workers.py`、`veya/oskill/pure/worker_brief.py` | 行落在既有 `<bots_root>/<bot_id>/workers.json`；委托本身仍走既有 `DelegateRuntime` |
| **W2** compact & resume 带 worker | ✅ 完成 | `server/bot_team/workers.py`（`resume_brief_for`）、`server/routes/session.py` | 复用既有 compact/handoff brief；**flag 关时逐字节不变**（有测试） |
| **U** `llm_tap` + image-to-text | ⚠️ 部分 | `scripts/llm_tap.py` ✅；`veya/omodul/image_to_text.py` ⚠️ | tap 已端到端跑通。image-to-text 的 omodul + 内容哈希缓存 + 测试都在，但**没有生产调用方**——「模型没有图片输入能力时先描述」这个判定属于模型传输层的决定，跨组，见 §5 |
| **S** usage 页 token 明细 | ✅ 完成 | `veya/omodul/token_usage.py`、`server/routes/usage.py` | 按 range / bot / model / kind 聚合既有 usage 事件，含 steward/checkpoint/review/image |
| **THIRD_PARTY_NOTICES** | ✅ 完成 | `THIRD_PARTY_NOTICES.md` | 仓库原本没有该文件（W9 也没建）。只登记本组**实际借鉴**的两个来源 |
| 前端 | ➖ 按设计不做 | — | 设计文档 §6 明确「前端一律不改」；S 与 prewarm 只提供后端面 |

### Flag 矩阵（全部默认 OFF，已实测）

```
VEYA_PROMPT_CACHE       registered=True  enabled=False
VEYA_CONTEXT_STEWARD    registered=True  enabled=False
VEYA_MEMORY_REVIEW      registered=True  enabled=False
VEYA_READ_OWN_CHAT      registered=True  enabled=False
VEYA_PERSISTENT_WORKERS registered=True  enabled=False
```

`VEYA_BOT_MEMORY_MD`（既有 flag，item 5 沿用）由 `server/bot_team/memory.py:bot_memory_enabled()` 读环境变量，记忆工具的注册挂在它上面，同样默认 OFF。

---

## 2. 新增的模型可调用工具

全部由 `server/tool_registry.py` 追加注册，各自随 flag 关闭，跨 bot 访问一律拒绝。

| 工具名 | 条目 | 何时注册 | 身份 | 副作用声明 |
|---|---|---|---|---|
| `read_own_chat` | N | `VEYA_READ_OWN_CHAT` 开 | `bot_scope.current_bot()` + `BotRegistry.owner_id` | `PURE_READ` / `parallel_safe` |
| `bot_memory_search` | 主体 5 | `VEYA_BOT_MEMORY_MD` 开 | `current_bot()` | `PURE_READ` / `parallel_safe` |
| `remember` | 主体 5 + O | `VEYA_BOT_MEMORY_MD` 开 | `current_bot()` | `LOCAL_WRITE` / `idempotency_key` |
| `forget` | 主体 5 + O | `VEYA_BOT_MEMORY_MD` 开 | `current_bot()` | `LOCAL_WRITE` / `idempotency_key` |
| `recall` | 主体 5 + O | `VEYA_BOT_MEMORY_MD` 开 | `current_bot()` | `PURE_READ` / `parallel_safe` |

命名说明：per-bot 的 grep 检索面取名 `bot_memory_search` 而非 `memory_search`，因为 `server/tool_registry.py` 里**已有**一个 canonical `memory_search`（Personal MemoryRecord v2）。两个模型可见工具不能同名，否则模型无法分辨；两者复用同一套纯检索算法。`remember` 吸收了旧的 `bot_memory_note` 行为（条目会被规范化并封顶，而不是让文件无限增长）。

`describe_image`（U）**没有**注册为模型工具——目前只有 omodul，见 §5。

---

## 3. 实际跑过的测试命令与结果

全部命令形如 `.venv/bin/python -m pytest ...`（`workdir` 均为仓库根）。

| # | 命令 | 结果 |
|---|---|---|
| 1 | `pytest -q tests/test_nano_w10_b.py` | **68 passed**（本轮最终） |
| 2 | `pytest -q tests/test_lorca_p1_bots.py tests/test_p3a_bot_isolation.py` | **36 passed** |
| 3 | `pytest -q tests/test_lorca_p2_groups.py tests/test_product_shell.py` | **24 passed** |
| 4 | `pytest -q tests/guardians/test_3o_migration_guards.py` | **9 passed, 1 failed** — `test_direct_io_checker_passes_with_baseline`，**非本组引入**，见下 |
| 5 | `python scripts/check_3o_gates.py --strict` | 见 §4 |
| 6 | `python scripts/check_no_reverse_dep.py` | 6 处违规，**均不在本组文件** |
| 7 | `python scripts/check_oskill_pure.py` | `[OK] oskill 纯净度通过 (共扫描 53 个文件)` |
| 8 | `python scripts/steward_eval.py` | `3/3 passed` |
| 9 | `python scripts/llm_tap.py --stats --log …` | 输出 cache-hit 报告，退出码 0 |
| 10 | `python -m ruff check <本组 24 个文件>` | `All checks passed!` |
| 11 | `python -m ruff format --check <本组 24 个文件>` | `24 files already formatted` |
| 12 | llm_tap 端到端（假上游 + curl ×2） | 响应原样透传；两条 JSONL；第二条报 `prefix_divergence_offset: 56`；日志中 `Authorization` 出现次数 **0** |

规格里说的「2 个 resume 失败是既有失败」在本次运行中**没有出现**：`test_product_shell.py` 全绿。

### 关于 #4 的既有失败（重要，不要记成绿灯）

`test_direct_io_checker_passes_with_baseline` 断言新增直接 I/O 为 0，实际是 60。为了确认这 60 条不是本组造成的，我把 `91bae70f`（本组任何代码之前）checkout 到临时 worktree 跑同一个检查：

```
基线 91bae70f:  共 60 处新增直接 I/O（存量 288 条在基线内）
当前 HEAD:     共 60 处新增直接 I/O（存量 288 条在基线内）
diff 两个 [FAIL] 列表: 完全一致
```

即**本组新增直接 I/O 违规 = 0**，该 guardian 失败在 W10 之前就存在。另逐个核对了本组 24 个文件，均不在新增违规列表里。

---

## 4. 3O 门禁

```
OPRIM_PEER_CALLS_VIOLATIONS=0
OMODUL_DIRECT_CALLS_VIOLATIONS=0   ← 本轮从 2 降到 0
DIRECT_IO_NEW_VIOLATIONS=60         ← 与基线逐行相同，本组新增 0
DIRECT_MEMORY_WRITE_VIOLATIONS=0
ACTION_GATEWAY_BYPASS_VIOLATIONS=4  ← 与基线相同，本组新增 0
```

上一轮留下的 `context_steward.py` 从 `branch_room_sync` 借 `compute_fingerprint_for` / `_redact`，属于 `OMODUL_DIRECT_OMODUL_CALLS`，是门禁当时唯一报出的两条。已改掉：哈希仍走单一实现 `veya.oprim.audit_chain.compute_event_hash`，脱敏仍走单一实现 `obase.redact_value`（经 `veya.platform.load`），只删掉 omodul→omodul 这条边，行为不变。

### 元素 → 3O 层 → 理由

| 元素 | 层 | 文件 | 理由 |
|---|---|---|---|
| `section_digest` | oprim | `veya/oprim/prompt_cache.py` | 一个原子动作，对一段 prompt 求稳定哈希前缀；无兄弟调用、无 IO、无 trail；哈希实现复用 `obase` |
| `cached_token_split` | oprim | 同上 | 一个原子动作：把 provider 各异的 usage 键名（`cachedContentTokenCount` / `cache_read_input_tokens` / `prompt_tokens_details.cached_tokens`）归一为 `(cached, uncached)` |
| `ordered_sections` / `frozen_tool_set` / `toolset_unchanged` / `usage_cache_rate` / `should_compact_idle` / `prewarm_due` | oskill (pure) | `veya/oskill/pure/prompt_cache.py` | ≥2 个 oprim 组合；纯函数、零 IO、无 `user_id`、**不读时钟**（`now` 一律注入） |
| `PromptCacheBookkeeper` | omodul | `veya/omodul/prompt_cache_metrics.py` | 一段有明确成败语义的记录事务，usage sink 由调用方注入；标准签名 + `status/error` 必返；不 import `server.*` |
| `steward_threshold` / `build_steward_prompt` / `parse_steward_decision` | oskill (pure) | `veya/oskill/pure/steward_decide.py` | 一次性判定 + prompt 构建 + 解析；纯、无 IO |
| `context_steward` | omodul | `veya/omodul/context_steward.py` | 标准 omodul 契约；LLM 经注入的 `LLMCaller`；声明 `cost` + `decision_trail` + `fingerprint` |
| `build_anchor` / `parse_anchor` / `render_snippet` | oskill (pure) | `veya/oskill/pure/chat_anchors.py` | 纯渲染（`part 2 #1234` 锚点格式） |
| `read_own_chat` | server 薄层 | `server/bot_team/own_chat.py` | 调用**既有** `veya.oservi.history_store`，不新建索引也不新建包装层（§1.4 单源） |
| `scratch_is_live` / `gc_scratch` / `gc_expired_scratch` / `daily_log_line` / `gardening_proposals` / `memory_search_hits` / `entry_allowed` / `cap_report` / `normalize_entry` / `review_decision` | oskill (pure) | `veya/oskill/pure/memory_layers.py` | 全部确定性算法：TTL、GC、日志行格式、修剪提议、grep 打分、敏感条目拒绝、体积判定；零 IO |
| `LayeredMemoryStore` | server 薄层 | `server/bot_team/memory_layers.py` | 复用既有 `BotMemoryStore` 的原子写与文件名校验，只加 daily/scratch/team 三个视图 + journal |
| `memory_review` | omodul | `veya/omodul/memory_review.py` | 一轮复审事务；规则本地强制（模型说了不算）；journal 写入器注入；`cost` + `decision_trail` |
| `worker_view` / `worker_context_budget` / `merge_worker_briefs` | oskill (pure) | `veya/oskill/pure/worker_brief.py` | 纯投影 / 预算分摊 / brief 合并 |
| `WorkerRegistry` | server 薄层 | `server/bot_team/workers.py` | 状态存既有 bots root 下的 `workers.json`，与 `conversations.py` 同一套 `atomic_write_json` |
| `describe_image_cached` | omodul | `veya/omodul/image_to_text.py` | 描述调用由调用方注入（复用既有视觉栈，不建第二套），缓存按内容 sha256 |
| `token_usage` | omodul | `veya/omodul/token_usage.py` | 对注入记录做确定性折叠；无 IO、无 LLM |
| `steward_eval` | script | `scripts/steward_eval.py` | 手写会话样本 + 默认 mock LLM，离线可跑 |
| `llm_tap` | script | `scripts/llm_tap.py` | 开发工具，规格明确「may be on」；绝不落 header |

### 冻结约束自查

- **无第二套调度/审批/事件存储/能力注册表**：turn 走既有 `server/bot_team/turns.py`；compact 走既有 `server/routes/session.py` 的 `_owned_session`；usage 走既有 `server/events.py` 的 `event_store`（`usage.recorded` 只是往既有白名单**尾部追加**）；工具走既有 `server/tool_registry.py` 的 `master_tools`；委托走既有 `DelegateRuntime`。
- **无关键词语义路由**：prompt 组装只做分段定序 + 哈希，不含任何「该不该用缓存」的程序判断。
- **新 REST 路由全部校验认证 + 调用者归属**：`/performance/prompt-cache{,-typing-signal}` 用 `Depends(auth_mod.require_user)`；`/api/v1/usage/tokens` 同样；`/api/v1/bots/{bot_id}/memory/{summary,search,journal}` 与 `/api/v1/bots/{bot_id}/workers` 用 `Depends(auth_mod.get_current_user)` + `_owned_bot()`（跨用户一律 404，测试覆盖）。
- **共享文件只追加**：`veya/oprim/__init__.py`、`server/feature_flags.py`、`server/tool_registry.py`、`server/events.py` 均只追加，未重排、未删他人行。`server/routes/bot_memory.py` 里的新路由必须**声明在** `/{bot_id}/memory/{name}` 通配之前（FastAPI 按注册顺序匹配，通配会把 `/summary` 当文件名吃掉并返回 422），因此把本组自己追加的那一段上移了位置——动的是本组自己的行，他人行相对顺序未变。

---

## 5. 没做完的部分 / 需要人决定

1. **U 的 image-to-text 没有生产调用方**（`veya/omodul/image_to_text.py`）。
   omodul、内容哈希缓存、单测都在，但没有任何地方调用它。触发条件是「当前模型没有图片输入能力」——这个判定属于模型传输层（`_llm_transport` / ModelRouter 的能力面），跨组边界，我没有擅自改。本组留的接口是 `describe_image_cached(config, input)`，描述调用由调用方注入。
2. **`describe_image` 未注册为模型工具**，理由同上：先要确定谁拥有「这个模型能不能吃图」这个判定。
3. **前端一律未改**（设计文档 §6 的自觉约束）。因此 S 的 usage 页 token 明细、主体 4 的 prewarm 端点目前**只有后端面，没有 UI**。
4. **`VEYA_BOT_MEMORY_MD` 没有进 `server/feature_flags.py` 的 FLAGS 元组**——它是既有的、由 `bot_memory_enabled()` 读环境变量实现的 flag。我没有把它登记进新表，避免改变既有语义；`test_every_new_flag_defaults_off` 只断言本组新增的 5 个。
5. **`tests/guardians/test_3o_migration_guards.py::test_direct_io_checker_passes_with_baseline` 仍是红的**，且本组无权修（60 条违规全在本组范围外）。已用 worktree 对基线证明本组新增为 0，但**不能**因此宣称该门禁通过。
6. **没有跑全量测试套件**（规格明令 OOM 风险）。只跑了本组文件 + 规定的 4 个回归文件 + 3O guardian。
7. **Apache/MIT 借鉴只登记了 2 个来源**：`DeepSeek-Bot`（M/N/O/S）与 `chat-on-steroids`（W1/W2）。`Agent-Reach`（item V channel ladder）**不属于本组**，未登记也未实现。chat-on-steroids 的 ChatGPT web-UI 自动化/录制**故意没借鉴**（provider 条款风险），已在 notices 里写明。
8. **记忆条目 300 字上限的归属做了重新划分**：`normalize_entry` 默认仍按 300 字截断（记忆**复审**产出的条目走这条），但 `append_entry`（`remember` 工具的落盘路径）改为按**原样行**做体积检查并落盘，超 4000 字上限时带「make room」建议失败而不是静默截断。理由：静默截断等于替用户删内容，与设计里「绝不静默删除用户写的行」冲突。此改动已同步进 `docs/NANO_W10_B_DESIGN.md` §2.1。

---

## 6. 提交

本分支自 `91bae70f` 起的提交：

```
a0bb2797 chore(nano-w10b): notices, llm_tap --stats, ruff on the group's own files
abbf58c9 fix(nano-w10b): drop the omodul->omodul calls the 3O gate flagged
74b1823c fix(nano-w10b): make the group B layer tests pass on real behaviour
d631fd35 wip(nano-w10b): checkpoint after host reboot
8d2bc461 docs(nano-w10b): group scope
91bae70f docs(nano-w10): spec                                    ← 基线
```

未 push，未重启任何服务，未提交任何密钥或 `.env`。
