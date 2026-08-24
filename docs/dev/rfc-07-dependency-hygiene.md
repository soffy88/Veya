# RFC-07: 核心依赖卫生现状调研（PR-08）

> 状态：全部 7 个包已分类完成并执行（2026-08-24，分三轮）。第一轮（§3.2/3.3 原始
> 版本）对 pandas/numpy/pyarrow/matplotlib/networkx/plotly 的判断有错，第二轮
> 用运行时 import 拦截重新核实并纠正——过程记在 §6，不删第一轮的错误结论，
> 留作方法论教训。第三轮（§7）补上了一处第二轮遗漏的真实回归：`veya start`
> 走 `veya/server/app.py` → `server/app.py`，后者对 matplotlib/networkx/plotly
> 是真 eager import——纯改 `pyproject.toml` 声明后, 用户跟着 README 走"pip
> install veya → veya start"三步会在第三步炸。已在 `server/app.py` 给两个
> 可视化路由加 guarded import 修掉。
> 依据：docs/VEYA_10_OF_10_PLAN.md §13（Dependency Hygiene 做到 10/10）
> 范围：核实哪些"重包"能安全挪进 `optional-dependencies`。

## 1. 目的

计划文档 §13.1/§13.2 提出 core 应该极轻，`pandas`/`numpy`/`pyarrow`/`matplotlib`/
`plotly`/`networkx`/`textual` 这类重包应该拆进 extras。这份 RFC 核实：这七个包
里哪些**真的**可以直接挪。

## 2. 方法（第二轮修正后的版本）

第一轮只用 grep 判断"这个 import 是不是在 eager 路径上"，犯了两个方法论错误
（§6 详述）。第二轮改用**运行时验证**：用 `sys.meta_path` 装一个自定义 finder，
对指定包名在 `find_spec` 里直接抛 `ImportError`（模拟"这个包没装"），然后真的
`import server.app`，看会不会炸、炸在哪一行。这是比 grep 更可信的证据——直接
观测解释器的真实导入行为，不用猜测某处 import 是不是在字符串里、是不是缩进在
函数体内。

## 3. 结论：7 个包全部可以安全挪 extras，已全部执行

### 3.1 `textual`

唯一使用点 `cli/main.py` 的 TUI 分支本来就包在 `try/except Exception` 里失败
自动回退（`cli/main.py:127-135`）。挪进新增的 `tui` extra。

### 3.2 `pandas` / `numpy` / `pyarrow`

用 `find_spec` 拦截这三个包后 `import server.app`，确实会炸——但炸的位置是：

```text
server/app.py
  → server/routes/adversarial.py         (eager)
    → server/adversarial_chamber.py      (eager: veya.platform.load("omodul"))
      → platform/3O/omodul/omodul/__init__.py
        → omodul.explain_codebase → oprim.glob_match
          → platform/3O/oprim/oprim/_do_calculus_intervention.py
            → import numpy
```

这条链**完全不经过 `server/quant_coprocessor.py`**——第一轮判断的那条
`darwin_evolution → quant_coprocessor` 链是假的：`quant_coprocessor.py` 里
`import pandas`/`import numpy`/`pd.read_parquet(...)` 那几处，逐行核对后
**全部在一个 f-string 里**（`_build_sandbox_script()` 拼出来的、要写进沙箱
子进程执行的脚本文本，`server/quant_coprocessor.py:69` 的 `return f"""..."""`
一直到 `~90` 行）——那是给沙箱子进程用的源代码字符串，不是当前进程真的执行的
`import` 语句。这个模块自己真正的顶层 import（1-30 行）只有 `asyncio`/
`contextlib`/`json`/`logging`/`os`/`Path`/`Any`/`veya.platform`/`veya.sandbox`，
零 pandas/numpy 依赖。`darwin_evolution.py` 也一样：只在 `__init__` 里构造
`QuantCoprocessor()`，构造本身不碰 pandas。

真正拉 `numpy` 的是 `server/routes/adversarial.py` 经 3O `omodul`/`oprim` 子库的
另一条完全独立的链——但这条链本身就要求 `platform/3O/` 存在（`omodul`、`oprim`
是 3O 子库提供的包名，不在 `veya` wheel 里）。也就是说：**一个真正干净的
`pip install veya`（不带 3O 子库）本来就会在这一步炸——不是因为 numpy，是因为
`omodul` 根本不存在**。numpy 装不装对这个已经存在的失败没有增量影响。

`pandas`/`pyarrow` 在整条 `server.app` eager 导入图里完全没被摸到（find_spec
拦截 numpy 后进程直接在上面那条链里挂掉，pandas/pyarrow 从未被 import 到；
单独只拦 pandas/pyarrow 不拦 numpy 测过，同样不会被真正 import）。

结论：三者从核心依赖挪到新增的 `quant` extra。

### 3.3 `matplotlib` / `networkx` / `plotly`

`find_spec` 拦截确认是真的 eager（这条第一轮判断对了，这次只是换成运行时验证）：

```text
server/app.py
  → server/routes/visualization.py            (eager) → veya/visualization.py (门面) → veya/omodul/visualization.py → matplotlib
  → server/routes/advanced_visualization.py   (eager) → veya/advanced_visualization.py (门面) → veya/omodul/advanced_visualization.py → networkx + plotly
```

两个是**不同的 router**（之前只查了 `advanced_visualization` 一个，漏了
`visualization.py` 单独引入 matplotlib 这条）。都在 `server/app.py` 顶层
`include_router`。

结论：三者从核心依赖挪到新增的 `viz` extra。

### 3.4 为什么"挪了之后 server.app 照样能跑"不矛盾

matplotlib/networkx/plotly 对 `server.app` 是真 eager——直接删掉这三个包，
`import server.app` 现在就会炸。但这不代表"不能挪进 extras"，因为：

1. **CLI 完全不需要 `server.app`**：`cli/main.py`/`cli/headless.py`/
   `cli/simple_cli.py` 全仓库 grep 零处 import `server.app`；`veya`/
   `veya-headless`/`veya-simple` 三个命令走的是 `coordinator_master`/
   `headless_run`，不碰 FastAPI 应用对象。纯 CLI 场景装 core 就够。
2. **生产 docker 镜像不受影响**：`deploy/requirements.txt`
   （`Dockerfile.backend:44` 先装的那份 pinned 全量清单）已经独立锁定这六个
   包的精确版本——`pyproject.toml` 核心依赖列表删减跟 `Dockerfile.backend`
   最终镜像里装了什么完全无关，构建时两份清单都会跑一遍
   （`Dockerfile.backend:44` 装 `requirements.txt`，`:49` 再装 `pip install .`）。
3. 真要单独 `pip install veya` 再手动跑 `server.app`（不用 docker、不用
   `deploy/requirements.txt`）的人，现在要么装 `veya[server]`（= `quant` +
   `viz`），要么继续用 core（CLI 场景）——这是标准 extras 用法，不是新问题，
   是把隐式的"这些包 server.app 需要"显式声明出来。

## 4. 已执行

`pyproject.toml`：

- 核心依赖删除 `textual`/`networkx`/`matplotlib`/`plotly`/`pandas`/`numpy`/
  `pyarrow`，只剩 `fastapi`/`uvicorn`/`httpx`/`aiohttp`/`python-dotenv`/
  `python-multipart`/`structlog`/`apscheduler` 八项。
- 新增 `tui`（textual）、`quant`（pandas/numpy/pyarrow）、`viz`（networkx/
  matplotlib/plotly）三个 extras，以及一个便捷组合 `server = ["veya[quant]",
  "veya[viz]"]`（自引用 extras 语法，`pip install --dry-run -e ".[server]"`
  验证过 hatchling/pip 能正确解析并解出 matplotlib/plotly 等传递依赖，见 §5）。

## 5. 验证

- `tomllib.load` 解析 `pyproject.toml` 通过，`dependencies`/各 `optional-
  dependencies` 分组内容核对过。
- `pip install --dry-run -e ".[server]"` 和 `.[quant]"`：真实调用 pip 的依赖
  解析器（不是只读 TOML），确认自引用 extras 语法被正确处理，`matplotlib`/
  `plotly`/`pandas`/`numpy`/`pyarrow` 都能通过对应分组解出来（`Would install
  veya-0.6.0`，无报错）；`--dry-run` 不写盘，没有改动本地环境。
- `sys.meta_path` + `find_spec` 运行时拦截测试（§3.2/3.3 依据）：分别拦截
  `{pandas, numpy, pyarrow}` 和 `{matplotlib, networkx, plotly}` 两组后
  `import server.app`，确认了真实失败点（前者炸在 3O omodul 链，后者炸在两个
  visualization router）。**注意**：第一轮用的是 Python 2 遗留的
  `find_module` hook（在 Python 3.14 下永远不会被调用，等于没装拦截器），
  导致第一次"矛盾结果"（挡了 matplotlib 却显示导入成功）——换成正确的
  `find_spec` 后结果才可信，见 §6。
- CLI 不依赖 `server.app` 的结论：`grep -rln "server.app\|server\\.app" cli/`
  零命中（排除 `tests/`）。

## 6. 方法论教训（第一轮错在哪）

1. **f-string 里的代码文本会被 grep 当成真代码**：`grep "^import numpy"` 匹配
   到的 `server/quant_coprocessor.py:72` 其实是三重引号字符串里的一行文本
   （沙箱子进程脚本源码），不是解释器真正执行的 import 语句。**教训**：看到
   `import`/`from` 匹配命中后，要往上翻看它是不是在一个 `f"""..."""` 或
   `"""..."""` 块里，尤其是这种"拼子进程脚本"的代码生成器模式在这个仓库里
   不止一处（`_build_sandbox_script` 至少两个变体）。
2. **函数体内的缩进 import 会被不带 `^` 锚点的宽松正则误判成 eager**：
   `veya/oprim/vad.py` 里 `import numpy as np` 其实缩进在函数体内（已经是
   懒加载），第一轮用的 grep pattern 里有一支没锚定行首（`"import $pkg "`
   而不是 `"^import $pkg"`），把缩进的行也匹配上了。**教训**：判断"是不是
   eager"不能只看有没有 import 这个词出现，要么严格锚定 `^import`/`^from`，
   要么（更可靠）直接用运行时拦截验证，不要靠正则猜缩进语义。
3. 两个错误共同导致第一轮把 pandas/numpy/pyarrow 错误分类成"不安全"，多写了
   一份不必要的"懒加载重构方案"（已删除，不再具有参考价值）。真正安全与否
   最终是靠 §3.2 的 `find_spec` 拦截实验纠正的，不是靠更仔细地重新 grep。

## 7. 第三轮：补上第二轮遗漏的真实回归（2026-08-24）

第二轮验证过 `import server.app` 在 matplotlib/networkx/plotly 缺失时会真的
`ImportError`（§3.3，这个结论没错），但当时只满足于"证明它们确实是 eager 的、
挪 extras 不会让 docker 变差"，**没有反过来确认"挪了之后，还有谁指望这条
import 链不炸"**。核对 `README.md` 的三步快速开始（`pip install veya` →
`veya init` → `veya start`）时发现：`cli/product.py::run_start()` 会
`from veya.server.app import app`，而 `veya/server/app.py:27` 内部又
`from server.app import app as _agentos_app`——是同一条链，同一批 eager
import。也就是说，纯改 `pyproject.toml` 声明（不改一行运行代码）之后，
用户照着 README 走三步，第三步 `veya start` 就会因为装的是 core（不含
`matplotlib`/`networkx`/`plotly`）而崩溃——这是这次调研自己制造的新回归，
必须在同一轮里堵上，不能留到"以后有空再修"。

### 修复

`server/app.py`：把 `advanced_visualization_router`/`visualization_router`
两个 import 从模块顶层的固定 import 挪进一个 `_load_viz_routers()` 辅助函数，
`try/except ImportError` 兜底返回 `(None, None)` 并记一条带 `pip install
veya[viz]` 提示的 warning；对应的两处 `app.include_router(...)` 加
`is not None` 判断跳过未挂载的路由。**没有**动 `adversarial_router`/
`darwin_evolution` 那条线——那条线的失败原因是 3O `omodul`/`oprim` 子库本身
不在 wheel 里（不是这三个包装不装的问题，见 §3.2），修那个是完全不同性质
的问题（server.app 该不该在没有 3O 子库时也能跑），不在这次 PR-08 范围内。

### 验证（运行时，不是只看代码顺眼）

用 `find_spec` 拦截 `{matplotlib, plotly}`（刻意不拦 `networkx`——3O 的
`oprim._do_calculus_intervention.py` 也在用它，混在一起测会把"该拦的"和
"3O 本来就要求存在"这两件事搞混）后 `import server.app`：

```text
IMPORTED OK, total routes: 60   (正常 62, 少了两个视觉路由, 差值精确对上)
viz router loaded:      False
adv viz router loaded:  False
```

不拦截时（本地 venv 全部包都在）：`total routes: 62`，两个路由都 `True`，
跟改动前行为一致。`--follow-imports=skip` 下 `mypy server/app.py` 0 错误
（中途踩过一次 mypy 对 try/except 里重复绑定同名变量的 `no-redef` 误报，
挪成显式返回 `tuple[APIRouter | None, APIRouter | None]` 的函数解决，不是
`# type: ignore` 糊过去）。`ruff check server/app.py` 除一处这次没碰的
pre-existing `E402`（`veya.oservi.gateway`，跟这次改动无关）外干净。
