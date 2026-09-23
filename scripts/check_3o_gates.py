#!/usr/bin/env python3
"""3O 架构门聚合 + SPEC 分类计数（Phase 1 triage 版）。

分类口径（SPEC §35/§43 + Phase 1 triage 裁定）：

  OPRIM_PEER_CALLS_INFO        veya/oprim/__init__ 包重导出（非逻辑互调）
  OPRIM_PEER_CALLS_TYPES       仅 types 共享类型依赖（ALLOW，无 runtime behavior）
  OPRIM_PEER_CALLS_VIOLATIONS  其余 oprim->oprim（必须为 0；vad->audio 已消除）

  OMODUL_DIRECT_CALLS_VIOLATIONS  omodul->omodul 裸调（扣 manifest 例外后必须为 0）
  OMODUL_FROZEN_EXCEPTIONS        scripts/3o_exceptions.json 显式例外（到期删除）

  DIRECT_IO_ALLOWED        # 3O-IO-ALLOW 标记文件（harness substrate 等，本性 IO）
  DIRECT_IO_BASELINED      基线内 B 类存量（逐项有 owner/remediation，见 3O_RECONCILIATION.md）
  DIRECT_IO_NEW_VIOLATIONS 基线外新增（必须为 0；D 类误报修 detector，不进基线）

  DIRECT_MEMORY_WRITE_VIOLATIONS  INSERT INTO memory_* 落在 authority
      （runtime/personal/runtime.py，PersonalRuntimeStore）之外（必须为 0）

  ACTION_GATEWAY_READONLY_INFO    只读执行（git rev-parse/diff/show/status/log，
      失败返回空，不产生 side effect；Phase 5 补只读观测 oprim 后迁移）
  ACTION_GATEWAY_BYPASS_VIOLATIONS  mutating 执行绕过网关（必须为 0）

--strict 阻断任一 VIOLATIONS 桶 > 0。默认报告口径退出码 0。

冻结约束：本脚本只计数分类，不做任务语义选型，不碰主链路。
"""

from __future__ import annotations

import argparse
import ast
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
EXCEPTIONS_FILE = SCRIPTS / "3o_exceptions.json"

MEMORY_AUTHORITY = "runtime/personal/runtime.py"

# 只读 git 观测（无 side effect，失败返回空串）；其余一律按疑似 mutation 处理。
GIT_READONLY = {
    "rev-parse",
    "diff",
    "show",
    "status",
    "log",
    "ls-files",
    "branch",
    "rev-list",
}


def _iter_py(directory: pathlib.Path):
    if directory.is_dir():
        yield from sorted(directory.rglob("*.py"))


def _imports_of(path: pathlib.Path) -> list[tuple[str, int]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, OSError):
        return []
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((a.name, node.lineno) for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.append((node.module, node.lineno))
    return out


def _load_exceptions() -> list[dict]:
    try:
        data = json.loads(EXCEPTIONS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    exceptions = data.get("exceptions", [])
    return [e for e in exceptions if isinstance(e, dict)]


def classify_oprim() -> dict:
    info: list[str] = []
    types: list[str] = []
    violations: list[str] = []
    for path in _iter_py(ROOT / "veya" / "oprim"):
        rel = path.relative_to(ROOT).as_posix()
        for mod, lineno in _imports_of(path):
            if mod == "veya.oprim" or mod.startswith("veya.oprim."):
                entry = f"{rel}:{lineno} -> {mod}"
                if path.name == "__init__.py":
                    info.append(entry)
                elif mod in ("veya.oprim.types",) or mod.startswith("veya.oprim.types."):
                    types.append(entry)
                else:
                    violations.append(entry)
    return {"info": info, "types": types, "violations": violations}


def classify_omodul() -> dict:
    found: list[dict] = []
    for path in _iter_py(ROOT / "veya" / "omodul"):
        if path.name == "__init__.py":
            continue
        rel = path.relative_to(ROOT).as_posix()
        for mod, lineno in _imports_of(path):
            if mod == "veya.omodul" or mod.startswith("veya.omodul."):
                found.append({"file": rel, "line": lineno, "target": mod})
    manifest = _load_exceptions()
    exceptions: list[dict] = []
    violations: list[dict] = []
    for item in found:
        if any(
            e.get("file") == item["file"] and e.get("target") == item["target"]
            for e in manifest
            if e.get("rule") == "OMODUL_DIRECT_OMODUL_CALLS"
        ):
            exceptions.append(item)
        else:
            violations.append(item)
    return {"violations": violations, "exceptions": exceptions}


def classify_direct_io() -> dict:
    r = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "check_no_direct_io.py"),
            str(ROOT),
            "--baseline",
            str(SCRIPTS / "baseline_direct_io.txt"),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    new = sorted(line for line in (r.stdout + r.stderr).splitlines() if line.startswith("[FAIL]"))
    return {"returncode": r.returncode, "new_violations": new}


def classify_memory_writes() -> dict:
    violations: list[str] = []
    scanned = 0
    for scope in ("server", "runtime", "veya", "tools"):
        for path in _iter_py(ROOT / scope):
            if "__pycache__" in path.as_posix():
                continue
            rel = path.relative_to(ROOT).as_posix()
            if rel == MEMORY_AUTHORITY:
                continue
            scanned += 1
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "INSERT INTO memory" in text:
                violations.append(rel)
    return {
        "authority": MEMORY_AUTHORITY,
        "scanned_files": scanned,
        "violations": sorted(set(violations)),
    }


def _git_subcommand(node: ast.Call) -> str | None:
    """解析 subprocess.run([...]) 首参 argv；返回 git 子命令或 None（无法判定）。

    只要求 argv[0]=="git" 且 argv[1] 为常量只读子命令；其余参数（ref、path、
    f-string 插值等）不改变 `diff/rev-parse/...` 的只读性质。
    """
    if not node.args:
        return None
    argv = node.args[0]
    if not isinstance(argv, ast.List) or len(argv.elts) < 2:
        return None
    head, sub = argv.elts[0], argv.elts[1]
    if (
        isinstance(head, ast.Constant)
        and head.value == "git"
        and isinstance(sub, ast.Constant)
        and isinstance(sub.value, str)
    ):
        return sub.value
    if isinstance(head, ast.Constant) and head.value == "git" and not isinstance(sub, ast.Constant):
        return "__dynamic_subcommand__"
    return "__non_git__"


def classify_gateway() -> dict:
    readonly: list[str] = []
    violations: list[str] = []
    for scope in (ROOT / "server" / "goal_run", ROOT / "veya" / "omodul"):
        for path in _iter_py(scope):
            rel = path.relative_to(ROOT).as_posix()
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (SyntaxError, OSError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                func = node.func
                if not (isinstance(func.value, ast.Name) and func.value.id == "subprocess"):
                    continue
                sub = _git_subcommand(node)
                entry = f"{rel}:{node.lineno} subprocess.{func.attr}"
                if sub is not None and sub != "__non_git__" and sub in GIT_READONLY:
                    readonly.append(f"{entry} [git {sub} readonly]")
                else:
                    violations.append(entry)
    return {"readonly_info": sorted(set(readonly)), "violations": sorted(set(violations))}


def _run_gate(script: str, *args: str) -> dict:
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / script), str(ROOT), *args],
        capture_output=True,
        text=True,
        timeout=300,
    )
    return {"script": script, "returncode": r.returncode}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="3O 门聚合 + 分类计数（Phase 1 triage）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--strict", action="store_true", help="任一 VIOLATIONS 桶 >0 则失败")
    args = ap.parse_args(argv)

    gates = [
        _run_gate(
            "check_no_reverse_dep.py",
            "--baseline",
            str(SCRIPTS / "baseline_reverse_dep.txt"),
            "--quiet",
        ),
        _run_gate(
            "check_oskill_pure.py",
            "--baseline",
            str(SCRIPTS / "baseline_oskill.txt"),
            "--quiet",
        ),
        _run_gate(
            "check_no_direct_io.py",
            "--baseline",
            str(SCRIPTS / "baseline_direct_io.txt"),
            "--quiet",
        ),
    ]
    oprim = classify_oprim()
    omodul = classify_omodul()
    dio = classify_direct_io()
    mem = classify_memory_writes()
    gw = classify_gateway()

    report = {
        "existing_gates": gates,
        "OPRIM_PEER_CALLS_INFO": oprim["info"],
        "OPRIM_PEER_CALLS_TYPES": oprim["types"],
        "OPRIM_PEER_CALLS_VIOLATIONS": oprim["violations"],
        "OMODUL_DIRECT_CALLS_VIOLATIONS": omodul["violations"],
        "OMODUL_FROZEN_EXCEPTIONS": omodul["exceptions"],
        "DIRECT_IO_NEW_VIOLATIONS": dio["new_violations"],
        "DIRECT_MEMORY_WRITE_VIOLATIONS": mem["violations"],
        "DIRECT_MEMORY_WRITE_AUTHORITY": mem["authority"],
        "ACTION_GATEWAY_READONLY_INFO": gw["readonly_info"],
        "ACTION_GATEWAY_BYPASS_VIOLATIONS": gw["violations"],
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("== existing gates ==")
        for g in gates:
            print(f"  {g['script']}: returncode={g['returncode']}")
        print(
            f"OPRIM_PEER_CALLS_INFO={len(oprim['info'])} "
            f"TYPES={len(oprim['types'])} VIOLATIONS={len(oprim['violations'])}"
        )
        for v in oprim["violations"]:
            print(f"  [OPRIM-VIOLATION] {v}")
        print(
            f"OMODUL_DIRECT_CALLS_VIOLATIONS={len(omodul['violations'])} "
            f"EXCEPTIONS={len(omodul['exceptions'])}"
        )
        for v in omodul["violations"]:
            print(f"  [OMODUL-VIOLATION] {v}")
        print(f"DIRECT_IO_NEW_VIOLATIONS={len(dio['new_violations'])}")
        for v in dio["new_violations"]:
            print(f"  {v}")
        print(
            f"DIRECT_MEMORY_WRITE_VIOLATIONS={len(mem['violations'])} "
            f"(authority={mem['authority']}, scanned={mem['scanned_files']})"
        )
        for v in mem["violations"]:
            print(f"  [MEMWRITE-VIOLATION] {v}")
        print(
            f"ACTION_GATEWAY_READONLY_INFO={len(gw['readonly_info'])} "
            f"BYPASS_VIOLATIONS={len(gw['violations'])}"
        )
        for v in gw["readonly_info"]:
            print(f"  [READONLY] {v}")
        for v in gw["violations"]:
            print(f"  [BYPASS-VIOLATION] {v}")

    existing_gate_failures = [g for g in gates if g["returncode"] != 0]
    if args.strict and (
        existing_gate_failures
        or oprim["violations"]
        or omodul["violations"]
        or dio["new_violations"]
        or mem["violations"]
        or gw["violations"]
    ):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
