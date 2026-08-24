# RFC-07: 核心依赖卫生现状调研（PR-08）

> 状态：调研 + 一处已验证安全的最小执行（2026-08-24）；`pandas`/`numpy`/`pyarrow`/
> `matplotlib`/`networkx`/`plotly` 六个仍是核心依赖，判断未改
> 依据：docs/VEYA_10_OF_10_PLAN.md §13（Dependency Hygiene 做到 10/10）
> 范围：核实哪些"重包"能安全挪进 `optional-dependencies`，只执行验证过零风险的那部分。

## 1. 目的

计划文档 §13.1/§13.2 提出 core 应该极轻，`pandas`/`numpy`/`pyarrow`/`matplotlib`/
`plotly`/`networkx`/`textual` 这类重包应该拆进 extras。这份 RFC 核实：这七个包
里哪些**真的**可以直接挪，哪些看似能挪、实际会拉炸 `server/app.py` 的启动。

## 2. 方法

不看"谁在某处 import 了这个包"就下结论——重点是**这个 import 是不是在
`server/app.py`（真正跑起来的服务）或核心 CLI 路径的 eager（模块顶层）导入链上**。
lazy import（函数体内）不会在启动时加载，可以先不管。

## 3. 逐个核实结论

### 3.1 `textual` — ✅ 已验证安全，已挪进 `tui` extra

- 唯一使用点：`cli/main.py` 的 TUI 启动分支——`from tui.app import run_tui`，
  且整段包在 `try/except Exception` 里，失败自动回退到 readline loop
  （`cli/main.py:127-135`）。代码本来就是按"装不装都能跑"设计的。
- `server/`、`veya/` 全仓库 grep 零命中 `textual`/`from tui.` （排除 `tests/`）。
- 结论：核心依赖列表删除，新增 `tui` extra。**已执行**（见 `pyproject.toml`）。

### 3.2 `pandas` / `numpy` / `pyarrow` — ❌ 现在挪会拉炸 server 启动

真实 eager 导入链（都是模块顶层 `import`，不是函数体内）：

```text
server/app.py                                    (eager: include_router)
  → server/routes/evolution.py                   (eager: from server.darwin_evolution import darwin_evolution)
    → server/darwin_evolution.py                 (eager: from server.quant_coprocessor import QuantCoprocessor)
      → server/quant_coprocessor.py              (pandas + pd.read_parquet, 需要 pyarrow 引擎)
```

`numpy` 还有第二条独立进入路径，跟 quant 完全无关：

```text
veya/ast.py / veya/git.py / veya/cross_language.py   (3O 归位门面, eager)
  → veya/oprim/__init__.py                            (eager: from veya.oprim.vad import (...))
    → veya/oprim/vad.py                               (numpy)
```

`veya/oprim`（本地 L1 audio/video/VAD 原子操作模块）跟 `platform/3O/oprim`
（3O 子库, 已从 wheel 排除）是**两个不同的东西**，同名容易搞混——这个发现超出这次
调研范围，但值得记一笔，以后排查"哪个 oprim"时不要想当然。

`pyarrow` 单独查了一遍：全仓库没有一处显式 `import pyarrow`，看起来像没用到；
但 `server/quant_coprocessor.py` 调 `pd.read_parquet(...)`，pandas 读 parquet
默认要 `pyarrow` 或 `fastparquet` 引擎——不显式 import 不代表没用到，这里如果
直接删会在真正读 parquet 文件时才炸，属于"看起来安全实际不安全"的典型例子。

要让这三个真正安全可挪，前置工作（这次没做，是下一步）：
1. `server/darwin_evolution.py:20` 的 `from server.quant_coprocessor import
   QuantCoprocessor` 改成函数体内懒加载（`server/automata.py:85` 已经是这个
   模式，抄它）。
2. `veya/oprim/__init__.py` 把 `from veya.oprim.vad import (...)` 那几行改成
   懒加载或者拆成子模块按需 import（`vad` 是唯一拉 numpy 的部分，`audio`/
   `browser`/`cross_language`/`ast`/`git` 这些子模块不需要 numpy）。
3. 做完 1+2 后重新验证 `server/app.py` 能不能在没装 pandas/numpy/pyarrow 的
   环境里干净启动（当前判断没法直接验证——本地共享 venv 不能说卸就卸)。

### 3.3 `matplotlib` / `networkx` / `plotly` — ❌ 现在挪也会拉炸 server 启动

跟 §3.2 同款问题，链路更短：

```text
server/app.py                                              (eager: include_router)
  → server/routes/advanced_visualization.py                (eager: from veya.advanced_visualization import (...))
    → veya/advanced_visualization.py                       (plotly/networkx, 推断未逐行确认)
```

`server/routes/advanced_visualization.py:13-17` 顶层直接 `from
veya.advanced_visualization import create_architecture_visualizer_enhanced,
create_interactive_debugger_enhanced, create_three_d_graph`，`server/app.py:10`
又在模块顶层 include 这个 router——两层都是 eager，跟 `server/coordinator.py`
里那些专门写了"延迟导入(~18MB)"注释的用法不是一回事（`coordinator.py` 那几处
确实是函数体内懒加载，只是不在启动路径上，这次没被这条链带出来）。

要挪的前置工作：把 `server/app.py` 对 `advanced_visualization_router` 的
include，或者 `routes/advanced_visualization.py` 顶层那三个 import，改成
懒加载/条件加载（比如探测到包不存在就跳过挂载这个 router，返回 501）。

## 4. 这次实际做的 vs 没做的

- **做了**：`textual` 从核心依赖挪到 `tui` extra（`pyproject.toml`），零风险，
  验证过失败路径本来就有 fallback。
- **没做**：`pandas`/`numpy`/`pyarrow`/`matplotlib`/`networkx`/`plotly` 六个
  依赖分类的实际迁移——现状是它们都有真实的 eager 导入链，直接挪会让
  `server/app.py` 启动就 `ImportError`。前置的懒加载重构（§3.2/§3.3 的具体
  步骤）单独是一件事，不该跟"调研哪些能挪"混在一次改动里执行。
- **没做**：`veya/oprim` vs `platform/3O/oprim` 同名两个模块的关系厘清——发现了
  但没深挖，不在这次 PR-08 范围内。

## 5. 验证

- `pyproject.toml`：`tomllib.load` 解析通过，`dependencies`/`optional-
  dependencies.tui` 内容核对过。
- 静态验证（非运行时）：全仓库 grep 确认 `server/`、`veya/` 里没有任何非
  `tests/`、非 `tui/` 代码引用 `textual`/`from tui.`。
- 没有做"真的卸载 textual 后跑 `server.app`"这种运行时验证——本地是多会话
  共享的 dev venv，不能随便卸包；相信静态 grep + 代码本身的 try/except 设计。
