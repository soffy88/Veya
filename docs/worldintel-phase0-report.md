# world-intel-mcp Phase 0 集成报告

- 日期：2026-10-21
- 分支：`feat/worldintel-phase0`
- 基线：`main`（`19712dd8`）
- 范围：默认关闭、仅惰性发现的 world-intel-mcp 集成

## 一、改了什么

新增 `server/world_intel_mcp.py`，按 `server/mcp_presets.py` 的装配方式启动
独立 stdio MCP 进程，并把上游 160 个工具收成一个既有网关形态的工具：

- 网关名：`mcp_world_intel`
- 动作名：`mcp_world_intel_<upstream-name-without-intel_>`
- 副作用：`SideEffect.PURE_READ`
- 上游调用参数保持不变，网关只负责把 `action` 映射回真实 MCP 工具名
- 工具结果标注为不可信外部数据，只作事实参考

该网关只注册进既有 `MasterToolRegistry`，**不加入常驻工具集**。模型通过
`search_tools` 显式检索后才把 `mcp_world_intel` 追加到当前会话工具面；直接调用
已注册的网关名也会走既有惰性加载兜底。没有新增第二入口、语义路由或工具裁藏。

配套改动：

- `server/feature_flags.py`：登记 `VEYA_WORLD_INTEL_MCP`，默认 `False`
- `server/app.py`：启动时尝试接线，关闭时立即返回；退出时关闭子进程
- `tests/server/test_world_intel_mcp.py`：覆盖开关关闭逐字节不变、惰性注册、
  缺失根目录、启动失败降级、网关调用参数透传

## 二、环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `VEYA_WORLD_INTEL_MCP` | 关闭 | 设为 `1`/`true`/`on`/`yes` 才启动并注册 |
| `VEYA_WORLD_INTEL_ROOT` | `/data/soffy/projects/world-intel-mcp` | 上游仓库根目录；本机实际路径为 `~/projects/world-intel-mcp` |

启动命令优先使用 `<root>/.venv/bin/world-intel-mcp`，不存在时回退到
`python -m world_intel_mcp.server`。根目录缺失、二进制缺失或握手失败都只记录日志并
返回 0，不影响 Veya 启动。

## 三、测试结果

执行环境：`/data/soffy/projects/veya/venv/bin/python`，工作目录为隔离 worktree。

```text
python -m pytest -q \
  tests/server/test_world_intel_mcp.py \
  tests/test_w14_mcp_pack.py \
  tests/test_feature_flags.py \
  tests/server/test_wshobson_index.py \
  tests/server/test_bughunter_index.py \
  tests/test_lazy_tools.py \
  tests/server/test_server_boot_gate.py

72 passed, 5 warnings
```

其中新测试 6 条全部通过。开关关闭测试同时断言全局注册表 schema 的 JSON 字节与
接线前完全一致，并断言不会构造/启动客户端。

Ruff：

```text
ruff check server/world_intel_mcp.py tests/server/test_world_intel_mcp.py server/feature_flags.py
All checks passed!

ruff format --check server/world_intel_mcp.py tests/server/test_world_intel_mcp.py server/feature_flags.py
3 files already formatted
```

说明：`server/app.py` 在 `main` 上已有 2 条 `UP041` 和既有格式差异；本次只新增
启动/关闭接线块，未顺手修改这些无关问题。新增块本身通过格式检查，且
`tests/server/test_server_boot_gate.py` 12 条全部通过。

## 四、真实冒烟结果

依赖只安装在上游仓库自己的 `~/projects/world-intel-mcp/.venv`，未改 Veya 的虚拟环境。

```text
VEYA_WORLD_INTEL_MCP=1 \
VEYA_WORLD_INTEL_ROOT=/data/soffy/projects/world-intel-mcp \
python -m ...

registered 1
{"source": "usgs", "count": 1, "place": "194 km WNW of Port-Olry, Vanuatu"}
```

调用链为 `mcp_world_intel` 网关 → 上游 `intel_earthquakes`，免密 USGS 数据返回成功。
上游 `tools/list` 实际发现 160 个工具。

## 五、提交

- 实现提交：见本报告之后的 `feat: add opt-in lazy world-intel MCP integration`
- 本报告提交：`docs: add world-intel Phase 0 report`

## 六、回滚

运行时不设置或删除 `VEYA_WORLD_INTEL_MCP` 即完全关闭，重启后恢复原工具面。
代码层回滚为反向应用本分支的两个提交：

```bash
git revert <report-sha> <implementation-sha>
```

回滚不涉及数据库迁移、不修改上游仓库、不产生持久化状态。`main` 工作区未改动，
本分支未 push。

## 七、已知限制与下一步

- Phase 0 只暴露一个惰性网关，160 个上游工具不逐个进入主工具面；这是刻意的
  上下文保护。
- 网关动作目录压缩了描述，完整参数 schema 由上游 `tools/list` 提供，但当前
  Phase 0 不把 160 份完整 schema 注入模型上下文。
- 上游 `tools/list` 响应约 94 KiB，超过 asyncio 默认 64 KiB 行限制；接线代码在
  启动后提高该客户端的读取上限到 4 MiB。这是针对已验证的启动失败做的局部处理，
  未修改 3O 子库。
- 若后续需要让模型看到逐工具完整 schema，应先评估上下文预算和检索质量，再决定
  是否拆成领域网关；不要直接把 160 个工具加入常驻面。
- 上游可选密钥（ACLED、NASA FIRMS、EIA、FRED、OpenSky 等）仍未配置；免密工具
  已可用。
