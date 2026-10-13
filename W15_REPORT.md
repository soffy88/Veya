# W15 报告 — 角色与技能预设包

分支 `fix/fixall-w15`（fixall 2026-10-14）。规格：`W15_PRESETS_SPEC.md`。

## 做了什么
- flag `VEYA_ROLE_SKILL_PRESETS`（`server/feature_flags.py` 末尾追加，默认 False）。
- `scripts/import_w15_presets.py`：从 `/tmp/w15-upstream` 浅克隆生成预设包，校验上游 LICENSE 为 MIT。
- `config/presets/roles/*.md`：26 个角色（工程 9、测试 2、产品 3、设计 2、营销 3、运营 3、研究 2、写作 2），英文原文 + 来源头（上游路径+commit、license、copyright、`summary_zh` 中文简介）。
- `config/presets/skills/<id>/`：agent-skills 全部 25 个技能，只导入 `.md`（上游 `idea-refine/scripts/*.sh` 未入库）。
- `server/role_skill_presets.py`：加载 + 纯函数 `apply_to_spec`；`server/routes/role_skill_presets.py`：
  `GET /api/v1/presets/roles`、`GET /api/v1/presets/skills`（flag 关 `available:false`+原因）、`POST /api/v1/bots/{bot_id}/presets/apply`（登录 + 严格 owner，非 owner/不存在 404；flag 关 409；写回唯一 `BotRegistry`）。
- 前端：bot 设置页 `PresetPicker.svelte`（从模板选择角色 / 添加技能），flag 关时隐藏。
- `THIRD_PARTY_NOTICES.md`：两个上游的 MIT 全文 + 版权行。

## 怎么启用
设 `VEYA_ROLE_SKILL_PRESETS=1`（env / 配置文件 / runtime 任一层）并重启 backend。

## 测试
- `tests/test_w15_presets.py`：14 passed。
- 回归 `tests/test_w1*.py tests/test_nano_*.py tests/test_lorca_*.py tests/test_orca_*.py tests/test_p3a_bot_isolation.py tests/test_product_shell.py`：944 passed, 6 skipped, 0 failed。
- gates.sh 对比基线：零新增（`server/role_skill_presets.py` 带 `# 3O-IO-ALLOW` 只读理由）。
- `svelte-check`：0 error（32 warning 均为既有）；`vite build` 通过。

## 新增模型可调用工具
无。

## 假设 / 缺口
- 角色写入 instructions 时整体 ≤ 4000 字符（现有 bot create/patch 请求模型上限），长模板会截断并附完整模板 URL。
- 共享/系统 bot（default、Chef、无 owner）不允许通过预设修改（比 PATCH 更严）。
- `available_skill_ids` 目前没有运行时消费者：技能已挂到 bot 字段，但**还不会进入 bot prompt**；要生效需改 bot 运行时（`build_objective` 或技能加载处），本轮未做。
- 建 bot 弹窗（NewBotSheet）里没有加入口：先建 bot，再在设置页选模板。
