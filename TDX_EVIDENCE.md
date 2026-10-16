# TDX 证据门禁（合入前复核，2026-10-16）

树：`5f88fdf4`（已合 main d7c2a383，含 SEC/XRT/GSK）。每条结论在合并后的树上重新执行，结果写入 GSK EvidenceLedger（`veya/gsk/evidence.py`，账本 /data/soffy/tmp/tdx/ev/tdx_evidence_ledger.jsonl）。gstack-evidence/gstack-review CLI 在本机不存在，故以此账本替代。

| 报告结论 | commit | 结果 | 证据 |
|---|---|---|---|
| 2.1 owner-only + flags OFF | 92e8935f | PASS | ======================== 3 passed, 5 warnings in 4.50s ========================= / ledger=FRESH |
| 2.2 publish perms enforced/persisted/audited | 9a151557,ac4df7ca | PASS | ======================== 5 passed, 5 warnings in 4.12s ========================= / ledger=FRESH |
| 2.3 needs-you from real approvals/events | e228eddb | PASS | ======================== 2 passed, 5 warnings in 4.09s ========================= / ledger=FRESH |
| 2.4 run history from canonical events | e228eddb | PASS | ======================== 4 passed, 5 warnings in 4.29s ========================= / ledger=FRESH |
| 2.5 lorca objective (TDX turns.py change DROPPED; main order kept) | 49cd11ba(dropped) | PASS | ======================== 3 passed, 5 warnings in 3.01s ========================= / ledger=FRESH |
| flags default OFF (3) | - | PASS | count=3 |
| owner dep on every tdx route | - | PASS | Depends(require_owner) x6; routes=6 |
| no new model-callable tool | - | PASS | new register calls=0 |

## 对报告的更正
- 报告第 4 节回归数字基于旧基线 c1364460，已作废；以合入前全量回归为准（见下）。
- 合入 SEC 后 TDX 测试曾 11/13 失败（全局鉴权中间件 + main 的 terminal_action_gate）；已改测试（4d02c7d1 等），未改生产鉴权。
- 第 2.5 节 turns.py 改动未合入：在 main 上 Lorca 用例已通过，保留 main 的提示顺序。
- 第 2.2 节：在 main 上 git_push 首先被 terminal_action_gate 拦截，TDX 发布权限是第二道闸（flag 开且 allowlist 放行后才生效）。

GATE: PASS

## 全量回归（合并后树，vreg2 同基线测试集 + tests/test_tdx_*.py）
44 failed / 2881 passed / 16 skipped；与 SEC 基线 46 failed 相比新增失败 0（少 2 个为 p0j/p0k 环境项）。日志 /data/soffy/tmp/tdx/reg/tdx.log。

## 前端门禁
apps/web: pnpm check 0 errors / 32 warnings，pnpm build 成功（合 GSK 前的树，TDX 前端文件此后未变）。日志 /data/soffy/tmp/tdx/web.log。
