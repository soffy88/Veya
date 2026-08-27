#!/usr/bin/env python3
"""严格 3O 迁移 · 阶段 0 强制检查 #2: check_oskill_pure.py

检测 oskill 层 (算法大脑) 是否混入副作用:
  IO       — 文件/网络/进程/终端 I/O (os/pathlib/subprocess/socket/httpx/open/print...)
  GLOBAL   — 模块级可变状态 / global 语句
  NONDET   — 非确定性调用 (random/time.now/uuid/hash/requests...)

用法:
    # 生成基线 (阶段 0: 存量 oskill 按历史放行, 只拦增量违规)
    python scripts/check_oskill_pure.py --write-baseline scripts/baseline_oskill.txt

    # 常规检查 (默认): 超过基线的违规 → 退出码 1
    python scripts/check_oskill_pure.py [--baseline scripts/baseline_oskill.txt]

    # 严格模式: 无视基线, 任何违规都失败 (阶段 2 收尾时启用)
    python scripts/check_oskill_pure.py --strict

    路径含 /pure/ 的文件或 docstring 含 "3O-PURE" 标记 → 天然严格 (基线不豁免),
    阶段 2 新增纯函数元素放 veya/oskill/pure/ 即可被强制纯净。
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import sys
from typing import Iterable

# ---- I/O 相关导入 (AST import 名 / 子模块前缀) ----
IO_IMPORTS: tuple[str, ...] = (
    "os",
    "pathlib",
    "sys",
    "io",
    "subprocess",
    "socket",
    "requests",
    "httpx",
    "aiohttp",
    "urllib",
    "tempfile",
    "shutil",
    "signal",
    "pty",
    "fcntl",
    "ctypes",
    "multiprocessing",
    "concurrent.futures",
    "threading",
    "asyncio",
    "select",
    "selectors",
    "pdb",
    "readline",
)
IO_MODULE_CALLS: dict[str, tuple[str, ...]] = {
    # module_prefix -> 函数名黑名单 (空 = 该模块任何调用都算 I/O)
    "os.": ("",),  # 任何 os.* 调用 (含 environ/getenv/remove/... 与 pure 无关)
    "Path": (
        ".read_text",
        ".write_text",
        ".open",
        ".exists",
        ".is_file",
        ".is_dir",
        ".iterdir",
        ".glob",
        ".rglob",
        ".mkdir",
        ".makedirs",
        ".unlink",
        ".rename",
        ".replace",
        ".rmdir",
        ".stat",
        ".lstat",
        ".chmod",
        ".touch",
        ".resolve",
        ".cwd",
        ".home",
        ".expanduser",
    ),
    "subprocess": ("",),
    "socket": ("",),
    "tempfile": ("",),
    "shutil": ("",),
}
IO_BUILTIN_CALLS: tuple[str, ...] = ("open", "input", "print", "breakpoint", "exec", "eval")

# ---- 全局状态 ----
GLOBAL_STMT_KW = ("global", "nonlocal")
MUTABLE_LITERALS = (
    ast.List,
    ast.Dict,
    ast.Set,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)

# ---- 非确定性 ----
NONDET_IMPORTS: tuple[str, ...] = ("random", "secrets", "uuid")
NONDET_MODULE_CALLS: dict[str, tuple[str, ...]] = {
    "time.": ("time", "clock", "monotonic", "perf_counter", "process_time", "sleep", "nanosleep"),
    "datetime.": ("now", "today", "utcnow"),
    "date.": ("today",),
    "uuid.": ("uuid1", "uuid4"),
    "random.": ("",),
    "secrets.": ("",),
    "os.": ("urandom", "getpid", "getppid"),
}
NONDET_BUILTIN_CALLS: tuple[str, ...] = ("hash",)


def _iter_target_files(root: pathlib.Path, targets: list[str]) -> Iterable[pathlib.Path]:
    for t in targets:
        d = root / t
        if d.is_dir():
            yield from sorted(d.rglob("*.py"))
        elif d.is_file():
            yield d


def _is_strict_pure(path: pathlib.Path, tree: ast.Module) -> bool:
    """路径含 /pure/ 或 docstring 标记 3O-PURE → 基线不豁免。"""
    if "pure" in path.as_posix().split("/"):
        return True
    doc = ast.get_docstring(tree) or ""
    return "3O-PURE" in doc


def _call_is_io(node: ast.Call) -> bool:
    fn = node.func
    # 内置函数
    if isinstance(fn, ast.Name):
        return fn.id in IO_BUILTIN_CALLS
    # 属性链: base.attr(...)
    if isinstance(fn, ast.Attribute):
        attr = fn.attr
        # 展开链取最左基名
        base = fn
        while isinstance(base, ast.Attribute):
            base = base.value
        if isinstance(base, ast.Name):
            base_name = base.id
            hits = IO_MODULE_CALLS.get(base_name, ())
            if hits == ("",):
                return True
            if attr in hits:
                return True
        # os.environ / os.getenv 等: base 是 os 的调用
        if isinstance(fn.value, ast.Attribute) and isinstance(fn.value.value, ast.Name):
            if fn.value.value.id in ("os", "sys", "subprocess", "socket", "pathlib"):
                return True
    return False


def _call_is_nondet(node: ast.Call) -> bool:
    fn = node.func
    if isinstance(fn, ast.Name):
        if fn.id in NONDET_BUILTIN_CALLS:
            return True
        hits = NONDET_MODULE_CALLS.get(fn.id + ".", ())
        return hits == ("",)  # 同模块名的直接调用很少见, 保守
    if isinstance(fn, ast.Attribute):
        attr = fn.attr
        base = fn.value
        if isinstance(base, ast.Name):
            base_name = base.id
            hits = NONDET_MODULE_CALLS.get(base_name + ".", ())
            if hits == ("",) or attr in hits:
                return True
        # 链式 datetime.datetime.now / uuid.uuid4
        if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
            mod, sub = base.value.id, base.attr
            if mod in ("datetime", "uuid", "time", "random", "secrets"):
                hits = NONDET_MODULE_CALLS.get(sub + ".", ())
                if hits == ("",) or attr in hits:
                    return True
    return False


def _module_assignments(tree: ast.Module) -> dict[str, ast.AST]:
    """模块级赋值名字 → 赋值节点（常量候选）。"""
    out: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                out[node.target.id] = node
    return out


_MUTATORS: tuple[str, ...] = (
    "append",
    "extend",
    "add",
    "update",
    "setdefault",
    "remove",
    "pop",
    "clear",
    "insert",
    "sort",
    "reverse",
    "discard",
    "difference_update",
    "symmetric_difference_update",
    "__setitem__",
)


def _is_mutated(tree: ast.Module, name: str) -> bool:
    """名字是否在模块内被变异（下标/属性赋值、mutator 调用、augassign、global）。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Global) and name in node.names:
            return True
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name:
                return True
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name):
                    if t.value.id == name:
                        return True
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name):
                    if t.value.id == name:
                        return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name):
                if node.func.value.id == name and node.func.attr in _MUTATORS:
                    return True
    return False


def _check_module_level_global(tree: ast.Module) -> list[str]:
    """模块级可变状态: 被变异的模块级赋值（常量豁免 — 不可变/从未变异的
    dict/list/set/compile 结果属于常量配置, 不构成状态）。"""
    out: list[str] = []
    assigns = _module_assignments(tree)
    for name, node in assigns.items():
        value = node.value if isinstance(node, ast.Assign) else node.value
        if value is None:
            continue
        is_mutable_init = isinstance(value, MUTABLE_LITERALS) or isinstance(value, ast.Call)
        if is_mutable_init and _is_mutated(tree, name):
            out.append(f"module-level mutable state {name!r} at line {node.lineno}")
    # 模块级直接 mutating 调用 (x.append(...) 作为语句)
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            fn = node.value.func
            if isinstance(fn, ast.Attribute) and fn.attr in _MUTATORS:
                out.append(f"module-level mutation call at line {node.lineno}")
    return out


def check_file(path: pathlib.Path, root: pathlib.Path) -> tuple[list[str], bool]:
    """返回 (违规列表, 是否严格纯净文件)。"""
    rel = path.relative_to(root).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        return [f"{rel}: SyntaxError {exc}"], False
    strict = _is_strict_pure(path, tree)
    violations: list[str] = []

    # import 面
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                top = a.name.split(".")[0]
                if top in IO_IMPORTS:
                    violations.append(f"{rel}:{node.lineno} IO import {a.name!r}")
                if top in NONDET_IMPORTS:
                    violations.append(f"{rel}:{node.lineno} NONDET import {a.name!r}")
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                top = node.module.split(".")[0]
                if top in IO_IMPORTS:
                    violations.append(f"{rel}:{node.lineno} IO import {node.module!r}")
                if top in NONDET_IMPORTS:
                    violations.append(f"{rel}:{node.lineno} NONDET import {node.module!r}")

    # global/nonlocal
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            violations.append(f"{rel}:{node.lineno} GLOBAL global 语句 {node.names}")
        if isinstance(node, ast.Nonlocal):
            violations.append(f"{rel}:{node.lineno} GLOBAL nonlocal 语句 {node.names}")

    # 调用面
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if _call_is_io(node):
                violations.append(f"{rel}:{node.lineno} IO call {ast.unparse(node.func)[:60]}")
            if _call_is_nondet(node):
                violations.append(f"{rel}:{node.lineno} NONDET call {ast.unparse(node.func)[:60]}")

    # 模块级可变状态 (仅顶层 body)
    for msg in _check_module_level_global(tree):
        violations.append(f"{rel}:{msg}")

    return violations, strict


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="oskill 纯净度强制检查")
    ap.add_argument("root", nargs="?", default=".", help="仓库根目录")
    ap.add_argument(
        "--targets", nargs="+", default=["veya/oskill"], help="扫描目录/文件 (默认 veya/oskill)"
    )
    ap.add_argument("--baseline", default=None, help="基线文件 (未列出的违规才失败)")
    ap.add_argument("--write-baseline", default=None, help="把当前违规写入基线文件")
    ap.add_argument("--strict", action="store_true", help="无视基线, 任何违规都失败")
    ap.add_argument("--quiet", action="store_true", help="通过时静默")
    args = ap.parse_args(argv)

    root = pathlib.Path(args.root).resolve()
    violations: list[str] = []
    for path in _iter_target_files(root, args.targets):
        file_violations, strict = check_file(path, root)
        for v in file_violations:
            if not strict and not args.strict:
                continue  # 非严格文件在常规模式下只计入基线
            violations.append(v)
        # 常规模式: 非严格文件全量进入基线比对 (下面统一处理)

    if args.write_baseline:
        all_v: list[str] = []
        for path in _iter_target_files(root, args.targets):
            fv, _ = check_file(path, root)
            all_v.extend(fv)
        out = pathlib.Path(args.write_baseline)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(sorted(set(all_v))) + ("\n" if all_v else ""), encoding="utf-8")
        print(f"[BASELINE] {len(set(all_v))} 条违规已写入 {out} (严格纯净文件除外由调用方另计)")
        return 0

    if not args.strict and args.baseline:
        baseline_path = pathlib.Path(args.baseline)
        if baseline_path.is_file():
            baseline = set(baseline_path.read_text(encoding="utf-8").splitlines())
        else:
            baseline = set()
        # 严格纯净文件违规不豁免
        new = [v for v in violations if v not in baseline]
    else:
        new = violations

    if new:
        for v in sorted(new):
            print(f"[FAIL] {v}")
        print(
            f"\n共 {len(new)} 处违规 (基线内存量未计)。运行 "
            f"`python scripts/check_oskill_pure.py --write-baseline ...` 可刷新基线。"
        )
        return 1
    if not args.quiet:
        print(
            f"[OK] oskill 纯净度通过 (共扫描 {len(list(_iter_target_files(root, args.targets)))} 个文件)。"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
