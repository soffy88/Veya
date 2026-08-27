"""commands/upgrade.py — P3-06 Migration / Upgrade.

提供 `veya upgrade --check` (检查更新/版本对比) 与 `veya migrate` (配置/数据迁移)。

架构约束:
- 非交互优先 (CI 友好), `--json` 可脚本化
- 缺省不写入任何破坏性变更 (先 `--check` 再 `--apply`)
- 迁移按版本号有序执行, 可幂等重跑
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from cli.product import _load_config, _save_config

__all__ = ["run_migrate", "run_upgrade"]


# ── 版本元数据：读取当前源码包版本，避免 CLI 与 pyproject 漂移 ───────────
def _source_version() -> str:
    try:
        import tomllib

        data = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
        return str(data["project"]["version"])
    except Exception:
        return "0.6.0"


CURRENT_VERSION = _source_version()
MIN_COMPATIBLE_VERSION = "0.6.0"


# ── 迁移步骤定义 (升序版本) ───────────────────────────────────────────
MIGRATIONS: list[dict[str, Any]] = [
    {
        "from_version": "0.6.0",
        "to_version": "0.7.0",
        "name": "config_llm_structure",
        "description": "规范化 config.llm 结构: provider/model 移入 llm 子对象",
        "apply": lambda cfg: _migrate_llm_structure(cfg),
    },
    {
        "from_version": "0.7.0",
        "to_version": "0.8.0",
        "name": "permission_profiles",
        "description": "引入权限档位 (READ_ONLY/DEVELOPMENT/PRODUCTION) 与默认值",
        "apply": lambda cfg: _migrate_permission_profiles(cfg),
    },
]


def _migrate_llm_structure(cfg: dict[str, Any]) -> dict[str, Any]:
    """旧版扁平 llm/provider/model → 统一 llm 子对象。"""
    llm = cfg.setdefault("llm", {})
    if "provider" in cfg and "provider" not in llm:
        llm["provider"] = cfg.pop("provider")
    if "model" in cfg and "model" not in llm:
        llm["model"] = cfg.pop("model")
    return cfg


def _migrate_permission_profiles(cfg: dict[str, Any]) -> dict[str, Any]:
    """引入 permission_profile 字段, 缺省 DEVELOPMENT。"""
    cfg.setdefault("permission_profile", "DEVELOPMENT")
    return cfg


# ── 公共工具 ──────────────────────────────────────────────────────────


def _get_installed_version() -> str:
    """尝试从 pip metadata 读取已安装版本。"""
    try:
        import importlib.metadata as md

        return md.version("veya")
    except Exception:
        return "unknown"


def _get_latest_version() -> str | None:
    """尝试从 PyPI 获取最新版本 (可选, 网络失败静默)。"""
    try:
        import urllib.request

        with urllib.request.urlopen("https://pypi.org/pypi/veya/json", timeout=5) as resp:
            data = json.loads(resp.read().decode())
        return data["info"]["version"]
    except Exception:
        return None


def _version_tuple(v: str) -> tuple[int, ...]:
    """版本字符串 → 元组 (忽略后缀)。"""
    return tuple(int(p) for p in v.split(".") if p.isdigit())


def _needs_migration(cfg: dict[str, Any]) -> bool:
    """判断配置是否需要迁移 (版本号不匹配)。"""
    stored = cfg.get("version", "0.0.0")
    return _version_tuple(stored) < _version_tuple(CURRENT_VERSION)


# ── CLI: upgrade --check ──────────────────────────────────────────────


def _build_upgrade_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="veya upgrade",
        description="检查 Veya 版本更新与配置迁移状态",
    )
    p.add_argument("--check", action="store_true", help="仅检查, 不执行 (默认)")
    p.add_argument("--apply", action="store_true", help="执行可用的配置迁移")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    p.add_argument("--force", action="store_true", help="强制重跑所有迁移 (慎用)")
    return p


def run_upgrade(argv: list[str]) -> int:
    args = _build_upgrade_parser().parse_args(argv)

    installed = _get_installed_version()
    latest = _get_latest_version()
    cfg = _load_config()
    stored_version = cfg.get("version", "0.0.0")
    needs_mig = _needs_migration(cfg)

    applicable = []
    for m in MIGRATIONS:
        if _version_tuple(stored_version) < _version_tuple(m["to_version"]):
            applicable.append(m)

    result = {
        "installed_version": installed,
        "latest_version": latest,
        "config_version": stored_version,
        "current_code_version": CURRENT_VERSION,
        "needs_migration": needs_mig,
        "applicable_migrations": [
            {
                "name": m["name"],
                "from": m["from_version"],
                "to": m["to_version"],
                "desc": m["description"],
            }
            for m in applicable
        ],
        "update_available": latest is not None
        and _version_tuple(latest) > _version_tuple(installed),
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not result["update_available"] else 1

    # Human output
    print("veya upgrade — 版本检查")
    print(f"  安装版本: {installed}")
    print(f"  配置版本: {stored_version}")
    print(f"  代码基线: {CURRENT_VERSION}")
    if latest:
        print(f"  PyPI 最新: {latest}")
        if _version_tuple(latest) > _version_tuple(installed):
            print("  ⚠  有更新可用: pip install --upgrade veya")
    else:
        print("  PyPI 最新: (离线/不可达)")

    if applicable:
        print(f"\n  待迁移项 ({len(applicable)}):")
        for m in applicable:
            print(
                f"    - {m['name']}: {m['from_version']} → {m['to_version']} ({m['description']})"
            )
    else:
        print("\n  配置已是最新, 无需迁移")

    if args.apply:
        if not applicable:
            print("\n无迁移可执行。")
            return 0
        print("\n执行迁移...")
        for m in applicable:
            cfg = _load_config()
            cfg = m["apply"](cfg)
            cfg["version"] = m["to_version"]
            _save_config(cfg)
            print(f"  ✔ {m['name']} → {m['to_version']}")
        print("\n迁移完成。建议运行 `veya doctor` 复检。")
    else:
        print("\n提示: 加 --apply 执行上述迁移, 或 `veya migrate` (同义)")

    return 0


# ── CLI: migrate ──────────────────────────────────────────────────────


def _build_migrate_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="veya migrate",
        description="执行配置/数据迁移 (升级后运行)",
    )
    p.add_argument("--check", action="store_true", help="仅列出待迁移项")
    p.add_argument("--apply", action="store_true", help="执行迁移 (默认行为, 与无参数相同)")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    p.add_argument("--force", action="store_true", help="强制重跑所有迁移")
    return p


def run_migrate(argv: list[str]) -> int:
    args = _build_migrate_parser().parse_args(argv)

    cfg = _load_config()
    stored_version = cfg.get("version", "0.0.0")

    applicable = []
    for m in MIGRATIONS:
        if args.force or _version_tuple(stored_version) < _version_tuple(m["to_version"]):
            applicable.append(m)

    if args.check or args.json:
        result = {
            "config_version": stored_version,
            "target_version": CURRENT_VERSION,
            "migrations": [
                {
                    "name": m["name"],
                    "from": m["from_version"],
                    "to": m["to_version"],
                    "desc": m["description"],
                }
                for m in applicable
            ],
        }
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            if applicable:
                print("待执行迁移:")
                for m in applicable:
                    print(
                        f"  - {m['name']}: {m['from_version']} → {m['to_version']} ({m['description']})"
                    )
            else:
                print("无待执行迁移。")
        return 0

    if not applicable:
        print("无待执行迁移。")
        return 0

    print(f"veya migrate — {stored_version} → {CURRENT_VERSION}")
    for m in applicable:
        cfg = _load_config()
        cfg = m["apply"](cfg)
        cfg["version"] = m["to_version"]
        _save_config(cfg)
        print(f"  ✔ {m['name']} → {m['to_version']}")

    print("\n迁移完成。")
    return 0


# ── 向后兼容: 旧 CLI 入口别名 ────────────────────────────────────────


def run_upgrade_cli(argv: list[str]) -> int:
    """cli.main 兼容入口。"""
    return run_upgrade(argv)


def run_migrate_cli(argv: list[str]) -> int:
    """cli.main 兼容入口。"""
    return run_migrate(argv)
