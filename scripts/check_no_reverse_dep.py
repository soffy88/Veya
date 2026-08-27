#!/usr/bin/env python3
"""严格 3O 迁移 · 阶段 0 强制检查 #1: check_no_reverse_dep.py

禁止「上层被下层 import」（依赖方向单向向下）。

3O 分层 (rank 0=最底, 4=最顶):
    rank 0  veya/obase   — 句柄层 (基建: telemetry/authz/sandbox/llm 通道)
    rank 1  veya/oprim   — 原子操作层 (无副作用原语)
    rank 2  veya/oskill  — 纯算法层 (可纯函数化的逻辑, 阶段 2 起强制纯净)
    rank 3  veya/omodul  — 流程控制层 (编排: tool_pipeline/agent_loop/session_tree)
    rank 4  veya/oservi  — 服务装配层 (daemon/gateway 骨架)

规则:
  R1 同层互引允许; 高层可引低层; 低层引高层 = 违规 (reverse dependency)。
  R2 任何 3O 层都禁止 import 业务根 (server/agents/cli/commands/config/session/
     tools/registries/hooks/streaming/subagent/tui/apps/auth/permission/services/
     security/infra, 以及 veya.tools/veya.im/veya.models/veya.server 等业务子包)。
  R3 裸名导入旧主库 (import oskill → platform/3O/oskill) = 违规,
     必须写 veya.oskill (旧子库只作参考, 逐步替换)。
  R4 veya.errors / veya.compat / veya.platform 是跨层基建桥, 任何层可用。

用法:
    python scripts/check_no_reverse_dep.py [root] [--quiet]
    python scripts/check_no_reverse_dep.py --write-baseline scripts/baseline_reverse_dep.txt
    python scripts/check_no_reverse_dep.py --baseline scripts/baseline_reverse_dep.txt
退出码: 0 = 通过; 1 = 发现违规 (列出明细)

基线模式: 阶段 0 存量违规先记录到基线文件, 后续每阶段只拦增量;
各阶段把修复项从基线里删掉, 最终目标 = 无基线跑 strict。
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import sys
from collections.abc import Iterable

# rank 表: veya.<layer> -> 层号
LAYER_RANK: dict[str, int] = {
    "veya.obase": 0,
    "veya.oprim": 1,
    "veya.oskill": 2,
    "veya.omodul": 3,
    "veya.oservi": 4,
}

# 3O 层所在目录 (相对 root)
LAYER_DIRS: dict[str, str] = {
    "veya.obase": "veya/obase",
    "veya.oprim": "veya/oprim",
    "veya.oskill": "veya/oskill",
    "veya.omodul": "veya/omodul",
    "veya.oservi": "veya/oservi",
}

# R2: 业务根 — 3O 任何层不得触碰
BUSINESS_ROOTS: tuple[str, ...] = (
    "server",
    "agents",
    "cli",
    "commands",
    "config",
    "session",
    "tools",
    "registries",
    "hooks",
    "streaming",
    "subagent",
    "tui",
    "apps",
    "auth",
    "permission",
    "services",
    "security",
    "infra",
    "mode",
)

# R2 补充: veya 命名空间下的业务子包
VEYA_BUSINESS_SUBPKGS: tuple[str, ...] = (
    "veya.tools",
    "veya.im",
    "veya.models",
    "veya.server",
    "veya.cli",
    "veya.commands",
    "veya.config",
    "veya.session",
    "veya.streaming",
    "veya.subagent",
    "veya.registries",
    "veya.hooks",
)

# R4: 跨层基建桥
BRIDGE_PKGS: tuple[str, ...] = ("veya.errors", "veya.compat", "veya.platform")

# R3: 旧主库裸名 (sys.path 注入后裸 import 会落到 platform/3O)
OLD_MAINLIB_NAMES: tuple[str, ...] = ("obase", "oprim", "oskill", "omodul", "oservi")


def _layer_of_module(module: str) -> int | None:
    """veya.obase.x → 0; veya.oservi.engines.y → 4; 非 3O veya.* → None。"""
    for pkg, rank in LAYER_RANK.items():
        if module == pkg or module.startswith(pkg + "."):
            return rank
    return None


def _iter_py_files(root: pathlib.Path) -> Iterable[pathlib.Path]:
    for layer_dir in LAYER_DIRS.values():
        d = root / layer_dir
        if d.is_dir():
            yield from sorted(d.rglob("*.py"))


def _import_roots(node: ast.Import | ast.ImportFrom) -> list[tuple[str, str]]:
    """提取一条 import 语句涉及的 (顶层名, 完整模块名)。"""
    out: list[tuple[str, str]] = []
    if isinstance(node, ast.Import):
        for a in node.names:
            out.append((a.name.split(".")[0], a.name))
        return out
    # ImportFrom: from X.Y import ... → X (相对导入放行)
    if node.level or not node.module:
        return []
    return [(node.module.split(".")[0], node.module)]


def check_file(path: pathlib.Path, root: pathlib.Path) -> list[str]:
    """返回违规描述列表 (空 = 合规)。"""
    violations: list[str] = []
    rel = path.relative_to(root).as_posix()
    # 定位该文件所属层
    owner_layer: str | None = None
    for pkg, layer_dir in LAYER_DIRS.items():
        if rel == layer_dir or rel.startswith(layer_dir + "/"):
            owner_layer = pkg
            break
    if owner_layer is None:
        return violations  # 非 3O 目录, 跳过

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        return [f"{rel}: SyntaxError {exc}"]

    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        lineno = getattr(node, "lineno", 0)
        for root_name, full_name in _import_roots(node):
            # R3: 裸名导入旧主库
            if root_name in OLD_MAINLIB_NAMES:
                violations.append(
                    f"{rel}:{lineno} R3 裸名导入旧主库 {root_name!r} "
                    f"(会解析到 platform/3O) → 应写 veya.{root_name}"
                )
                continue
            # R2: 业务根
            if root_name in BUSINESS_ROOTS:
                violations.append(
                    f"{rel}:{lineno} R2 越界导入业务根 {root_name!r} ({owner_layer} 不得依赖上层)"
                )
                continue
            # veya.* 内部: rank 检查
            if root_name == "veya":
                module = full_name
                if any(module == b or module.startswith(b + ".") for b in BRIDGE_PKGS):
                    continue  # R4 基建桥
                if any(module == b or module.startswith(b + ".") for b in VEYA_BUSINESS_SUBPKGS):
                    violations.append(
                        f"{rel}:{lineno} R2 越界导入业务子包 {module!r} "
                        f"({owner_layer} 不得依赖上层)"
                    )
                    continue
                target_rank = _layer_of_module(module)
                if target_rank is not None:
                    owner_rank = LAYER_RANK[owner_layer]
                    if target_rank > owner_rank:
                        violations.append(
                            f"{rel}:{lineno} R1 反向依赖: {owner_layer} "
                            f"(rank {owner_rank}) 导入了 {module} (rank {target_rank})"
                        )
    return violations


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="3O 依赖方向强制检查")
    ap.add_argument("root", nargs="?", default=".", help="仓库根目录")
    ap.add_argument("--quiet", action="store_true", help="只报违规, 不打印统计")
    ap.add_argument("--baseline", default=None, help="基线文件 (未列出的违规才失败)")
    ap.add_argument("--write-baseline", default=None, help="把当前违规写入基线文件并退出")
    args = ap.parse_args(argv)

    root = pathlib.Path(args.root).resolve()
    all_violations: list[str] = []
    for path in _iter_py_files(root):
        all_violations.extend(check_file(path, root))
    all_violations = sorted(set(all_violations))

    if args.write_baseline:
        out = pathlib.Path(args.write_baseline)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            "\n".join(all_violations) + ("\n" if all_violations else ""), encoding="utf-8"
        )
        print(f"[BASELINE] {len(all_violations)} 条违规已写入 {out}")
        return 0

    if args.baseline:
        baseline_path = pathlib.Path(args.baseline)
        baseline = (
            set(baseline_path.read_text(encoding="utf-8").splitlines())
            if baseline_path.is_file()
            else set()
        )
        all_violations = [v for v in all_violations if v not in baseline]

    if all_violations:
        for v in all_violations:
            print(f"[FAIL] {v}")
        print(f"\n共 {len(all_violations)} 处依赖方向违规。")
        return 1
    if not args.quiet:
        total = sum(1 for _ in _iter_py_files(root))
        print(
            f"[OK] {total} 个 3O 层文件全部满足单向依赖 (obase ← oprim ← oskill ← omodul ← oservi)。"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
