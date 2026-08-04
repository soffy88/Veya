#!/usr/bin/env python3
"""3O 附录 B: check_manifest.py

§2.5: 主库必须暴露 __manifest__（元素清单 + 签名 + 版本）。

检查 veya/obase/__init__.py：
1. 必须定义 __manifest__ dict；
2. 每个清单项 name 形如 "<module>.<attr>"，须能实际 import；
3. 至少 1 项。

用法：python scripts/check_manifest.py [root]
退出码：0 = 通过；1 = 失败
"""

from __future__ import annotations

import importlib
import pathlib
import sys


def main(root: str = ".") -> int:
    init = pathlib.Path(root) / "veya" / "obase" / "__init__.py"
    if not init.exists():
        print(f"[FAIL] {init} not found")
        return 1
    sys.path.insert(0, str(pathlib.Path(root).resolve()))
    try:
        pkg = importlib.import_module("veya.obase")
    except Exception as exc:  # noqa: BLE001, RUF100
        print(f"[FAIL] cannot import veya.obase: {exc}")
        return 1
    manifest = getattr(pkg, "__manifest__", None)
    if not isinstance(manifest, dict):
        print("[FAIL] veya/obase/__init__.py missing dict __manifest__ (§2.5)")
        return 1
    if not manifest:
        print("[FAIL] __manifest__ is empty")
        return 1
    errors: list[str] = []
    for name, meta in manifest.items():
        if "." not in name:
            errors.append(f"manifest key '{name}' must be '<module>.<attr>'")
            continue
        mod_name, attr = name.split(".", 1)
        try:
            mod = importlib.import_module(f"veya.obase.{mod_name}")
        except Exception as exc:  # noqa: BLE001, RUF100
            errors.append(f"manifest '{name}': cannot import module: {exc}")
            continue
        if not hasattr(mod, attr):
            errors.append(f"manifest '{name}': module has no attribute '{attr}'")
        if not isinstance(meta, dict) or "signature" not in meta:
            errors.append(f"manifest '{name}': missing 'signature' metadata")
    if errors:
        print(f"[FAIL] {len(errors)} manifest issue(s):")
        for e in errors:
            print("  -", e)
        return 1
    print(f"[OK] __manifest__ valid ({len(manifest)} elements)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
