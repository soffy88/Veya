#!/usr/bin/env python3
"""docs/VEYA_10_OF_10_PLAN.md §3 / PR-01: architecture/manifest.yaml 现状校验。

只读报告模式（不改运行行为）：
1. kernel.master_entry / canonical_* / compat_facades 里的模块必须真实可 import。
2. deprecated[].known_importers 跟仓库现状做 diff——报告"清单说有但代码里没了"
   （该更新清单）和"代码里新增但清单没记"（该核实是不是新漂移）两类偏差。
3. forbidden_imports（当前为空）非空时，用 AST 扫描真的拉红违规 import。

用法：python scripts/check_architecture_manifest.py [root]
退出码：0 = 通过（含"只有报告没有强制项"的情况）；1 = manifest 结构错误或 forbidden_imports 违规。
"""

from __future__ import annotations

import ast
import pathlib
import sys

import yaml


def _iter_py_files(root: pathlib.Path):
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _find_importers(root: pathlib.Path, target: str) -> set[str]:
    """扫描 root 下所有 .py 文件, 找出真的 import 了 target 模块的文件 (点分模块名)。"""
    found: set[str] = set()
    skip_dirs = {"venv", "node_modules", ".git", "platform", "docs", "site", "deploy"}
    for path in _iter_py_files(root):
        if any(part in skip_dirs for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name == target or name.startswith(target + "."):
                    rel = path.relative_to(root).with_suffix("")
                    found.add(str(rel).replace("/", "."))
    return found


def _module_importable(root: pathlib.Path, dotted: str) -> bool:
    rel = pathlib.Path(*dotted.split(".")).with_suffix(".py")
    if (root / rel).exists():
        return True
    pkg_init = root / pathlib.Path(*dotted.split(".")) / "__init__.py"
    return pkg_init.exists()


def main(root_arg: str = ".") -> int:
    root = pathlib.Path(root_arg).resolve()
    manifest_path = root / "architecture" / "manifest.yaml"
    if not manifest_path.exists():
        print(f"[FAIL] {manifest_path} not found")
        return 1

    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    errors: list[str] = []
    reports: list[str] = []

    kernel = manifest.get("kernel", {})
    for key in ("master_entry", "canonical_llm", "canonical_history"):
        for dotted in kernel.get(key, []) or []:
            if not _module_importable(root, dotted):
                errors.append(f"kernel.{key}: {dotted} 找不到对应文件/包")

    for alias, target in (manifest.get("compat_facades") or {}).items():
        if not _module_importable(root, target):
            errors.append(f"compat_facades: {alias} -> {target}, target 找不到对应文件/包")

    for entry in manifest.get("deprecated") or []:
        name = entry["name"]
        recorded = set(entry.get("known_importers") or [])
        actual = _find_importers(root, name)
        actual -= {name}  # 排除模块自身
        stale = recorded - actual
        new = actual - recorded
        if stale:
            reports.append(
                f"[INFO] {name}: 清单记录但代码里已不再引用 (该更新 known_importers): "
                + ", ".join(sorted(stale))
            )
        if new:
            reports.append(
                f"[WARN] {name}: 代码里新出现但清单没记录的引用 (核实是不是新漂移): "
                + ", ".join(sorted(new))
            )

    forbidden = manifest.get("forbidden_imports") or []
    for rule in forbidden:
        src, dst = rule["from"], rule["to"]
        src_path = root / pathlib.Path(*src.split("."))
        src_path = src_path if src_path.is_dir() else src_path.with_suffix(".py")
        if not src_path.exists():
            continue
        importers = _find_importers(root, dst) & {
            str(p.relative_to(root).with_suffix("")).replace("/", ".")
            for p in (src_path.rglob("*.py") if src_path.is_dir() else [src_path])
        }
        if importers:
            errors.append(
                f"forbidden_imports: {src} -> {dst} 违规, 命中: {', '.join(sorted(importers))}"
            )

    for line in reports:
        print(line)

    if errors:
        print(f"[FAIL] {len(errors)} manifest violation(s):")
        for e in errors:
            print("  -", e)
        return 1

    print(f"[OK] architecture manifest 结构校验通过 ({len(reports)} 条现状提示, 0 条强制违规)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
    except ImportError:
        print("[SKIP] PyYAML 未安装, 跳过 architecture manifest 校验")
        sys.exit(0)
