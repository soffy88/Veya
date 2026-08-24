#!/usr/bin/env python3
"""docs/VEYA_10_OF_10_PLAN.md §28「建立 legacy import scanner」。

只读报告模式（不改运行行为，退出码恒为 0）：给全仓库两类 legacy 路径生成
import 引用清单，作为 `docs/LEGACY_MIGRATION.md`（计划 PR-03 设想产出）的
数据来源，而不是维护一份手写文档跟代码脱节。

覆盖两类：
1. `architecture/manifest.yaml::deprecated` 里登记的模块（跟
   `check_architecture_manifest.py` 的 known_importers diff 是同一份数据，
   这里只是换一种更完整的展示形式）。
2. "3O 归位门面"——`veya/*.py` 里那些 `sys.modules[__name__] = _impl` 的
   5 行 alias 文件（自动按文件内 docstring 标记词发现，不是手写清单，新增/
   删除门面文件不需要跟着改这个脚本）。manifest.yaml 的 `compat_facades`
   目前只登记了 3 个（kernel 会用到的），完整清单比这个大得多——这是本脚本
   相对 `check_architecture_manifest.py` 的增量覆盖面。

用法：python scripts/legacy_import_scan.py [root]
"""

from __future__ import annotations

import ast
import pathlib
import sys

_FACADE_MARKER = "3O 归位门面"
_SKIP_DIRS = {"venv", "node_modules", ".git", "platform", "docs", "site", "deploy", "__pycache__"}


def _iter_py_files(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(
        p
        for p in root.rglob("*.py")
        if not any(part in _SKIP_DIRS for part in p.relative_to(root).parts)
    )


def _discover_facades(root: pathlib.Path) -> dict[str, str]:
    """扫描 veya/*.py, 找出所有 "3O 归位门面" 别名文件, 返回 {旧路径: 真实实现路径}。"""
    facades: dict[str, str] = {}
    veya_dir = root / "veya"
    if not veya_dir.is_dir():
        return facades
    for path in sorted(veya_dir.glob("*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if _FACADE_MARKER not in text:
            continue
        mod_name = f"veya.{path.stem}"
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            continue
        real: str | None = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("veya."):
                for alias in node.names:
                    if alias.asname == "_impl":
                        real = f"{node.module}.{alias.name}"
        facades[mod_name] = real or "?"
    return facades


def _build_import_index(root: pathlib.Path) -> dict[str, set[str]]:
    """一次性解析全仓库, 返回 {被 import 的模块名: {引用它的文件(点分)}}。

    单趟扫描, 供后面对多个 target 名字复用——避免每个 target 各自重扫全仓库
    (35 个门面文件若各扫一遍会是 O(N*M), 在这个仓库规模下会超时)。
    """
    index: dict[str, set[str]] = {}
    for path in _iter_py_files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        rel = str(path.relative_to(root).with_suffix("")).replace("/", ".")
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                index.setdefault(name, set()).add(rel)
    return index


def _find_importers(index: dict[str, set[str]], target: str) -> set[str]:
    found: set[str] = set()
    for name, importers in index.items():
        if name == target or name.startswith(target + "."):
            found |= importers
    return found


def main(root_arg: str = ".") -> int:
    root = pathlib.Path(root_arg).resolve()

    print("== legacy import scan (report-only, exit 0 恒定) ==\n")

    index = _build_import_index(root)

    print("-- 3O 归位门面 (veya/*.py 5 行 sys.modules 别名) --")
    facades = _discover_facades(root)
    if not facades:
        print("  (未发现门面文件)")
    for alias, target in sorted(facades.items()):
        importers = _find_importers(index, alias) - {alias}
        tag = "0 处引用 (可安全考虑清理)" if not importers else f"{len(importers)} 处引用"
        print(f"  {alias} -> {target}  [{tag}]")
        for imp in sorted(importers):
            print(f"      - {imp}")

    manifest_path = root / "architecture" / "manifest.yaml"
    if manifest_path.exists():
        try:
            import yaml

            manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        except ImportError:
            manifest = {}
        deprecated = manifest.get("deprecated") or []
        print("\n-- architecture/manifest.yaml::deprecated --")
        if not deprecated:
            print("  (清单为空)")
        for entry in deprecated:
            name = entry["name"]
            importers = _find_importers(index, name) - {name}
            print(f"  {name} -> {entry.get('replacement', '?')}  [{len(importers)} 处引用]")
            for imp in sorted(importers):
                print(f"      - {imp}")

    print("\n(只读报告, 不产生非零退出码; 迁移决策见对应 rfc / ADR 文档)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
