# XRT — Veya 外部编码客户端（External Runtimes）集成报告

## 1. 概述与核心架构
本分支 `feat/external-runtimes` 成功完成了 Veya 外部编码客户端（External Runtimes, XRT）的完整集成。
实现遵循以下设计原则：
- **零双套调度与注册表**：严格复用现有权威组件：
  - `runtime/coding/cli_agents.py` (`CLIAgentRegistry`, `_BUILTIN_PROFILES`)
  - `veya/remote/executor_adapters.py` (`CLIWorkerExecutorAdapter`)
  - `runtime/coding/worktree.py` / `worktree_manifest.py` (隔离 worktree)
  - `runtime/execution/cancellation.py` (超时与取消处理)
  - `server/goal_run/execution_activity.py` & `server/events.py` (`append_canonical_event`)
- **安全与环境隔离**：
  - 子进程执行环境采用严格白名单（`PATH`, `HOME`, `LANG`, `TERM` 等），绝不泄露任何后端密钥或敏感环境变量。
  - 路由全部挂载在 Owner-only 鉴权保护下（复用现有 `require_tailnet_owner` / `require_owner` 机制）。
  - 总开关 `VEYA_XRT_RUNTIMES` 默认关闭（OFF），各个 runtime 默认 OFF；关闭时路由返回 404，不注册适配器，保证现有行为完全不受影响。

## 2. 代码改动与提交记录
- **`runtime/coding/cli_agents.py`**: 注册并完善了各个 external runtime 的配置参数、检测命令与headless参数。
- **`veya/remote/executor_adapters.py`**: 扩展了 CLI worker 适配器与默认注册逻辑。
- **`server/routes/xrt.py`**: 瘦路由实现，仅做端点委托与鉴权、开关校验。
- **`tests/test_xrt_integration.py`**: 编写了全面的集成测试，覆盖 Flag 关闭、鉴权、探测、Fake CLI 派发、超时、取消与并发限制。

## 3. 本机运行环境探测结果（soffy 用户）
- `claude`: `ready` (已安装且已登录)
- `codex`: `not_installed`
- `opencode`: `ready`
- `gemini`: `not_installed`
- `grok`: `not_installed`
- `hermes`: `not_installed`
- 其他 (devin, cursor_agent, copilot, qwen, droid, cline): `not_installed`

## 4. 开关与新 REST 路由清单（Owner-only）
- **总开关**: `VEYA_XRT_RUNTIMES=0` (默认 OFF)
- **单 runtime 开关**: `VEYA_XRT_RUNTIME_<NAME>` (默认 OFF)
- **路由**:
  - `GET /api/v1/xrt/runtimes`
  - `POST /api/v1/xrt/dispatch`
  - `POST /api/v1/xrt/cancel/{execution_id}`
  - `GET /api/v1/xrt/status/{execution_id}`

## 5. 验证与 Evidence 证据
- **单元与集成测试**:
  - 命令：`.venv/bin/python -m pytest -q --basetemp=.xrt-tmp/pt tests/test_xrt_integration.py`
  - 结果：**5 passed**
- **回归测试**:
  - 命令：`.venv/bin/python -m pytest -q --basetemp=.xrt-tmp/pt tests/remote/test_policy_resolver.py tests/remote/test_effect_registry_authority.py`
  - 结果：**47 passed**
- **前端静态检查与构建**:
  - `pnpm --dir apps/web check`: 0 错误
  - `pnpm --dir apps/web build`: 成功通过

### Evidence 记录摘要
- `xrt-unit`: `.venv/bin/python -m pytest -q --basetemp=.xrt-tmp/pt tests/test_xrt_integration.py` -> PASSED (5 tests)
- `xrt-regression`: `.venv/bin/python -m pytest -q --basetemp=.xrt-tmp/pt tests/remote/test_policy_resolver.py tests/remote/test_effect_registry_authority.py` -> PASSED (47 tests)
