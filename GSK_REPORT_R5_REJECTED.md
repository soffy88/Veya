# GSK R4 实施与验证报告

日期：2026-10-15  
工作树：`/data/soffy/projects/veya-gsk`  
分支：`feat/gstack-borrow`

## 结论

R4 指定的回归修复、四项真实调用接线及对应定向测试已完成。要求的测试集最终结果为 **69 passed, 0 failed, 5 warnings**；`git diff --check main...HEAD` 通过。`veya/server/app.py` 相对 `main` 的 diff 为空，原治理 MCP 路由保留；`THIRD_PARTY_NOTICES.md` 相对 `main` 仅增加 32 行、删除 0 行。

五条警告来自现有 pgmpy 弃用提示和用户技能脚本的 Python 字符串转义警告，不是测试失败。

## 变更、提交、测试与 gstack-evidence

| 项目 | 已提交 SHA | 主要文件 | 实测测试 / 检查 | 最终 gstack-evidence 标签 |
|---|---|---|---|---|
| 恢复治理 MCP 路由，移除 legacy gateway 中重复的 GSK 路由注册 | `a2466151cdecae831315d60cd46a75d392d7bf0d`, `db044b3dd2574b13322bba3deb9970c57d55177f` | `veya/server/app.py`, `tests/test_gsk_mcp_mount.py` | `test_governed_mcp_router_is_mounted_by_legacy_gateway`；并核对 `git diff main -- veya/server/app.py` 为空 | `gsk-r4-auth-mcp` — **FRESH** |
| direct-exec Careful/Freeze 守卫改为 flag opt-in，关闭时不改变普通命令规划 | `a655bc84037b36cb0c4996d00244ebf2aec527fd` | `veya/remote/direct_exec.py`, `veya/gsk/careful.py`, `tests/test_gsk_careful.py` | `test_direct_exec_guard_wiring_is_opt_in`：flag OFF 正常规划；flag ON 拒绝危险命令和强制推送；Freeze 外路径拒绝 | `gsk-r4-guard-envelope` — **FRESH** |
| 恢复第三方声明原文并追加 gstack MIT 归属与许可证 | `5221cef6c466b4da624a5dea94aaaea3c14be867` | `THIRD_PARTY_NOTICES.md` | `git diff --numstat main -- THIRD_PARTY_NOTICES.md` → `32 0`；`git diff --check main...HEAD` 通过 | `gsk-r4-required` — **FRESH**；声明文件另以 diff 统计核验 |
| GSK receipts/evidence API 使用仓库现有 Tailnet owner 认证，不接受伪造身份头 | `1c56fd6e28c5148371f607cb1b2e486a91a57297`, `db044b3dd2574b13322bba3deb9970c57d55177f`, `3980c0e1c2149677c4504db0ea62284eef62e82f` | `server/routes/gsk.py`, `veya/server/routes/gsk.py`, `tests/test_gsk_routes.py` | `test_gsk_routes_feature_flags_are_hidden_when_off`, `test_gsk_receipts_requires_real_session_and_owner`, `test_gsk_evidence_requires_real_session_and_owner`, `test_spoofed_identity_headers_do_not_grant_gsk_access`；覆盖 OFF→404、未登录→401、非 owner→403、owner→200 | `gsk-r4-auth-mcp` — **FRESH** |
| 信任信封接入浏览器及渠道读取文本入口，默认关闭 | `12049f07c481458e08bedb4981caa8f41f9b6fbd` | `server/channel_tools.py`, `server/dom_reader_tools.py`, `veya/gsk/envelope.py`, `tests/test_gsk_envelope.py` | `test_read_channel_applies_envelope_only_when_enabled`, `test_read_browser_applies_envelope_only_when_enabled` | `gsk-r4-guard-envelope` — **FRESH** |
| 渠道外发增加 attempt / delivered / failed 哈希链回执，并锚定至现有执行事件流 | `3cbbfa01c8b76c5e437c4171c37b0bd69c9f07d3`, `2f0c47ad38562a7d947478867b8297ef15b10242`, `463c430ac0fab25ca1a4821cfbe033d957b3aeb2` | `server/channels/dispatch.py`, `veya/gsk/receipts.py`, `veya/remote/execution_events.py`, `tests/test_gsk_receipts.py` | `test_egress_receipts_chain_and_verify`, `test_channel_delivery_records_metadata_only_when_enabled`, `test_egress_receipts_detect_deleted_tail_against_event_anchor`, `test_failed_channel_delivery_records_failed_receipt` | `gsk-r4-egress` — **FRESH** |
| direct-exec 测试/构建命令的证据记录接入执行事件流；只写脱敏命令、退出码、摘要与工作区指纹 | `d9085030c11d594bbb2b827ecf62e02acfaf84e7`, `6abb0d79bf89d472bcf6a4708b7028a9186b48df`, `463c430ac0fab25ca1a4821cfbe033d957b3aeb2` | `veya/remote/direct_exec.py`, `veya/gsk/evidence.py`, `veya/remote/execution_events.py`, `tests/test_gsk_evidence.py` | `test_direct_execution_records_evidence_only_when_enabled`, `test_evidence_ledger_lifecycle` | `gsk-r4-skills-evidence`、`gsk-r4-required` — **FRESH** |
| 站点技能存为现有技能树中的被动 `SKILL.md` 文件，注入既有 worker context，连续 3 次成功后激活 | `4e7d87c7e870c6a2af355c1900a986cac16e7638`, `42ba407b64d063c27713164c764a00d7a0dc37b3` | `veya/gsk/domain_skills.py`, `veya/remote/tool_adapter.py`, `tests/test_gsk_domain_skill.py` | `test_domain_skills_quarantine_and_activation`, `test_domain_skill_injects_into_existing_execution_context_only_when_enabled`, `test_three_successful_domain_tasks_activate_quarantined_note`, `test_domain_notes_use_existing_skills_tree_without_callable_manifest` | `gsk-r4-skills-evidence` — **FRESH** |
| 修整 GSK 文件尾随空格 | `4022a4de9b2079081fcf2fc7c6910e559ae426ab` | GSK Python 实现及测试文件 | `git diff --check main...HEAD` | `gsk-r4-required` — **FRESH** |

所有上表中的 SHA 均为该 worktree 中实际存在的提交。`gstack-evidence` 标签已在报告提交前后以当前实现对应的测试选择运行，并在最后通过 `check --max-age 24` 验证为 FRESH。完整标签输出位于 gstack 本机证据账本及其日志；未修改 `.ref/gstack`。

## 最终强制测试

执行的规格命令：

```bash
.venv/bin/python -m pytest -q tests/test_gsk_*.py tests/remote/test_policy_resolver.py tests/remote/test_effect_registry_authority.py --basetemp=.gsk-tmp/pt
```

结果：`69 passed, 5 warnings in 8.31s`，退出码 `0`。原始输出保存在 `.gsk-tmp/gsk-required-final.log`；对应新鲜证据标签为 `gsk-r4-required`。通过 gstack-evidence 执行相同的 10 个测试文件时，因 runner 不展开 glob，命令显式列出文件；同样结果为 `69 passed`，`tests_ran=69`，标签检查为 **FRESH**。

定向证据标签结果：

| 标签 | 结果 | `tests_ran` |
|---|---:|---:|
| `gsk-r4-auth-mcp` | FRESH | 6 |
| `gsk-r4-guard-envelope` | FRESH | 6 |
| `gsk-r4-egress` | FRESH | 4 |
| `gsk-r4-skills-evidence` | FRESH | 6 |
| `gsk-r4-required` | FRESH | 69 |

## 额外回归结果与限制

另运行一组广泛回归（排除卡住的 `tests/test_canonical_proxies.py`），结果为 **1124 passed, 6 skipped, 3 failed, 26 warnings**。三个失败随后单独重跑仍失败：

- `tests/test_lorca_p1_bots.py::test_bot_turn_carries_its_bot_into_the_canonical_request`
- `tests/test_lorca_p6_runners.py::test_local_turn_is_unchanged_by_lorca`
- `tests/test_lorca_p6_runners.py::test_runner_turn_endpoint_executes_the_hubs_bot_locally`

它们共同表现为断言期待 `done:summarise three sources`，实际收到 `done:请使用中文（与用户消息或回忆的语言保持一致）简洁`。失败触及 Lorca bot/request 回复路径，不在本次 GSK 改动文件内；由于未另建干净 `main` worktree 作基线比较，本报告不将其定性为已证明的基线失败，也不声称该广泛回归全绿。原始日志为 `.gsk-tmp/r4-regression-clean.log`，隔离复跑日志为 `.gsk-tmp/r4-lorca-isolated.log`。

## 安全边界与最终状态

- GSK 功能 flag 默认关闭；开启后才执行相应信任信封、外发回执、证据记录或站点技能行为。
- 回执与证据的默认数据位置在 Veya 用户数据目录下，路径可由环境变量覆写；测试数据使用 `tmp_path`，未在仓库根建立 `.gsk-data` 或 `.gsk-evidence`。
- `veya/server/app.py` 相对 `main` 无 diff；第三方声明只追加。
- 未 push、未重启服务；未删除工作区外文件。
- 报告写入并提交后，不再修改代码或文档。
