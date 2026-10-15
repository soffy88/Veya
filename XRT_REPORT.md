# XRT 外部编码客户端集成报告

日期：2026-10-15  
工作树：`/data/soffy/projects/veya-xrt`  
分支：`feat/external-runtimes`

## 结论

XRT 功能实现和专项验证已完成：12 个 runtime profile、受开关约束的 adapter 注册、owner-only REST 路由、隔离 worktree 执行、流式事件、diff/产物回收、超时/取消/并发限制以及 Remote 页面的增量面板均已落地。最终专项测试 `tests/test_xrt_*.py` 为 **14 passed**；`pnpm check` 为 **0 errors**，`pnpm build` 成功。

整体回归并非全绿，不能宣称所有验收都无条件通过：ACP 回归有 1 项失败，Lorca 回归有 3 项失败；大范围 W1/Nano/Lorca 合并测试未在 Local2 单次命令时限内完成。详情和证据 ID 见“验证结果”。这些失败没有通过修改无关模块或降低现有 ACP 权威语义来掩盖。

## R3 清单逐项交付

| 项目 | 状态、提交与文件 | 测试 / 新鲜证据 |
|---|---|---|
| 1. 12 个表驱动 profiles，复用 `CLIAgentRegistry` / `CLIWorkerExecutorAdapter`，总开关 OFF 时不注册新增 adapters | 完成。`2b32bc49`：`runtime/coding/cli_agents.py`、`veya/remote/executor_adapter.py`、`veya/remote/executor_adapters.py`。`6e10efab` 优化所有 runtime 的共享发现缓存。 | `test_xrt_profiles_cover_all_twelve_cli_runtimes`、`test_xrt_extra_adapters_are_opt_in_and_per_runtime`。Local2 fresh test run：`direct_e7ae4849e6cb4215a2da41fee7c48667`，14 passed。gstack-evidence 标签：本机未安装 `gstack-evidence` CLI；使用可查询的 Local2 新鲜运行证据。 |
| 2. XRT 路由改为薄委托，执行复用 worktree、共享 CLI worker adapter 和 cancellation | 完成。`eb9eac16`：`server/routes/xrt.py`（74 行）、新增 `veya/remote/xrt_runtime.py`。 | `test_fake_cli_dispatch_collects_worktree_diff_and_stream`、`test_fake_cli_timeout_and_cancel`、`test_fake_cli_concurrency_limit_is_enforced`。同一专项运行 `direct_e7ae4849e6cb4215a2da41fee7c48667`。gstack-evidence 标签：CLI 不可用；Local2 run ID 如左。 |
| 3. 子进程环境白名单、后端密钥不透传、活动流及产物回收 | 完成。`0a4a1c33`：`veya/remote/executor_adapters.py`、`veya/remote/xrt_runtime.py`、两份 XRT 测试；`b3cfb529` 将 CLI 版本探测超时限制为 2 秒；`6e10efab` 让 12 个 runtime 共用 registry 缓存。使用 `PATH`、`HOME`、`LANG`、`LC_*`、`TERM`、`USER` 和显式 runtime 配置目录变量，不读取凭据文件内容。每次成功完成会注册 `outputs/worktree.diff` 与 `evidence/execution_result.json` 并写 manifest。 | `test_safe_runtime_env_excludes_backend_secrets`、`test_fake_cli_dispatch_collects_worktree_diff_and_stream`、`test_fake_cli_nonzero_exit_is_reported_as_failed`。Local2 fresh evidence：`direct_e7ae4849e6cb4215a2da41fee7c48667`。gstack-evidence 标签：CLI 不可用；未伪造 gstack 证据。 |
| 4. 移除强制登录状态后门 | 完成。`eb9eac16` 重写原 XRT 路由；`runtime/coding/cli_agents.py` 的登录状态仅根据凭据文件存在性或状态命令退出码判断，不读取内容。 | 对 `runtime/`、`server/`、`veya/`、`apps/`、`tests/` 中 Python/Svelte 源文件做过字符串检查，未发现该后门变量的引用。该项为静态检查，无独立 pytest 名称。gstack-evidence 标签：CLI 不可用；静态检查已执行。 |
| 5. 恢复 Remote 原页面，并增量挂载外部客户端面板 | 完成。`8d6b4a0a`：新增 `apps/web/src/lib/components/XrtExternalRuntimes.svelte`，仅向 `apps/web/src/routes/remote/+page.svelte` 增加 2 行导入/挂载。与 `main` 比较该页面为 **2 insertions、0 deletions**。 | `cd apps/web && pnpm check`：退出码 0，0 errors、32 warnings；证据 `direct_ef352204c19d4313a020039ff753bc05`。`pnpm build`：退出码 0；证据 `direct_6e112b05f5eb4f9680fe9852966bcbc4`。gstack-evidence 标签：CLI 不可用；附 Local2 命令结果。 |
| 6. CLI Agents 与 XRT 所有路由使用现有 owner 校验 | 完成。`ec5a7ee9`：`server/routes/cli_agents.py`；XRT 路由 `eb9eac16`：`server/routes/xrt.py`。复用 `require_tailnet_owner`，未创建自定义认证头或认证体系。 | `test_xrt_disabled_returns_404_before_auth`、`test_xrt_no_token_returns_401_when_enabled`、`test_xrt_non_owner_returns_403`、`test_cli_agent_routes_are_owner_only`。最终 XRT 专项运行 `direct_e7ae4849e6cb4215a2da41fee7c48667`，14 passed。 |
| 7. fake CLI 与要求的回归 | XRT 专项覆盖成功、非零退出码、未安装、未登录、环境密钥过滤、diff/manifest、事件输出、超时、取消、并发限制、全局/单 runtime 开关和 HTTP 认证。`0a4a1c33`、`50c9953f`：`tests/test_xrt_integration.py`、`tests/test_xrt_runtime.py`。 | XRT 专项：`direct_e7ae4849e6cb4215a2da41fee7c48667`，**14 passed**。回归中存在失败/超时，见下节；不能据此宣称全部仓库回归通过。 |

## 开关与 REST API

- 总开关：`VEYA_XRT_RUNTIMES`，未设置时默认为关闭（`0`）。支持 `0/off`、`1/all`，或以逗号列出允许的 runtime ID，例如 `claude_code,opencode`。可用 `VEYA_XRT_RUNTIME_<RUNTIME_ID_UPPER>=0` 单独禁用某项；全局关闭时路由返回 404。默认并发限制为全局 2、每个 runtime 1，可用 `VEYA_XRT_MAX_CONCURRENT` 与 `VEYA_XRT_MAX_PER_RUNTIME` 配置。
- `GET /api/v1/xrt/runtimes`：探测状态/版本/登录态。
- `POST /api/v1/xrt/dispatch`：创建独立 worktree 并派发任务。
- `POST /api/v1/xrt/cancel/{execution_id}`：取消运行中的任务。
- `GET /api/v1/xrt/status/{execution_id}`：读取状态、退出码、输出、diff 和产物清单。
- 上述路由都由 `require_tailnet_owner` 保护；关闭时 404、未认证 401、非 owner 403 均有自动化测试。`server/app.py` 已注册两个 router，见任务提交 `eb9eac16`、`ec5a7ee9`。
- 新增模型可调用工具：无。

## 本机 runtime 探测（2026-10-15）

只执行安装/版本和登录状态探测；未发起任务调用、未读取或输出凭据内容。

| Runtime ID | 实际探测状态 | 版本（如可得） |
|---|---|---|
| `devin` | `not_installed` | — |
| `claude_code` | `ready` | `2.1.214 (Claude Code)` |
| `codex` | `ready` | `codex-cli 0.157.1` |
| `cursor_agent` | `not_installed` | — |
| `gemini` | `installed_not_logged_in` | 未能取得版本 |
| `copilot` | `not_installed` | — |
| `qwen` | `not_installed` | — |
| `droid` | `not_installed` | — |
| `cline` | `ready` | `3.0.62` |
| `opencode` | `ready` | `1.18.35` |
| `grok` | `ready` | `1.0.41 (4220f3b224a6)` |
| `hermes` | `installed_not_logged_in` | 未能取得版本 |

这次 12 项探测共享 registry 缓存后约 4.26 秒完成；对应性能提交 `6e10efab`。运行仅使用真实 CLI 的版本或登录状态探测，没有调用真实模型或付费订阅任务。

## 验证结果及未完成项

| 测试/检查 | 结果与证据 |
|---|---|
| `TMPDIR="$PWD/.xrt-tmp" .venv/bin/python -m pytest -q tests/test_xrt_*.py --basetemp=.xrt-tmp/pt` | **14 passed**；新鲜 Local2 `test.run` ID：`direct_e7ae4849e6cb4215a2da41fee7c48667`。 |
| `cd apps/web && pnpm check` | 退出码 0；0 errors、32 warnings（现有项目警告）；Local2 execution ID：`direct_ef352204c19d4313a020039ff753bc05`。 |
| `cd apps/web && pnpm build` | 退出码 0；Local2 execution ID：`direct_6e112b05f5eb4f9680fe9852966bcbc4`。 |
| `tests/remote/test_acp_executor_adapter.py tests/remote/test_policy_resolver.py tests/remote/test_effect_registry_authority.py` | **52 passed、1 failed**；`test_probe_acp_manifest_ready` 期望 `READY`，实际 `UNAVAILABLE`。`veya/remote/execution_contract.py` 与 `main` 相同，本任务没有修改 ACP 能力/可达性权威，未通过强行放宽该语义来掩盖失败。Local2 execution ID：`direct_28670f8269c942bbbd88bcbecba39f8a`。 |
| Nano 子集 | `test_nano_w10_a.py` **69 passed**；`test_nano_w10_b.py` **68 passed**；`test_nano_w10_c.py` + `test_nano_w10_d.py` **97 passed**。Local2 execution IDs 分别为 `direct_b9b753c499ec495b95440cca7835cc8e`、`direct_a498173fc80541cf8a36676df732a3c4`、`direct_4f24d27db37548fa9e78490bc03743fb`。 |
| W11/W12 子集 | `tests/test_w11*.py tests/test_w12*.py` **71 passed**；Local2 execution ID：`direct_4f24d27db37548fa9e78490bc03743fb`。`test_w11_c_side_effect_approval.py` 单独复测 **5 passed**。 |
| Lorca 子集 | `tests/test_lorca_*.py` **153 passed、3 failed**：`test_bot_turn_carries_its_bot_into_the_canonical_request`、`test_local_turn_is_unchanged_by_lorca`、`test_runner_turn_endpoint_executes_the_hubs_bot_locally`。失败均为预期英文回包与实际默认中文指令回包不一致；本任务未修改这些测试或其业务文件。Local2 execution ID：`direct_b07023cecfce402a81e4a1c17205b293`。 |
| 合并回归尝试 | W1/Nano/Lorca 合并执行在 Local2 90 秒命令限时内未完成（运行到 W13 测试附近）；完整 Nano 集合也在 80 秒限时内未完成。已对较慢 Nano 模块分开执行并记录上表，不把超时记作通过。 |
| `gstack-evidence run` | 当前 PATH 中未找到 `gstack-evidence` 可执行文件，无法生成其原生证据标签。没有编造此类记录；以上列出可复核的 Local2 新鲜 execution/test-run ID。 |

## 安全与交付边界

未 push、未重启服务或容器、未调用真实模型任务、未改工作树外文件。Remote 原页面没有删除行。源代码检索未发现强制登录后门残留。实现提交按 R3 分块完成；报告写成后不再修改源代码。
