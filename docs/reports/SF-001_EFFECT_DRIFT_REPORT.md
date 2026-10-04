# SF-001_EFFECT_DRIFT_REPORT — Phase 1

> 只读诊断。未接线: 本报告不改变任何权限判定。

## 汇总

```
TOTAL_TOOLS              146
DRIFT (resolved != 引擎所见) 146
  其中 resolved=UNKNOWN   138
  其中 LEGACY_MAPPED      8
CURRENT_ALLOW            146
CURRENT_DENY             0
CURRENT_ABSTAIN          0
```

## 读法

`engine_context_effect` 恒为 `read`: `_build_context` 对 146 个已注册
master 工具无一例外产出 `read`(它硬编码为 `write` 的三个名字
`file.write`/`file.patch`/`artifact.write` 都不在 registry 里)。

因此 drift 列即「真实 effect 与引擎所见不一致」的全量。

## 两类漂移

### A. 已知的真实降级(LEGACY_MAPPED, confidence 0.5)

前缀表命中,effect 可信但来源是名称推断,不能当权威:

| tool | resolved | 引擎所见 | 当前判定 |
|---|---|---|---|
| `edit_diagram` | WRITE | read | ALLOW |
| `edit_hashline` | WRITE | read | ALLOW |
| `fetch_url` | READ | read | ALLOW |
| `list_files` | READ | read | ALLOW |
| `read_file_ast` | READ | read | ALLOW |
| `read_hashline` | READ | read | ALLOW |
| `search_genesis_ledger` | READ | read | ALLOW |
| `write_file` | WRITE | read | ALLOW |

`write_file` / `edit_hashline` / `edit_diagram` 是**可证明的降级**:
它们确实写文件,引擎却按只读判定为 ALLOW。

### B. 无知的漂移(UNKNOWN, confidence 0.0)

138 个工具没有任何可信 effect 来源。它们不是「remote」,
是**未知**。含 `github_pr_create_draft`、`github_pr_post_review`、
`skill_delete`、`memory_forget`、`team_shutdown_request` 等。

这些当前全部 ALLOW。Stage D 会把它们改为 fail-closed;
Phase 2 的逐工具分类负责把其中能确定的部分转成 DECLARED。

## 全量漂移清单

| tool | resolved_effect | provenance | conf | 引擎所见 | 判定 | drift |
|---|---|---|---|---|---|---|
| `edit_diagram` | WRITE | LEGACY_MAPPED | 0.5 | read | ALLOW | ✓ |
| `edit_hashline` | WRITE | LEGACY_MAPPED | 0.5 | read | ALLOW | ✓ |
| `fetch_url` | READ | LEGACY_MAPPED | 0.5 | read | ALLOW | ✓ |
| `list_files` | READ | LEGACY_MAPPED | 0.5 | read | ALLOW | ✓ |
| `read_file_ast` | READ | LEGACY_MAPPED | 0.5 | read | ALLOW | ✓ |
| `read_hashline` | READ | LEGACY_MAPPED | 0.5 | read | ALLOW | ✓ |
| `search_genesis_ledger` | READ | LEGACY_MAPPED | 0.5 | read | ALLOW | ✓ |
| `write_file` | WRITE | LEGACY_MAPPED | 0.5 | read | ALLOW | ✓ |
| `agent_loop_run` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `ask_user` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `assemble_code_context` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `ast_grep_rewrite` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `ast_grep_search` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `browser_run` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `code_blast_radius` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `coding_apply_patch` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `coding_build` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `coding_diff` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `coding_discard` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `coding_finalize_patch` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `coding_run_command` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `coding_run_lint` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `coding_run_tests` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `coding_run_typecheck` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `coding_task_run` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `coding_workspace_detect` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `coding_worktree_create` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `coding_worktree_status` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `decision_query` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `decision_record` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `delegate_to_genesis` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `display_diagram` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `evolve_solution` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `get_market_data_schema` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `github_issue_fetch` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `github_issue_fix_prepare` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `github_pr_create_draft` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `github_pr_fetch` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `github_pr_post_review` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `github_pr_review_prepare` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `goal_add_todo` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `goal_start` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `goal_status` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `graph_query` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `graph_store` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `grep` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `harness_guides_load` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `harness_guides_search` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `harness_guides_show` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `harness_performance_query` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `harness_ratchet_apply` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `harness_ratchet_approve` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `harness_ratchet_candidates` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `harness_ratchet_reject` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `harness_sensor_list` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `harness_sensor_report` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `harness_sensor_run` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `learning_candidate_create` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `learning_eval` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `learning_rollback` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `memory_correct` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `memory_explain` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `memory_forget` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `memory_get` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `memory_recall_project_lessons` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `memory_search` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `memory_show_source` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `memory_supersede` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `memory_write` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `produce_wechat_article` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `run_backtest_coprocessor` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `run_in_sandbox` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `runtime_calls_ingest` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `runtime_calls_query` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `skill_confirm` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `skill_create` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `skill_delete` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `skill_deprecate` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `skill_rollback` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `skill_run` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `skill_search` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `skill_show` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `skill_update` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `stateful_current` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `stateful_goto` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `stateful_history` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `stateful_start` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `system_boundary_scan` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `system_gate_check` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `system_graph_cycle` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `system_graph_review` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `system_quota_should_run` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `system_quota_spend_slot` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `system_terminal_gate_check` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `system_todo_claim` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `team_approve_shutdown` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `team_create` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `team_delete` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `team_list` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `team_read_messages` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `team_reject_shutdown` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `team_send_message` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `team_shutdown_request` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `team_status` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `team_task_create` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `team_task_get` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `team_task_list` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `team_task_update` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `tool_search` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `veya_escalation_list` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `veya_events` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `veya_mission_cancel` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `veya_mission_continue` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `veya_mission_create` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `veya_mission_inspect` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `veya_mission_run` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `veya_mission_set_mode` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `veya_report_get` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `veya_report_latest` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `veya_review_apply` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `veya_reviews` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `wayfind_add_fog` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `wayfind_add_ticket` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `wayfind_chart` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `wayfind_claim` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `wayfind_compile_runbook` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `wayfind_complete` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `wayfind_decisions` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `wayfind_frontier` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `wayfind_gh_add_fog` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `wayfind_gh_add_ticket` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `wayfind_gh_chart` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `wayfind_gh_claim` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `wayfind_gh_complete` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `wayfind_gh_decisions` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `wayfind_gh_frontier` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `wayfind_gh_graduate_fog` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `wayfind_gh_resolve` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `wayfind_gh_rule_out_of_scope` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `wayfind_gh_wire_blocking` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `wayfind_graduate_fog` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `wayfind_resolve` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `wayfind_rule_out_of_scope` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `wayfind_to_spec` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `wayfind_wire_blocking` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |
| `wechat_discover` | UNKNOWN | UNKNOWN | 0.0 | read | ALLOW | ✓ |

_共 146 行。清单同时说明一件更重要的事: 其中 138 行的
resolved_effect 是 UNKNOWN,也就是说修复不能靠「把 remote 传下去」——
必须先让工具有声明。_