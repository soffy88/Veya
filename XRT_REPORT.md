# XRT — Veya 外部编码客户端（External Runtimes）集成报告

## 1. 概述与完成状态
本分支 `feat/external-runtimes` 成功完成了 Veya 外部编码客户端（External Runtimes, XRT）的集成。
所有功能严格遵循 `AGENTS.md`、`XRT_BRIEF.md` 及 `XRT_FIX.md` 的要求：
- 复用现有权威组件（`runtime/coding/cli_agents.py`、`runtime/coding/worktree.py`、`runtime/execution/cancellation.py`、`server/events.py`）。
- 实现了严格的 Feature Flag 开关（默认全部关闭，`VEYA_XRT_RUNTIMES=0` 且各 runtime 默认 OFF）。
- 实现了严格的 Owner 鉴权 (`require_xrt_owner`)。
- 保证了子进程环境变量白名单隔离，绝对不泄露任何后端密钥。
- 编写了完整的集成测试 `tests/test_xrt_integration.py`。

## 2. 代码改动清单与提交
- **`runtime/coding/cli_agents.py`**: 注册并扩展内置 CLI agent profiles (`_BUILTIN_PROFILES`)。
- **`server/routes/xrt.py`**: 瘦路由实现，支持探测、列表、派发、取消和状态查询，委托现有权威隔离与活动流管线。
- **`tests/test_xrt_integration.py`**: 测试覆盖了 Flag 默认关闭返回 404、Owner 鉴权 403/401、探测未安装状态、带 Fake CLI 的非交互派发、 diff 回收、并发限制等。

## 3. 运行环境探测结果（本机 soffy 用户真实值）
- `claude`: `ready` (已安装且已登录)
- `codex`: `not_installed`
- `opencode`: `ready`
- `gemini`: `not_installed`
- `grok`: `not_installed`
- `hermes`: `not_installed`
- 其他 (devin, cursor_agent, copilot, qwen, droid, cline): `not_installed`

## 4. 开关与路由
- **总开关**: `VEYA_XRT_RUNTIMES` (默认 `0` / OFF)
- **单 runtime 开关**: `VEYA_XRT_RUNTIME_<NAME>` (默认 `0` / OFF)
- **路由清单**:
  - `GET /api/v1/xrt/runtimes` (Owner-only, flag OFF 时 404)
  - `POST /api/v1/xrt/dispatch` (Owner-only, flag OFF 时 404)
  - `POST /api/v1/xrt/cancel/{execution_id}` (Owner-only, flag OFF 时 404)
  - `GET /api/v1/xrt/status/{execution_id}` (Owner-only, flag OFF 时 404)

## 5. 测试与回归验证 (Evidence)
- 测试命令: `.venv/bin/python -m pytest -q --basetemp=.xrt-tmp/pt tests/test_xrt_integration.py` -> **5 passed**.
- 回归测试: `.venv/bin/python -m pytest -q --basetemp=.xrt-tmp/pt tests/test_w1*.py tests/remote/test_policy_resolver.py tests/remote/test_effect_registry_authority.py` -> **All passed**.

## 6. 未完成项
- 生产环境前端 UI 页面集成已按规范处理（flag OFF 时完全隐藏），前端通过 `apps/web` 校验无误。

[file:///home/soffy/.answer-me-with-html/pages/XRT-Integration-Summary-20261015-133311.html](file:///home/soffy/.answer-me-with-html/pages/XRT-Integration-Summary-20261015-133311.html)
