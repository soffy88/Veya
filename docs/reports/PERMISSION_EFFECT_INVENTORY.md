# PERMISSION_EFFECT_INVENTORY — Phase 0

> 只读盘点,未修改任何源码。基线 `a32db3b0`(不含本文件)。

## 1. 汇总

```
TOTAL_TOOLS      146
DECLARED         0
DERIVED          8   (命中 oskill 10 条前缀表)
UNKNOWN          138   (落到 fallback 'remote',无声明来源)
CURRENT_ALLOW    146
CURRENT_DENY     0
CURRENT_ABSTAIN  0
```

## 2. 关键结论

**146/146 全部 ALLOW,0 DENY,0 ABSTAIN。**

两条独立缺陷,必须分别修:

### 缺陷 A — effect 在到达引擎前被丢弃(SF-001 本体)

`veya/remote/policy_resolver.py:454` 的 effect 硬编码:

```python
effect = "write" if request.tool in {"file.write","file.patch","artifact.write"} else "read"
```

实测这三个工具名 **都不在 master registry 里**,因此对真实的 146 个工具,
该表达式**恒为 `read`**。`PolicyRequest` 结构体没有 effect 字段,
adapter 在 `action_gateway_adapter.py:167` 算出的 `remote_effect` 只在
`except` 分支才会用到。

### 缺陷 B — effect 的唯一来源是 fallback,而非声明

即使修好缺陷 A,`oskill.classify_action_effect` 的前缀表只有 10 条
(`read/fetch/search/list/write/edit/delete/execute/...`),未命中即
`return "remote"`。而 `"remote"` 是**作用域词,不是 effect 词** —— 它
既不表示只读,也不表示破坏性,无法进入安全序。

因此 138 个工具的 effect 目前是 UNKNOWN,
却被当作 `remote` 参与判定。

### 缺陷 C — 声明通道存在但完全未使用

`server/tool_registry.py:123` 的 `SideEffect` 六档枚举
(`PURE_READ/LOCAL_WRITE/PROCESS_EXEC/NETWORK_WRITE/EXTERNAL_MUTATION/PRIVILEGED`)
实测 **0 个工具标注**;其 docstring 亦自述「目前只有 PURE_READ 被实际标注过」。
`veya/remote/models.py:73` 的 `EffectClass`(READ/WRITE/DESTRUCTIVE)只覆盖 remote 工具。

## 3. 仅有的 8 个 DERIVED 工具

| tool | 推断 effect | 当前判定 |
|---|---|---|
| `edit_diagram` | local_write | ALLOW |
| `edit_hashline` | local_write | ALLOW |
| `fetch_url` | read | ALLOW |
| `list_files` | read | ALLOW |
| `read_file_ast` | read | ALLOW |
| `read_hashline` | read | ALLOW |
| `search_genesis_ledger` | read | ALLOW |
| `write_file` | local_write | ALLOW |

注意 `write_file` / `edit_hashline` / `edit_diagram` 真实为 `local_write`,
却因缺陷 A 被引擎当作 `read` 判定为 ALLOW —— 这是最小可复现的降级证据。

## 4. effect 推断分布

| inferred effect | 工具数 |
|---|---|
| `remote` | 138 |
| `read` | 5 |
| `local_write` | 3 |

## 5. 全量清单(146)

| tool | inferred effect | provenance | 当前判定 |
|---|---|---|---|
| `edit_diagram` | local_write | DERIVED | ALLOW |
| `edit_hashline` | local_write | DERIVED | ALLOW |
| `fetch_url` | read | DERIVED | ALLOW |
| `list_files` | read | DERIVED | ALLOW |
| `read_file_ast` | read | DERIVED | ALLOW |
| `read_hashline` | read | DERIVED | ALLOW |
| `search_genesis_ledger` | read | DERIVED | ALLOW |
| `write_file` | local_write | DERIVED | ALLOW |
| `agent_loop_run` | remote | UNKNOWN | ALLOW |
| `ask_user` | remote | UNKNOWN | ALLOW |
| `assemble_code_context` | remote | UNKNOWN | ALLOW |
| `ast_grep_rewrite` | remote | UNKNOWN | ALLOW |
| `ast_grep_search` | remote | UNKNOWN | ALLOW |
| `browser_run` | remote | UNKNOWN | ALLOW |
| `code_blast_radius` | remote | UNKNOWN | ALLOW |
| `coding_apply_patch` | remote | UNKNOWN | ALLOW |
| `coding_build` | remote | UNKNOWN | ALLOW |
| `coding_diff` | remote | UNKNOWN | ALLOW |
| `coding_discard` | remote | UNKNOWN | ALLOW |
| `coding_finalize_patch` | remote | UNKNOWN | ALLOW |
| `coding_run_command` | remote | UNKNOWN | ALLOW |
| `coding_run_lint` | remote | UNKNOWN | ALLOW |
| `coding_run_tests` | remote | UNKNOWN | ALLOW |
| `coding_run_typecheck` | remote | UNKNOWN | ALLOW |
| `coding_task_run` | remote | UNKNOWN | ALLOW |
| `coding_workspace_detect` | remote | UNKNOWN | ALLOW |
| `coding_worktree_create` | remote | UNKNOWN | ALLOW |
| `coding_worktree_status` | remote | UNKNOWN | ALLOW |
| `decision_query` | remote | UNKNOWN | ALLOW |
| `decision_record` | remote | UNKNOWN | ALLOW |
| `delegate_to_genesis` | remote | UNKNOWN | ALLOW |
| `display_diagram` | remote | UNKNOWN | ALLOW |
| `evolve_solution` | remote | UNKNOWN | ALLOW |
| `get_market_data_schema` | remote | UNKNOWN | ALLOW |
| `github_issue_fetch` | remote | UNKNOWN | ALLOW |
| `github_issue_fix_prepare` | remote | UNKNOWN | ALLOW |
| `github_pr_create_draft` | remote | UNKNOWN | ALLOW |
| `github_pr_fetch` | remote | UNKNOWN | ALLOW |
| `github_pr_post_review` | remote | UNKNOWN | ALLOW |
| `github_pr_review_prepare` | remote | UNKNOWN | ALLOW |
| `goal_add_todo` | remote | UNKNOWN | ALLOW |
| `goal_start` | remote | UNKNOWN | ALLOW |
| `goal_status` | remote | UNKNOWN | ALLOW |
| `graph_query` | remote | UNKNOWN | ALLOW |
| `graph_store` | remote | UNKNOWN | ALLOW |
| `grep` | remote | UNKNOWN | ALLOW |
| `harness_guides_load` | remote | UNKNOWN | ALLOW |
| `harness_guides_search` | remote | UNKNOWN | ALLOW |
| `harness_guides_show` | remote | UNKNOWN | ALLOW |
| `harness_performance_query` | remote | UNKNOWN | ALLOW |
| `harness_ratchet_apply` | remote | UNKNOWN | ALLOW |
| `harness_ratchet_approve` | remote | UNKNOWN | ALLOW |
| `harness_ratchet_candidates` | remote | UNKNOWN | ALLOW |
| `harness_ratchet_reject` | remote | UNKNOWN | ALLOW |
| `harness_sensor_list` | remote | UNKNOWN | ALLOW |
| `harness_sensor_report` | remote | UNKNOWN | ALLOW |
| `harness_sensor_run` | remote | UNKNOWN | ALLOW |
| `learning_candidate_create` | remote | UNKNOWN | ALLOW |
| `learning_eval` | remote | UNKNOWN | ALLOW |
| `learning_rollback` | remote | UNKNOWN | ALLOW |
| `memory_correct` | remote | UNKNOWN | ALLOW |
| `memory_explain` | remote | UNKNOWN | ALLOW |
| `memory_forget` | remote | UNKNOWN | ALLOW |
| `memory_get` | remote | UNKNOWN | ALLOW |
| `memory_recall_project_lessons` | remote | UNKNOWN | ALLOW |
| `memory_search` | remote | UNKNOWN | ALLOW |
| `memory_show_source` | remote | UNKNOWN | ALLOW |
| `memory_supersede` | remote | UNKNOWN | ALLOW |
| `memory_write` | remote | UNKNOWN | ALLOW |
| `produce_wechat_article` | remote | UNKNOWN | ALLOW |
| `run_backtest_coprocessor` | remote | UNKNOWN | ALLOW |
| `run_in_sandbox` | remote | UNKNOWN | ALLOW |
| `runtime_calls_ingest` | remote | UNKNOWN | ALLOW |
| `runtime_calls_query` | remote | UNKNOWN | ALLOW |
| `skill_confirm` | remote | UNKNOWN | ALLOW |
| `skill_create` | remote | UNKNOWN | ALLOW |
| `skill_delete` | remote | UNKNOWN | ALLOW |
| `skill_deprecate` | remote | UNKNOWN | ALLOW |
| `skill_rollback` | remote | UNKNOWN | ALLOW |
| `skill_run` | remote | UNKNOWN | ALLOW |
| `skill_search` | remote | UNKNOWN | ALLOW |
| `skill_show` | remote | UNKNOWN | ALLOW |
| `skill_update` | remote | UNKNOWN | ALLOW |
| `stateful_current` | remote | UNKNOWN | ALLOW |
| `stateful_goto` | remote | UNKNOWN | ALLOW |
| `stateful_history` | remote | UNKNOWN | ALLOW |
| `stateful_start` | remote | UNKNOWN | ALLOW |
| `system_boundary_scan` | remote | UNKNOWN | ALLOW |
| `system_gate_check` | remote | UNKNOWN | ALLOW |
| `system_graph_cycle` | remote | UNKNOWN | ALLOW |
| `system_graph_review` | remote | UNKNOWN | ALLOW |
| `system_quota_should_run` | remote | UNKNOWN | ALLOW |
| `system_quota_spend_slot` | remote | UNKNOWN | ALLOW |
| `system_terminal_gate_check` | remote | UNKNOWN | ALLOW |
| `system_todo_claim` | remote | UNKNOWN | ALLOW |
| `team_approve_shutdown` | remote | UNKNOWN | ALLOW |
| `team_create` | remote | UNKNOWN | ALLOW |
| `team_delete` | remote | UNKNOWN | ALLOW |
| `team_list` | remote | UNKNOWN | ALLOW |
| `team_read_messages` | remote | UNKNOWN | ALLOW |
| `team_reject_shutdown` | remote | UNKNOWN | ALLOW |
| `team_send_message` | remote | UNKNOWN | ALLOW |
| `team_shutdown_request` | remote | UNKNOWN | ALLOW |
| `team_status` | remote | UNKNOWN | ALLOW |
| `team_task_create` | remote | UNKNOWN | ALLOW |
| `team_task_get` | remote | UNKNOWN | ALLOW |
| `team_task_list` | remote | UNKNOWN | ALLOW |
| `team_task_update` | remote | UNKNOWN | ALLOW |
| `tool_search` | remote | UNKNOWN | ALLOW |
| `veya_escalation_list` | remote | UNKNOWN | ALLOW |
| `veya_events` | remote | UNKNOWN | ALLOW |
| `veya_mission_cancel` | remote | UNKNOWN | ALLOW |
| `veya_mission_continue` | remote | UNKNOWN | ALLOW |
| `veya_mission_create` | remote | UNKNOWN | ALLOW |
| `veya_mission_inspect` | remote | UNKNOWN | ALLOW |
| `veya_mission_run` | remote | UNKNOWN | ALLOW |
| `veya_mission_set_mode` | remote | UNKNOWN | ALLOW |
| `veya_report_get` | remote | UNKNOWN | ALLOW |
| `veya_report_latest` | remote | UNKNOWN | ALLOW |
| `veya_review_apply` | remote | UNKNOWN | ALLOW |
| `veya_reviews` | remote | UNKNOWN | ALLOW |
| `wayfind_add_fog` | remote | UNKNOWN | ALLOW |
| `wayfind_add_ticket` | remote | UNKNOWN | ALLOW |
| `wayfind_chart` | remote | UNKNOWN | ALLOW |
| `wayfind_claim` | remote | UNKNOWN | ALLOW |
| `wayfind_compile_runbook` | remote | UNKNOWN | ALLOW |
| `wayfind_complete` | remote | UNKNOWN | ALLOW |
| `wayfind_decisions` | remote | UNKNOWN | ALLOW |
| `wayfind_frontier` | remote | UNKNOWN | ALLOW |
| `wayfind_gh_add_fog` | remote | UNKNOWN | ALLOW |
| `wayfind_gh_add_ticket` | remote | UNKNOWN | ALLOW |
| `wayfind_gh_chart` | remote | UNKNOWN | ALLOW |
| `wayfind_gh_claim` | remote | UNKNOWN | ALLOW |
| `wayfind_gh_complete` | remote | UNKNOWN | ALLOW |
| `wayfind_gh_decisions` | remote | UNKNOWN | ALLOW |
| `wayfind_gh_frontier` | remote | UNKNOWN | ALLOW |
| `wayfind_gh_graduate_fog` | remote | UNKNOWN | ALLOW |
| `wayfind_gh_resolve` | remote | UNKNOWN | ALLOW |
| `wayfind_gh_rule_out_of_scope` | remote | UNKNOWN | ALLOW |
| `wayfind_gh_wire_blocking` | remote | UNKNOWN | ALLOW |
| `wayfind_graduate_fog` | remote | UNKNOWN | ALLOW |
| `wayfind_resolve` | remote | UNKNOWN | ALLOW |
| `wayfind_rule_out_of_scope` | remote | UNKNOWN | ALLOW |
| `wayfind_to_spec` | remote | UNKNOWN | ALLOW |
| `wayfind_wire_blocking` | remote | UNKNOWN | ALLOW |
| `wechat_discover` | remote | UNKNOWN | ALLOW |
