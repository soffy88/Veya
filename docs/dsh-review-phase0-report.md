# dsh-decision-review Phase 0 落地报告

分支：`feat/dsh-review-phase0`
前置评估：`docs/dsh-decision-review-integration.md`（第十节 Phase 0 方案）
状态：代码落地完成，未 merge / 未 push / 未部署 / 未重启服务。

---

## 一、本轮范围

按第十节推荐 Phase 0 全部 8 项执行：

| # | 第十节条目 | 落地情况 |
|---|---|---|
| 1 | 追加 `VEYA_DSH_DECISION_REVIEW` 开关（默认关闭） | `server/feature_flags.py` 表尾追加，未重排既有条目 |
| 2 | 纯逻辑移植为 `server/decision_review_core.py` | 完成，stdlib-only，无 I/O |
| 3 | 工具槽位 `server/decision_review.py`，对齐 `world_intel_mcp.py` 形态 | 完成 |
| 4 | `server/app.py` 旁接线 + 退出时关闭 | 完成 |
| 5 | 后端默认缺失，返回 `unavailable` + `route_unavailable` | 完成 |
| 6 | 不动 DSH profile / 不写 `cordis.patch.yml` / 不 `dsh plugin add` | 未触碰 |
| 7 | 测试镜像 `tests/server/test_world_intel_mcp.py` | `tests/server/test_decision_review.py` |
| 8 | 冒烟账号沿用 `dshsmoke_*` | 沿用，未新增 |

**未做（Phase 1 范围）**：复核后端（Jev 适配层或自建 `/v1/decision`）、
决策点白名单配置化、`inputFingerprint` 接 `server/events.py`、
`decision_record` 与复核结果关联、`THIRD_PARTY_NOTICES.md` 追加条目。

---

## 二、逐项落地

### 2.1 开关

`server/feature_flags.py` 表尾追加一行（第十节给出的原样形态）：

```python
# dsh-decision-review Phase 0 (default OFF; lazy tool slot only)
FeatureFlag("VEYA_DSH_DECISION_REVIEW", False, "safety", "2027-06-01"),
```

`owner=safety` 与第十节给定值一致；`removal_date=2027-06-01` 同。位置在
`VEYA_ROUND_COMPACTION` 之后，是表的最后一项，没有重排任何既有条目。

开关关闭时 `enabled()` 走既有 `config.adapters.feature_flag` 单一权威解析
（runtime > environment > file > default），无本地优先逻辑。

### 2.2 `server/decision_review_core.py`

移植自 `~/projects/dsh-decision-review/src/decision-core.mjs`（398 行）。
原文件零三方 import，Python 侧保持同构：

| 上游导出 | Python 侧 |
|---|---|
| `validatePluginConfig` | `validate_plugin_config` |
| `normalizeDecisionInput` | `normalize_decision_input` |
| `buildDecisionRequest` | `build_decision_request` |
| `parseDecisionResponse` | `parse_decision_response` |
| `projectDecisionResult` | `project_decision_result` |
| `DecisionInputError` | `DecisionInputError` |
| `SENSITIVE_TEXT_PATTERNS` | `SENSITIVE_TEXT_PATTERNS` |
| `LIMITS` | `LIMITS` |
| `DECISION_PATH` | `DECISION_PATH` |

`LIMITS` 15 项数值逐一对应，无改动。`requirePlainObject` 的"普通对象"判定
在 Python 侧按 `dict` 判定（原 JS 用 `Object.getPrototypeOf(value) ===
Object.prototype` 排除数组与类实例）。

关键行为全部保留：

- `mode === 'off'` 时 `validate_plugin_config` 只校验 mode，不要求
  `baseUrl` / `model` / 白名单（上游 `test/decision-core.test.mjs` 的行为）。
- `baseUrl` 拒绝 `/v1` 后缀、`/decision` 后缀、完整端点、内嵌凭据、查询串与片段。
- 白名单外调用抛 `DECISION_POINT_NOT_ALLOWED`，在 fetch 之前。
- 疑似凭据输入抛 `SENSITIVE_INPUT`，请求不构造。
- `parse_decision_response` 对非法 JSON、候选集外 choice、超范围 confidence、
  含非候选键的 probabilities 一律返回 `None`（对应上游 `undefined`）。
- `project_decision_result` 的 shadow / abstained / ok / not_adoptable 四态
  与 `adoptable` 判定不变。

### 2.3 `server/decision_review.py`（工具槽位）

形态对齐 `server/world_intel_mcp.py`，导出 `enabled()` / `status()` /
`wire_master_tools()` / `close_decision_review()`。

- 工具名 `decision_review`，注册进既有 `MasterToolRegistry`（默认全局），
  **不进常驻工具集**，经 `search_tools` 显式检索后追加到会话工具面。
- `side_effect=SideEffect.PURE_READ`。
- 未配置复核端点时返回 `unavailable` + `route_unavailable`，主流程照旧；
  工具本身不发起任何网络请求（Phase 0 无后端）。
- 开关关闭时 `wire_master_tools()` 直接返回 0，不构造任何客户端、不注册、
  全局注册表 schema JSON 字节不变。

### 2.4 `server/app.py` 接线

在 world-intel 旁追加：

- lifespan 启动段 try/except 调用 `wire_master_tools()`，失败只 warning 不阻断启动。
- lifespan 关闭段 try/except 调用 `close_decision_review()`。

### 2.5 未触碰

`~/.config/veya/dsh.env`、`cordis.patch.yml`、`dsh plugin add` 均未执行；
主链提示词、常驻工具集、DSH profile overlay 均无改动。

---

## 三、测试

新增 `tests/server/test_decision_review.py`（43 项），镜像
`tests/server/test_world_intel_mcp.py` 的隔离手法（monkeypatch 清 flag/环境、
`lazy_tools.reset_sessions()`）。

覆盖第十节第 7 点要求的四类断言：

1. **开关关闭**：全局注册表 schema JSON 字节不变（`master_tools` 与注入
   registry 两条路径都断言）；`client_factory` 换成记录型可调用，证明不构造
   客户端；`wire_master_tools()` 返回 0 且工具未注册。
2. **敏感输入被拒且不分发请求**：经 `registry.execute()` 走真实工具体，
   `SENSITIVE_INPUT`，且结果里没有 `request` 字段（请求未构造）、
   注入的 transport 工厂未被调用。另对 5 种凭据形态逐一参数化，并配一条
   反向断言（普通中文业务文案不得被命中）。
3. **超时与父取消按时返回**：`asyncio.wait_for(..., 5.0)` 包住
   `registry.execute()`，注入的 transport 工厂设了 `entered` 事件——
   返回时该事件未置位，即调用路径根本没进入会挂起的 transport。
   父取消场景断言 `CancelledError` 原样穿透且 `task.cancelled()` 为真。
4. **reasonCode 映射**：`route_unavailable`（开关关 / 开关开无后端 /
   开关开配了后端三种路径都必须是它）、`INVALID_CONFIG`、
   以及 6 个 `DecisionInputError.code` 原样透传；另断言
   `GATEWAY_ERROR_REASONS` 与上游 `decision-model.mjs` 的失败表逐项一致。

另加纯逻辑移植回归：`LIMITS` 15 项数值、`DECISION_PATH`、`mode: off` 宽松校验、
`baseUrl` 10 条拒绝分支 + 2 条接受分支、`model` / `apiKeyEnv` / 白名单 /
`timeoutMs` / `abstainBelow` 校验、`normalize_decision_input` 6 类拒绝、
`build_decision_request` 结构与 `systemExtra` 追加、
`parse_decision_response` 1 正例 + 8 反例、`project_decision_result` 四态。

测试过程中发现并修掉两个真实缺陷（详见第七节）。

运行结果见第六节。

---

## 四、零侵入验证

- 开关关闭时 `server/decision_review.py` 只 import 常量与函数定义，
  无模块级副作用（无网络、无子进程、无注册）。
- `feature_flags.snapshot()` 仅多出一行 `VEYA_DSH_DECISION_REVIEW`。
- 常驻工具名集合在关闭时不变（测试断言）。
- `ruff check` 对 4 个改动/新增文件全绿；`ruff format --check` 全绿。
  `server/app.py` 的 2 处 UP041 与 format 漂移在本分支改动之前就存在
  （`git show HEAD:server/app.py` 复现同样结果），未顺手改。

---

## 五、风险与遗留

1. **复核服务仍不存在**：Phase 0 工具槽位返回 `unavailable`，
   后端选型（Jev 适配 vs 自建 `/v1/decision`）留 Phase 1。
2. **成本预算闸未接**：Phase 0 无调用路径，`max_calls` 类闸留 Phase 1。
3. **白名单未治理**：Phase 0 不构造请求，白名单仅在 core 校验层生效。
4. **`THIRD_PARTY_NOTICES.md` 未追加**：第十节未要求 Phase 0 补，列 Phase 1。
5. **测试未覆盖真实 transport**：Phase 0 没有 transport，超时/取消两条断言是
   "不进入会挂起的路径" 的反证，不是对超时实现的正面测试。接了后端后必须补。

---

## 六、验证命令与结果

```text
$ python3 -m pytest -q tests/server/test_decision_review.py
collected 43 items

tests/server/test_decision_review.py ................................... [ 81%]
........                                                        [100%]

============================== 43 passed in 1.80s ==============================

$ python3 -m pytest -q tests/server/test_world_intel_mcp.py \
      tests/server/test_round_compaction.py tests/server/test_tool_description_integrity.py \
      tests/server/test_server_boot_gate.py tests/test_config_authority.py
====================== 36 passed, 4 warnings in 21.61s ========================
（test_config_authority.py 单独跑：32 passed in 0.40s）

$ ruff check server/decision_review.py server/decision_review_core.py \
      tests/server/test_decision_review.py server/feature_flags.py
All checks passed!

$ ruff format --check server/decision_review.py server/decision_review_core.py \
      tests/server/test_decision_review.py server/feature_flags.py
4 files already formatted
```

---

## 七、测试中修掉的两个缺陷

写测试时暴露的两个移植/槽位缺陷，均已修复并有测试覆盖：

1. **`validate_plugin_config` 不接受已校验的 `PluginConfig`**。
   `normalize_decision_input` 与 `project_decision_result` 内部会对自己收到的
   配置再跑一次 `validate_plugin_config`；上游是纯对象所以无害，Python 侧
   `dict(PluginConfig)` 直接抛 `TypeError: 'PluginConfig' object is not iterable`。
   即"校验一次后复用"这条最自然的用法整个崩掉。
   修法：`validate_plugin_config` 对 `PluginConfig` 实例原样返回。
   `server/decision_review_core.py` 的 `validate_plugin_config`。
2. **`_tool` 没有 await `_review`**。`_review` 改成 async 后 `_tool` 仍写
   `return _review(payload)`，注册进 registry 后 `execute()` 拿到的是一串
   `'<coroutine object _review ...>'` 字符串——工具对大模型完全不可用，
   且伴随 `coroutine was never awaited` 警告。
   修法：`return await _review(payload)`。`server/decision_review.py` 的 `_tool`。

两个缺陷都是"不经 registry 直接调 `_review`"的冒烟测不出来的，
必须走 `registry.execute()` 的真实路径才会现形。

---

## 八、提交

- 分支：`feat/dsh-review-phase0`
- 未 merge / 未 push / 未部署 / 未重启服务。
