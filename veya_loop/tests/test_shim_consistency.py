"""veya_loop 装配面守护测试: 防主库转发漂移 + 防装配缺漏。

守护两条铁律:
  1. _ELEMENT_MAP 每个符号必须能在主库解析 (惰性 __getattr__ 不抛 AttributeError);
  2. oprim/omodul shim 文件的 __all__ 符号必须存在于主库对应模块 (转发不漂移)。
"""

from __future__ import annotations

import importlib
import pathlib
import re

import pytest

import veya_loop
from veya_loop import _assembly

_SRC = pathlib.Path(veya_loop.__file__).parent


# ---------------------------------------------------------------------------
# 1. _ELEMENT_MAP 全量可解析 + 类型正确
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(veya_loop._ELEMENT_MAP))
def test_element_map_resolvable(name: str) -> None:
    lib, symbol = veya_loop._ELEMENT_MAP[name]
    mod = _assembly.load(lib)
    assert hasattr(mod, symbol), f"{lib}.{symbol} 在主库缺失 (装配漂移!)"
    value = getattr(veya_loop, name)  # 触发惰性装配 + 缓存
    assert value is getattr(mod, symbol), "惰性装配缓存与主库对象不一致"


def test_element_map_symbols_are_callable_or_types() -> None:
    import types

    for name, (_lib, symbol) in veya_loop._ELEMENT_MAP.items():
        value = getattr(veya_loop, name)
        assert not isinstance(value, types.ModuleType), (
            f"{name} 装配到了模块对象 (应装配具体符号 {symbol})"
        )


# ---------------------------------------------------------------------------
# 2. shim 文件转发一致性 (oprim/_*.py, omodul/_*.py)
# ---------------------------------------------------------------------------


def _shim_modules() -> list[pathlib.Path]:
    return sorted(list((_SRC / "oprim").glob("_*.py")) + list((_SRC / "omodul").glob("_*.py")))


@pytest.mark.parametrize("shim", _shim_modules(), ids=lambda p: p.stem)
def test_shim_exports_exist_in_mainlib(shim: pathlib.Path) -> None:
    """shim 模块 __all__ 里的每个符号, 必须在主库同名模块存在。"""
    text = shim.read_text(encoding="utf-8")
    main_mod = f"{shim.parent.name}.{shim.stem}"
    main_pkg = _assembly.load(shim.parent.name)
    try:
        target = importlib.import_module(main_mod)
    except ModuleNotFoundError:
        # 主库子模块可能不存在 → 检查是否仅顶层转发
        target = main_pkg

    exported = re.findall(r"^__all__\s*=\s*\[(.*?)\]", text, re.S | re.M)
    if not exported:
        return  # 无显式 __all__ 的 shim 不守护
    names = re.findall(r'"([^"]+)"', exported[0])
    assert names, f"{shim} __all__ 为空"
    missing = [n for n in names if not hasattr(target, n)]
    assert not missing, f"{main_mod} 缺失 shim 转发符号: {missing}"


def _all_forwarder_shims() -> list[pathlib.Path]:
    """全部转发 shim (含非下划线, 如 omodul/cholesky_scm.py) —— 覆盖 _*.py 之外的转发面。"""
    out: list[pathlib.Path] = []
    for sub in ("obase", "oprim", "omodul"):
        d = _SRC / sub
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.py")):
            if p.stem != "__init__":
                out.append(p)
    return out


@pytest.mark.parametrize("shim", _all_forwarder_shims(), ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_shim_import_all(shim: pathlib.Path) -> None:
    """每个 shim 可导入, 且 __all__ 每个符号在导入后成功绑定 (无转发到缺失符号)。

    导入即执行转发 (X = _mainlib.mod.SYM), 主库符号消失时此处 ImportError/AttributeError ——
    因此本测试覆盖全部转发面 (含 shim 名≠主库模块名的, 如 long_task_state→long_task_goal)。
    """
    mod = importlib.import_module(f"veya_loop.{shim.parent.name}.{shim.stem}")
    names = list(getattr(mod, "__all__", []) or [])
    unresolved = [n for n in names if not hasattr(mod, n)]
    assert not unresolved, f"{shim.parent.name}/{shim.stem} 转发到缺失符号: {unresolved}"


# ---------------------------------------------------------------------------
# 3. 关键能力面符号抽查 (文档承诺的导出必须存在)
# ---------------------------------------------------------------------------

PROMISED_EXPORTS = {
    # P1 神经符号
    "PlanIR",
    "parse_ir",
    "validate",
    "compile_expr",
    "explain",
    "shrink_to_mus",
    "check_feasible",
    "optimize",
    "diff_all",
    "assign_one_to_one",
    "vcg",
    "check_strategyproof",
    "LeaseManager",
    "WaitForGraph",
    "Game",
    "pure_nash",
    "SnapshotStore",
    "MCTS",
    "puct",
    "lookahead",
    "ActionPlan",
    "Applier",
    "LocalSandbox",
    "SandboxPool",
    # Phase 2/3/4
    "CausalGraphStore",
    "causal_fault_diagnose",
    "closed_loop_intervene",
    "select_intervention",
    "expected_utility",
    "multi_step_plan",
    "counterfactual_rollout",
    "AuditEmitter",
    # L3 反事实 / 代码可靠性
    "StructuralSCM",
    "CholeskyMechanism",
    "HybridSCM",
    "bayesian_optimize",
    "run_code_reliability_loop",
    # 自有组件
    "HardenedExecutor",
    "PermissionContract",
    "dispatch_intervention",
    "ExecutionAdapter",
    "RestartAdapter",
    "dispatch_via_adapter",
}


def test_promised_exports_present() -> None:
    missing = sorted(PROMISED_EXPORTS - set(dir(veya_loop)))
    assert not missing, f"文档承诺导出缺失: {missing}"


# ---------------------------------------------------------------------------
# 4. CLI 入口 (打包声明 veya_loop.cli:main 必须可调用)
# ---------------------------------------------------------------------------


def test_cli_entrypoint() -> None:
    from veya_loop import cli

    assert callable(cli.main)
    cli.main(["--version"])  # 不抛异常, 打印 JSON


def test_cli_selftest_smoke() -> None:
    """selftest 冒烟必须全绿 (CLI 是装配面的可执行验证)。"""
    from veya_loop import cli

    result = cli.cmd_selftest(None)  # type: ignore[arg-type]
    assert result is None  # 全绿时仅打印 JSON 不退出
