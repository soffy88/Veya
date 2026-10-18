"""Lazy bridge to the external ``harness2`` checkout.

harness2 是平行仓库（默认 ``~/projects/harness2``），不是 Veya 的 submodule，
因此 Phase 0 不 vendoring 它、不改动 Veya 的 CI/结构，只在运行期按需 import。
路径可通过 ``HARNESS2_REPO`` 覆盖，缺失时给出清晰报错而不是静默失败。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HARNESS2_REPO = Path(
    os.environ.get("HARNESS2_REPO", str(Path.home() / "projects" / "harness2"))
).expanduser()


def ensure_harness2_on_path() -> None:
    """把 harness2 checkout 加到 sys.path（幂等）。"""
    if HARNESS2_REPO.is_dir() and str(HARNESS2_REPO) not in sys.path:
        sys.path.insert(0, str(HARNESS2_REPO))


def import_harness2_core():
    """返回 ``harness2.core`` 模块；不可用时抛清晰错误。

    ``harness2.core`` 本身不 re-export 子模块，所以这里显式 import 常用的
    ``spec`` / ``tree`` / ``proposer`` / ``schedule`` 并挂到返回对象上。
    """
    ensure_harness2_on_path()
    try:
        import harness2.core as core  # type: ignore
        import harness2.core.proposer as proposer  # type: ignore
        import harness2.core.schedule as schedule  # type: ignore
        import harness2.core.spec as spec  # type: ignore
        import harness2.core.tree as tree  # type: ignore
    except ImportError as exc:  # pragma: no cover - 依赖环境
        raise ImportError(
            f"harness2 不在可导入路径（HARNESS2_REPO={HARNESS2_REPO}）。"
            " Phase 0 全循环需要该仓库；只读导出阶段不依赖它。"
        ) from exc
    core.spec = spec
    core.tree = tree
    core.proposer = proposer
    core.schedule = schedule
    return core
