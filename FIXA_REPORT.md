# FIXA 验收缺陷修复交付报告

## 概述
基于 hark/gbp6-evoontology 分支，针对 veya-acc 验收（ACC_REPORT.md、evidence/）发现的 3 项 Bot 与事件机制缺陷，严格按 TDD（先写失败测试 RED、再修复 GREEN、再全面回归）完成修复，未侵入生产与外部环境，无新增失败。

---

## 一、每项根因与改动

### 1. POST 创建 Bot 时 `available_skill_ids` 被丢弃
- **根因**：
  `server/routes/bot_team.py` 中的 `BotCreateRequest` Pydantic 模型缺少 `available_skill_ids` 字段，且 `create_bot` 处理函数在构造 `BotSpec` 时未传入该字段，导致客户端在 POST 创建 Bot 时携带的技能 ID 列表被静默丢弃（落盘为 `[]`），必须后续额外调用 PATCH 请求才能生效。
- **改动**：
  - 在 `server/routes/bot_team.py` 的 `BotCreateRequest` 中新增 `available_skill_ids: list[str] | None = None`、`available_playbook_ids: list[str] | None = None` 及 `runner_id: str | None = None`。
  - 在 `create_bot` 处理函数中读取 `req.available_skill_ids` 并持久化到 `BotSpec`，并在创建返回的 `_bot_view` 中完整体现。
  - 配合 `VEYA_ROLE_SKILLS_RUNTIME=1` 时，新建 Bot 的提示词直接生效注入 `<available_skills>`，无需 PATCH。
- **Commit**：`314da7a0`

### 2. 消息规则信号无分发器、群/私聊消息返回 no_match 且未触发
- **根因**：
  1. `server/routes/nano_w9.py` 中的 `_routine_router()` 在构造 `RoutineSignalRouter` 时未提供 `dispatch` 参数（为 None），导致当规则匹配成功后，`RoutineSignalRouter._dispatch` 只能返回 `status: skipped, reason: "no dispatcher injected"`，未真实派发任务。
  2. `SignalRequest` 缺少 `pattern`、`content` 等兼容字段，且 `veya/oskill/pure/trigger_match.py` 的 `match_message_trigger` 仅支持 `text` 和 `regex`，未支持 `pattern` 别名与 `source: group` 过滤。
  3. `server/routes/bot_team.py` 的群消息 (`post_group_message`) 与私聊消息 (`post_bot_message`) 链路完全未接入规则信号分发器，消息到达后无法激活已配规则。
- **改动**：
  - `server/routes/nano_w9.py`：
    - 在 `_routine_router()` 中注入具备真实派发能力的 `default_dispatch` 分发器，通过 `dispatch_trigger` 完成目标 routine handoff，并将派发结果记入 `RoutineHistoryStore`（记录状态为 `dispatched`）。
    - 扩展 `SignalRequest`，兼容 `pattern`、`content`、`bot_id`、`group_id` 字段；若 `text` 未显式传值，自动回退取 `pattern` 或 `content`。
    - 注册 `/api/v1/signals` 别名路由 `signals_compat_router` 并挂载至根 app，兼容直连信号调用的客户端。
  - `server/nano_w9/routine_triggers.py`：
    - 优化 `_dispatch`，自动提取 routine 自身声明的 `trigger_topic`（支持 `event.arrive` 与 `schedule.trigger`），并将 `routine_id` 与 `routine` 原样传入 dispatcher，支持向下兼容各类派发函数签名。
  - `veya/oskill/pure/trigger_match.py`：
    - `match_message_trigger` 增强支持 `trigger.pattern` 别名与 `signal.pattern`/`signal.content`；增强 `source` 过滤逻辑（支持 `source="group"` 匹配群消息）。
  - `server/routes/bot_team.py`：
    - 在 `post_group_message`（群聊）与 `post_bot_message`（私聊）接收到消息时，若 `VEYA_EVENT_TRIGGERS` 或 `VEYA_ROUTINE_TRIGGERS` 开启，自动构造 `kind="message"` 信号并发往 `_routine_router().handle(signal)`，实现真实匹配与派发。
- **Commit**：`862fda92`

### 3. routine 创建默认 enabled 设计意图查清与调整
- **设计意图查清**：
  1. 领域模型一致性：`server/goal_run/models.py` 中 `RoutineSpec` 的数据模型定义默认为 `enabled: bool = True`。
  2. 前端与用户认知一致性：`apps/web/src/routes/team/bot/[bot_id]/RoutinesTab.svelte` 创建 routine 表单中，显式设定 `enabled: true`，说明用户在创建定时任务时意图是“立即生效进入调度”。
  3. 历史规范溯源：`ORCA_W4_SPEC.md` 原文为 “4. Create disabled + run-now: routines REST accepts enabled=false on create; POST /routines/{id}/run-now works on disabled routines”，其初衷是允许通过传 `enabled=false` 创建初始处于禁用态的 routine，但 REST 请求模型 `RoutineCreateRequest` 误将默认值写成了 `enabled: bool = False`，导致未显式传参的 API 客户端创建出的 routine 处于禁用状态，RoutineClock 不计入调度。
- **改动**：
  - 将 `server/routes/routines.py` 中的 `RoutineCreateRequest.enabled` 默认值改为 `True`。
  - 当调用方显式传入 `enabled=False` 时，严格遵循并保持禁用。
  - 同时在 `RoutineCreateRequest` 与 `RoutinePatchRequest` 中开放 `triggers` 与 `trigger_topic` 字段，使创建与更新 routine 时能同时配置事件触发器。
- **Commit**：`89110589`

---

## 二、Commit 记录

| Commit | 阶段 | 说明 |
|---|---|---|
| `83724857` | RED | test(fixa): add reproduction tests for Bot available_skill_ids, event triggers dispatcher, and routine default enabled (RED) |
| `314da7a0` | GREEN (Item 1) | feat(bot): persist available_skill_ids on POST bot creation |
| `89110589` | GREEN (Item 3) | feat(routines): default enabled=True on creation while respecting explicit enabled=False |
| `862fda92` | GREEN (Item 2) | feat(events): wire message event trigger dispatcher and handle group and bot message rules |
| `f4cdf063` | REFACTOR/STYLE | style(fixa): clean up unused imports and order imports with ruff |

---

## 三、测试原文（新测试）

执行命令：`python3 -m pytest -v tests/test_fixa_bot_events_routine.py`

```text
============================= test session starts ==============================
platform linux -- Python 3.14.4, pytest-9.1.1, pluggy-1.6.0 -- /usr/bin/python3
cachedir: .pytest_cache
rootdir: /data/soffy/projects/veya-fixa
configfile: pyproject.toml
plugins: anyio-4.14.2, asyncio-1.4.0, typeguard-4.4.4
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 4 items

tests/test_fixa_bot_events_routine.py::test_create_bot_persists_available_skill_ids PASSED [ 25%]
tests/test_fixa_bot_events_routine.py::test_signal_endpoint_message_pattern_matching_and_dispatch PASSED [ 50%]
tests/test_fixa_bot_events_routine.py::test_group_and_bot_messages_match_rules_and_dispatch_routine PASSED [ 75%]
tests/test_fixa_bot_events_routine.py::test_routine_create_defaults_enabled_true_and_respects_explicit_false PASSED [100%]

=============================== warnings summary ===============================
tests/test_fixa_bot_events_routine.py::test_create_bot_persists_available_skill_ids
  /home/soffy/.veya/skills/ecc_opensource_forker/run.py:63: SyntaxWarning: "\s" is an invalid escape sequence. Such sequences will not work in the future. Did you mean "\\s"? A raw string is also an option.
    [A-Za-z0-9_]*(KEY|TOKEN|SECRET|PASSWORD|PASS|API_KEY|AUTH)[A-Za-z0-9_]*\s*[=:]\s*['\"]?[A-Za-z0-9+/=_-]{8,}

tests/test_fixa_bot_events_routine.py::test_create_bot_persists_available_skill_ids
  /home/soffy/.veya/skills/ecc_opensource_packager/run.py:61: SyntaxWarning: "\`" is an invalid escape sequence. Such sequences will not work in the future. Did you mean "\\`"? A raw string is also an option.
    \`\`\`bash

tests/test_fixa_bot_events_routine.py::test_create_bot_persists_available_skill_ids
  /home/soffy/.veya/skills/ecc_opensource_sanitizer/run.py:40: SyntaxWarning: "\s" is an invalid escape sequence. Such sequences will not work in the future. Did you mean "\\s"? A raw string is also an option.
    pattern: [A-Za-z0-9_]*(api[_-]?key|apikey|api[_-]?secret)[A-Za-z0-9_]*\s*[=:]\s*['"]?[A-Za-z0-9+/=_-]{16,}

tests/test_fixa_bot_events_routine.py::test_create_bot_persists_available_skill_ids
  /home/soffy/.veya/skills/ecc_php_reviewer/run.py:40: SyntaxWarning: "\E" is an invalid escape sequence. Such sequences will not work in the future. Did you mean "\\E"? A raw string is also an option.
    - **Bare try/catch**: `catch (\Exception $e) {}` — log and handle, never silently swallow

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
======================== 4 passed, 4 warnings in 1.99s =========================
```

---

## 四、回归测试原文（基线回归）

执行命令：
`python3 -m pytest -q tests/test_w1*.py tests/test_nano_*.py tests/test_lorca_*.py tests/test_orca_*.py tests/test_p3a_bot_isolation.py tests/test_product_shell.py tests/test_gbp*.py`

```text
============================= test session starts ==============================
platform linux -- Python 3.14.4, pytest-9.1.1, pluggy-1.6.0
rootdir: /data/soffy/projects/veya-fixa
configfile: pyproject.toml
plugins: anyio-4.14.2, asyncio-1.4.0, typeguard-4.4.4
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collected 1080 items

tests/test_w11_bot_reply.py ..                                           [  0%]
tests/test_w11_c_side_effect_approval.py .....                           [  0%]
tests/test_w11_d2_ownership.py ...                                       [  0%]
tests/test_w11_d2_routes.py ....                                         [  1%]
tests/test_w11_e_correction_forget.py ...                                [  1%]
tests/test_w11_s2_s3_landing.py ..                                       [  1%]
tests/test_w11_s6_s7_routines.py ..                                      [  1%]
tests/test_w11_sf001_shim.py ...                                         [  2%]
tests/test_w12_brain_breaker.py ..........                               [  3%]
tests/test_w12_funcfix.py ..........                                     [  4%]
tests/test_w12_supervise_handoff_e2e.py .....                            [  4%]
tests/test_w12_supervise_root.py .                                       [  4%]
tests/test_w12_supervisor.py .....................                       [  6%]
tests/test_w13_remote_core.py ...............................            [  9%]
tests/test_w13_remote_service.py .....................                   [ 11%]
tests/test_w14_mcp_pack.py ...........................                   [ 13%]
tests/test_w15_presets.py ..............                                 [ 15%]
tests/test_w16_brief.py ........                                         [ 15%]
tests/test_w16_patrol.py .......                                         [ 16%]
tests/test_w16_routes.py ......                                          [ 17%]
tests/test_w16_tasks.py ......                                           [ 17%]
tests/test_w16_tools.py .........                                        [ 18%]
tests/test_w17_ask_once_cache.py ......                                  [ 19%]
tests/test_w17_canonical_host.py ......                                  [ 19%]
tests/test_w17_config_protection.py .....                                [ 20%]
tests/test_w17_session_transport.py ..                                   [ 20%]
tests/test_w17_ssh_core.py ................                              [ 21%]
tests/test_w17_ssh_rest.py ....                                          [ 22%]
tests/test_w17_ssh_sessions.py ...                                       [ 22%]
tests/test_w17_ssh_tools.py ............                                 [ 23%]
tests/test_w17_supervisor_bridge.py ...                                  [ 23%]
tests/test_w18_deepeval.py .......ssssss                                 [ 25%]
tests/test_nano_w10_a.py ............................................... [ 29%]
......................                                                   [ 31%]
tests/test_nano_w10_b.py ............................................... [ 35%]
.....................                                                    [ 37%]
tests/test_nano_w10_c.py ............................................... [ 42%]
......                                                                   [ 42%]
tests/test_nano_w10_d.py ............................................    [ 46%]
tests/test_nano_w6_approvals.py ..........................               [ 49%]
tests/test_nano_w6_delivery_states.py .....                              [ 49%]
tests/test_nano_w6_pairing.py ............                               [ 50%]
tests/test_nano_w7_channels.py ..................................        [ 53%]
tests/test_nano_w8_cli_hub.py ...................                        [ 55%]
tests/test_nano_w8_hands_models.py ..................................    [ 58%]
tests/test_nano_w8_runner_ops.py ................                        [ 60%]
tests/test_nano_w9_buzz.py ............................................. [ 64%]
..................................                                       [ 67%]
tests/test_nano_w9_cli_harness.py ....................................   [ 70%]
tests/test_lorca_msgbot_tool.py .........                                [ 71%]
tests/test_lorca_p1_bots.py .................                            [ 73%]
tests/test_lorca_p2_groups.py ...........                                [ 74%]
tests/test_lorca_p3_memory.py .......................................... [ 78%]
.....................                                                    [ 80%]
tests/test_lorca_p4_routines.py .............                            [ 81%]
tests/test_lorca_p5_push.py ......................                       [ 83%]
tests/test_lorca_p6_runners.py .....................                     [ 85%]
tests/test_orca_w1_workspace.py ........................                 [ 87%]
tests/test_orca_w2_sessions.py ................                          [ 88%]
tests/test_orca_w3_review.py ..........................                  [ 91%]
tests/test_orca_w4_routines_artifacts.py ..............................  [ 94%]
tests/test_orca_w5_design_mode.py ..........                             [ 95%]
tests/test_p3a_bot_isolation.py ...................                      [ 96%]
tests/test_product_shell.py .............                                [ 98%]
tests/test_gbp1_role_skills_runtime.py ....                              [ 98%]
tests/test_gbp2_skill_teach.py ..                                        [ 98%]
tests/test_gbp3_event_triggers.py .....                                  [ 99%]
tests/test_gbp4_evidence.py ...                                          [ 99%]
tests/test_gbp6_evoontology.py .......                                   [100%]

=============================== warnings summary ===============================
../../../../home/soffy/.veya/skills/ecc_opensource_forker/run.py:63
  /home/soffy/.veya/skills/ecc_opensource_forker/run.py:63: SyntaxWarning: "\s" is an invalid escape sequence. Such sequences will not work in the future. Did you mean "\\s"? A raw string is also an option.
    [A-Za-z0-9_]*(KEY|TOKEN|SECRET|PASSWORD|PASS|API_KEY|AUTH)[A-Za-z0-9_]*\s*[=:]\s*['\"]?[A-Za-z0-9+/=_-]{8,}

../../../../home/soffy/.veya/skills/ecc_opensource_packager/run.py:61
  /home/soffy/.veya/skills/ecc_opensource_packager/run.py:61: SyntaxWarning: "\`" is an invalid escape sequence. Such sequences will not work in the future. Did you mean "\\`"? A raw string is also an option.
    \`\`\`bash

../../../../home/soffy/.veya/skills/ecc_opensource_sanitizer/run.py:40
  /home/soffy/.veya/skills/ecc_opensource_sanitizer/run.py:40: SyntaxWarning: "\s" is an invalid escape sequence. Such sequences will not work in the future. Did you mean "\\s"? A raw string is also an option.
    pattern: [A-Za-z0-9_]*(api[_-]?key|apikey|api[_-]?secret)[A-Za-z0-9_]*\s*[=:]\s*['"]?[A-Za-z0-9+/=_-]{16,}

../../../../home/soffy/.veya/skills/ecc_php_reviewer/run.py:40
  /home/soffy/.veya/skills/ecc_php_reviewer/run.py:40: SyntaxWarning: "\E" is an invalid escape sequence. Such sequences will not work in the future. Did you mean "\\E"? A raw string is also an option.
    - **Bare try/catch**: `catch (\Exception $e) {}` — log and handle, never silently swallow

tests/test_nano_w10_d.py::test_capture_browser_a11y_with_mock_page
  /data/soffy/projects/veya-fixa/veya/obase/a11y.py:46: DeprecationWarning: 'asyncio.iscoroutinefunction' is deprecated and slated for removal in Python 3.16; use inspect.iscoroutinefunction() instead
    if asyncio.iscoroutinefunction(snapshot_fn):

tests/test_nano_w6_approvals.py::test_pure_evaluate_grant_allow_and_gated_ask
  tests/test_nano_w6_approvals.py:58: PytestWarning: The test <Function test_pure_evaluate_grant_allow_and_gated_ask> is marked with '@pytest.mark.asyncio' but it is not an async function. Please remove the asyncio mark. If the test is not marked explicitly, check for global marks applied via 'pytestmark'.
    def test_pure_evaluate_grant_allow_and_gated_ask():

tests/test_nano_w6_approvals.py::test_pure_evaluate_human_only_and_deny
  tests/test_nano_w6_approvals.py:76: PytestWarning: The test <Function test_pure_evaluate_human_only_and_deny> is marked with '@pytest.mark.asyncio' but it is not an async function. Please remove the asyncio mark. If the test is not marked explicitly, check for global marks applied via 'pytestmark'.
    def test_pure_evaluate_human_only_and_deny():

tests/test_nano_w6_approvals.py::test_composite_decision_allow_ask_deny
  tests/test_nano_w6_approvals.py:94: PytestWarning: The test <Function test_composite_decision_allow_ask_deny> is marked with '@pytest.mark.asyncio' but it is not an async function. Please remove the asyncio mark. If the test is not marked explicitly, check for global marks applied via 'pytestmark'.
    def test_composite_decision_allow_ask_deny(approval_store):

tests/test_nano_w6_approvals.py::test_oprim_approval_and_grant_atoms
  tests/test_nano_w6_approvals.py:120: PytestWarning: The test <Function test_oprim_approval_and_grant_atoms> is marked with '@pytest.mark.asyncio' but it is not an async function. Please remove the asyncio mark. If the test is not marked explicitly, check for global marks applied via 'pytestmark'.
    def test_oprim_approval_and_grant_atoms(approval_store):

tests/test_nano_w6_approvals.py::test_oprim_handoff_atoms
  tests/test_nano_w6_approvals.py:153: PytestWarning: The test <Function test_oprim_handoff_atoms> is marked with '@pytest.mark.asyncio' but it is not an async function. Please remove the asyncio mark. If the test is not marked explicitly, check for global marks applied via 'pytestmark'.
    def test_oprim_handoff_atoms(approval_store):

tests/test_nano_w6_approvals.py::test_rest_grants_list_and_revoke
  tests/test_nano_w6_approvals.py:434: PytestWarning: The test <Function test_rest_grants_list_and_revoke> is marked with '@pytest.mark.asyncio' but it is not an async function. Please remove the asyncio mark. If the test is not marked explicitly, check for global marks applied via 'pytestmark'.
    def test_rest_grants_list_and_revoke(approval_store, monkeypatch):

tests/test_nano_w6_approvals.py::test_mention_parsing_pure
  tests/test_nano_w6_approvals.py:470: PytestWarning: The test <Function test_mention_parsing_pure> is marked with '@pytest.mark.asyncio' but it is not an async function. Please remove the asyncio mark. If the test is not marked explicitly, check for global marks applied via 'pytestmark'.
    def test_mention_parsing_pure():

tests/test_nano_w6_approvals.py::test_route_text_to_runner_match_and_unknown
  tests/test_nano_w6_approvals.py:483: PytestWarning: The test <Function test_route_text_to_runner_match_and_unknown> is marked with '@pytest.mark.asyncio' but it is not an async function. Please remove the asyncio mark. If the test is not marked explicitly, check for global marks applied via 'pytestmark'.
    def test_route_text_to_runner_match_and_unknown(tmp_path):

tests/test_nano_w6_approvals.py::test_flag_off_by_default
  tests/test_nano_w6_approvals.py:597: PytestWarning: The test <Function test_flag_off_by_default> is marked with '@pytest.mark.asyncio' but it is not an async function. Please remove the asyncio mark. If the test is not marked explicitly, check for global marks applied via 'pytestmark'.
    def test_flag_off_by_default(monkeypatch):

tests/test_nano_w6_pairing.py::test_code_shape_and_alphabet
  tests/test_nano_w6_pairing.py:28: PytestWarning: The test <Function test_code_shape_and_alphabet> is marked with '@pytest.mark.asyncio' but it is not an async function. Please remove the asyncio mark. If the test is not marked explicitly, check for global marks applied via 'pytestmark'.
    def test_code_shape_and_alphabet(pairing_store):

tests/test_nano_w6_pairing.py::test_code_ttl_and_single_use
  tests/test_nano_w6_pairing.py:40: PytestWarning: The test <Function test_code_ttl_and_single_use> is marked with '@pytest.mark.asyncio' but it is not an async function. Please remove the asyncio mark. If the test is not marked explicitly, check for global marks applied via 'pytestmark'.
    def test_code_ttl_and_single_use(pairing_store):

tests/test_nano_w6_pairing.py::test_code_expiry
  tests/test_nano_w6_pairing.py:51: PytestWarning: The test <Function test_code_expiry> is marked with '@pytest.mark.asyncio' but it is not an async function. Please remove the asyncio mark. If the test is not marked explicitly, check for global marks applied via 'pytestmark'.
    def test_code_expiry(pairing_store):

tests/test_nano_w6_pairing.py::test_code_rate_limited
  tests/test_nano_w6_pairing.py:57: PytestWarning: The test <Function test_code_rate_limited> is marked with '@pytest.mark.asyncio' but it is not an async function. Please remove the asyncio mark. If the test is not marked explicitly, check for global marks applied via 'pytestmark'.
    def test_code_rate_limited(pairing_store):

tests/test_nano_w6_pairing.py::test_code_invalid_and_attempt_capped
  tests/test_nano_w6_pairing.py:68: PytestWarning: The test <Function test_code_invalid_and_attempt_capped> is marked with '@pytest.mark.asyncio' but it is not an async function. Please remove the asyncio mark. If the test is not marked explicitly, check for global marks applied via 'pytestmark'.
    def test_code_invalid_and_attempt_capped(pairing_store):

tests/test_nano_w6_pairing.py::test_store_never_persists_plaintext
  tests/test_nano_w6_pairing.py:81: PytestWarning: The test <Function test_store_never_persists_plaintext> is marked with '@pytest.mark.asyncio' but it is not an async function. Please remove the asyncio mark. If the test is not marked explicitly, check for global marks applied via 'pytestmark'.
    def test_store_never_persists_plaintext(pairing_store):

tests/test_nano_w6_pairing.py::test_credential_issue_and_verify
  tests/test_nano_w6_pairing.py:99: PytestWarning: The test <Function test_credential_issue_and_verify> is marked with '@pytest.mark.asyncio' but it is not an async function. Please remove the asyncio mark. If the test is not marked explicitly, check for global marks applied via 'pytestmark'.
    def test_credential_issue_and_verify(pairing_store):

tests/test_nano_w6_pairing.py::test_credential_expiry
  tests/test_nano_w6_pairing.py:108: PytestWarning: The test <Function test_credential_expiry> is marked with '@pytest.mark.asyncio' but it is not an async function. Please remove the asyncio mark. If the test is not marked explicitly, check for global marks applied via 'pytestmark'.
    def test_credential_expiry(pairing_store):

tests/test_nano_w6_pairing.py::test_pairing_rest_disabled_by_default
  tests/test_nano_w6_pairing.py:183: PytestWarning: The test <Function test_pairing_rest_disabled_by_default> is marked with '@pytest.mark.asyncio' but it is not an async function. Please remove the asyncio mark. If the test is not marked explicitly, check for global marks applied via 'pytestmark'.
    def test_pairing_rest_disabled_by_default(monkeypatch):

tests/test_nano_w6_pairing.py::test_pairing_rest_enabled_roundtrip
  tests/test_nano_w6_pairing.py:195: PytestWarning: The test <Function test_pairing_rest_enabled_roundtrip> is marked with '@pytest.mark.asyncio' but it is not an async function. Please remove the asyncio mark. If the test is not marked explicitly, check for global marks applied via 'pytestmark'.
    def test_pairing_rest_enabled_roundtrip(monkeypatch, tmp_path):

tests/test_orca_w2_sessions.py::TestBriefGeneration::test_focused_brief
tests/test_orca_w2_sessions.py::TestBriefGeneration::test_full_brief
  /data/soffy/projects/veya-fixa/server/routes/session_handoff.py:104: DeprecationWarning: datetime.datetime.utcnow() is deprecated and scheduled for removal in a future version. Use timezone-aware objects to represent datetimes in UTC: datetime.datetime.now(datetime.UTC).
    now = datetime.utcnow().isoformat() + "Z"

tests/test_gbp3_event_triggers.py::test_gbp3_http_webhook_and_history_endpoints
  /home/soffy/.local/lib/python3.14/site-packages/httpx2/_models.py:423: DeprecationWarning: Use 'content=<...>' to upload raw bytes/text content.
    headers, stream = encode_request(

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========== 1074 passed, 6 skipped, 27 warnings in 143.97s (0:02:23) ===========
```

---

FIXA DONE
