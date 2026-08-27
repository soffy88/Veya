#!/usr/bin/env python3
"""严格 3O 迁移 · 阶段 3 强制检查: check_no_direct_io.py

「任何业务代码直接 subprocess / open() / 网络请求 = 违规」——所有 I/O
必须经 oprim 原子操作（默认经 obase.container 句柄注入）。

扫描的业务层（默认）:
    veya/omodul veya/oskill veya/oservi agents cli commands server hooks
    session tools registries streaming subagent tui config auth permission
    services security infra mode

豁免（自身就是 I/O 的层/目录）:
    veya/obase   — 句柄层 (telemetry/sandbox/llm 通道的实现地)
    veya/oprim   — 原子层 (本阶段交付物, 物理触手)
    veya/oskill/pure — 纯函数层 (已有 check_oskill_pure 强制)
    platform/3O  — 旧主库参考 (只读)
    tests scripts docs — 非业务代码
    *.py 内 `# 3O-IO-ALLOW` 注释标记的文件 — 显式豁免 (逐文件, 需写理由)

检测类别:
    EXEC   — subprocess/os.system/os.popen/os.exec*/os.spawn*
    NET    — socket/requests/httpx/aiohttp/urllib
    FILE_W — open(w|a|r+) / Path.write_* / os.remove/mkdir/rename/... / shutil.*
    FILE_R — open(r) / Path.read_* / os.listdir/scandir/walk (只读也归 oprim)
    STDIO  — input() (交互输入) / print() 归 obase 可观测性, 不拦

用法:
    # 生成基线 (存量业务 I/O 放行, 只拦新增)
    python scripts/check_no_direct_io.py . --write-baseline scripts/baseline_direct_io.txt
    # CI
    python scripts/check_no_direct_io.py . --baseline scripts/baseline_direct_io.txt
    # 严格 (全量违规失败)
    python scripts/check_no_direct_io.py . --strict
退出码: 0 = 通过; 1 = 违规
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import sys
from collections.abc import Iterable

BUSINESS_DIRS: tuple[str, ...] = (
    "veya/omodul",
    "veya/oskill",
    "veya/oservi",
    "agents",
    "cli",
    "commands",
    "server",
    "hooks",
    "session",
    "tools",
    "registries",
    "streaming",
    "subagent",
    "tui",
    "config",
    "auth",
    "permission",
    "services",
    "security",
    "infra",
    "mode",
)

ALLOWED_DIRS: tuple[str, ...] = (
    "veya/obase",
    "veya/oprim",
    "veya/oskill/pure",
    "veya/platform",
    "platform",
    "tests",
    "scripts",
    "docs",
    "site",
    "deploy",
    "services/loop-plane",  # 独立微服务（infra 层本职即 I/O, 有自己的存储/执行面）
)

# 直接 I/O 的导入根
IO_IMPORTS: tuple[str, ...] = (
    "subprocess",
    "socket",
    "requests",
    "httpx",
    "aiohttp",
    "urllib",
    "shutil",
    "tempfile",
    "pty",
    "fcntl",
    "select",
    "selectors",
)

# os.* 直连调用黑名单 (破坏/执行/读写类)
OS_CALLS: tuple[str, ...] = (
    "system",
    "popen",
    "spawnl",
    "spawnle",
    "spawnlp",
    "spawnlpe",
    "spawnv",
    "spawnve",
    "spawnvp",
    "spawnvpe",
    "execv",
    "execve",
    "execl",
    "execle",
    "execlp",
    "execvp",
    "execvpe",
    "posix_spawn",
    "posix_spawnp",
    "remove",
    "unlink",
    "mkdir",
    "makedirs",
    "rmdir",
    "removedirs",
    "rename",
    "replace",
    "chmod",
    "chown",
    "symlink",
    "link",
    "listdir",
    "scandir",
    "walk",
    "fdopen",
    "open",
    "write",
    "read",
    "chdir",
    "fchdir",
    "setenv",
    "putenv",
    "kill",
    "killpg",
    "truncate",
    "ftruncate",
    "utime",
    "startfile",
    "mkfifo",
    "mknod",
)

# pathlib.Path 直连方法
PATH_METHODS: tuple[str, ...] = (
    "read_text",
    "write_text",
    "read_bytes",
    "write_bytes",
    "open",
    "mkdir",
    "unlink",
    "rename",
    "replace",
    "rmdir",
    "iterdir",
    "glob",
    "rglob",
    "touch",
    "chmod",
    "symlink_to",
    "hardlink_to",
    "copy",
    "copyfile",
    "readlink",
    "stat",
    "exists",
    "is_file",
    "is_dir",
)

# shutil 直连
SHUTIL_CALLS: tuple[str, ...] = (
    "copy",
    "copy2",
    "copyfile",
    "copyfileobj",
    "copytree",
    "move",
    "rmtree",
    "make_archive",
    "unpack_archive",
    "which",
    "disk_usage",
)

FILE_OPEN_MODES = ("w", "a", "r+", "w+", "a+", "x", "wb", "ab", "rb", "r")


def _iter_py_files(root: pathlib.Path, targets: list[str]) -> Iterable[pathlib.Path]:
    for t in targets:
        d = root / t
        if d.is_dir():
            yield from sorted(d.rglob("*.py"))
        elif d.is_file():
            yield d


def _is_allowed(path: pathlib.Path, root: pathlib.Path) -> bool:
    rel = path.relative_to(root).as_posix()
    return any(rel == allowed or rel.startswith(allowed + "/") for allowed in ALLOWED_DIRS)


def _has_allow_marker(path: pathlib.Path) -> bool:
    """文件头部 200 行内含 `# 3O-IO-ALLOW` → 显式豁免 (需写理由)。"""
    try:
        head = path.read_text(encoding="utf-8", errors="ignore").splitlines()[:200]
    except OSError:
        return False
    return any("# 3O-IO-ALLOW" in line for line in head)


def _call_kind(node: ast.Call) -> str | None:
    """返回调用类别 (EXEC/NET/FILE_W/FILE_R) 或 None。"""
    fn = node.func
    if isinstance(fn, ast.Name):
        if fn.id == "open":
            mode = "r"
            if node.args and len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                mode = str(node.args[1].value)
            return (
                "FILE_W"
                if (
                    mode.startswith(("w", "a", "x", "r+", "w+", "a+"))
                    or ("b" in mode and mode.startswith(("w", "a")))
                )
                else "FILE_R"
            )
        if fn.id == "input":
            return "STDIO"
        return None
    if isinstance(fn, ast.Attribute):
        base = fn.value
        base_name: str | None = None
        # 展开链取最左基名
        cur = base
        while isinstance(cur, ast.Attribute):
            cur = cur.value
        if isinstance(cur, ast.Name):
            base_name = cur.id
        attr = fn.attr
        if base_name == "os" and attr in OS_CALLS:
            if attr in ("listdir", "scandir", "walk", "read", "fdopen", "open"):
                return "FILE_R"
            if attr in (
                "chmod",
                "chown",
                "utime",
                "kill",
                "killpg",
                "truncate",
                "ftruncate",
                "startfile",
            ):
                return "EXEC"
            return (
                "FILE_W"
                if attr
                in (
                    "remove",
                    "unlink",
                    "mkdir",
                    "makedirs",
                    "rmdir",
                    "removedirs",
                    "rename",
                    "replace",
                    "symlink",
                    "link",
                    "write",
                    "setenv",
                    "putenv",
                    "chdir",
                    "fchdir",
                    "mkfifo",
                    "mknod",
                )
                else "EXEC"
            )
        if base_name == "shutil" and attr in SHUTIL_CALLS:
            return "FILE_W"
        if base_name in ("Path", "PosixPath", "WindowsPath", "PurePath"):
            return (
                "FILE_R"
                if attr
                in (
                    "read_text",
                    "read_bytes",
                    "readlink",
                    "stat",
                    "exists",
                    "is_file",
                    "is_dir",
                    "iterdir",
                    "glob",
                    "rglob",
                )
                else "FILE_W"
            )
        # subprocess.run / socket.socket / httpx.get 等
        if base_name == "subprocess":
            return "EXEC"
        if base_name in ("socket", "requests", "httpx", "aiohttp", "urllib"):
            return "NET"
        # Path(...).open / (Path("x")).read_text —— 链式
        if (
            isinstance(base, ast.Call)
            and isinstance(base.func, ast.Name)
            and base.func.id == "Path"
        ):
            return (
                "FILE_R"
                if attr in ("read_text", "read_bytes", "exists", "is_file", "is_dir")
                else "FILE_W"
            )
    return None


def check_file(path: pathlib.Path, root: pathlib.Path) -> list[str]:
    rel = path.relative_to(root).as_posix()
    violations: list[str] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, OSError):
        return violations
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                top = a.name.split(".")[0]
                if top in IO_IMPORTS:
                    violations.append(f"{rel}:{node.lineno} EXEC/NET import {a.name!r}")
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                top = node.module.split(".")[0]
                if top in IO_IMPORTS:
                    violations.append(f"{rel}:{node.lineno} EXEC/NET import {node.module!r}")
        elif isinstance(node, ast.Call):
            kind = _call_kind(node)
            if kind and kind != "STDIO":
                label = f"{ast.unparse(node.func)[:50]}"
                violations.append(f"{rel}:{node.lineno} {kind} {label}")
    return violations


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="业务层直接 I/O 强制检查")
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("--targets", nargs="+", default=None, help="覆盖默认业务目录")
    ap.add_argument("--baseline", default=None)
    ap.add_argument("--write-baseline", default=None)
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    root = pathlib.Path(args.root).resolve()
    targets = args.targets or list(BUSINESS_DIRS)

    all_violations: list[str] = []
    for path in _iter_py_files(root, targets):
        if _is_allowed(path, root) or _has_allow_marker(path):
            continue
        all_violations.extend(check_file(path, root))
    all_violations = sorted(set(all_violations))

    if args.write_baseline:
        out = pathlib.Path(args.write_baseline)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            "\n".join(all_violations) + ("\n" if all_violations else ""), encoding="utf-8"
        )
        print(f"[BASELINE] {len(all_violations)} 条直接 I/O 违规已写入 {out}")
        return 0

    if not args.strict and args.baseline:
        bp = pathlib.Path(args.baseline)
        baseline = set(bp.read_text(encoding="utf-8").splitlines()) if bp.is_file() else set()
        new = [v for v in all_violations if v not in baseline]
    else:
        new = all_violations

    if new:
        for v in sorted(new):
            print(f"[FAIL] {v}")
        print(
            f"\n共 {len(new)} 处新增直接 I/O（存量 {len(all_violations) - len(new)} 条在基线内）。"
        )
        return 1
    if not args.quiet:
        print(
            f"[OK] 业务层直接 I/O 检查通过（扫描 {len(targets)} 个目录, 基线放行 {len(all_violations)} 条存量）。"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
