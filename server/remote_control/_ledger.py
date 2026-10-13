# 3O-IO-ALLOW — server/remote_control/_ledger.py
# 理由：W13 的四本 JSON 账簿（hosts / sessions / wake-policy / file-handoff）
# 全部按仓库既有模式落盘 —— 读用 read_text、写复用
# server/bot_team/conversations.py:50 的 atomic_write_json（tmp + fsync +
# os.replace）。同形模块 server/supervisor/binding.py:1-8 已用同样的显式
# # 3O-IO-ALLOW 标记豁免；新增第二套原子写实现会违反「不新增第二套存储」的
# 冻结约束。因此本文件是 W13 唯一做物理 I/O 的地方，其余模块只经它读写。
"""Shared JSON-map ledger for the W13 remote-control stores.

Every W13 store is a flat ``id -> record`` JSON file. This module owns the
only physical I/O in the package so the rest stays pure:

* write — always via :func:`server.bot_team.conversations.atomic_write_json`
  (tmp file + ``fsync`` + ``os.replace``). W13 never calls ``open()`` for
  writing and never calls ``os.replace`` itself.
* read  — ``read_text`` + ``json.loads``, degrading to an empty map for a
  missing, unreadable or corrupt file (a corrupt ledger must not make the
  remote-control surface 500).

Each store supplies its own path; the path may be overridden per-process with
an environment variable so tests never touch the real home directory.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

_lock = threading.RLock()

#: Every ledger path W13 owns. Used by the test-suite isolation helper.
LEDGER_ENV_VARS: tuple[str, ...] = (
    "VEYA_REMOTE_CONTROL_HOSTS_PATH",
    "VEYA_REMOTE_CONTROL_SESSIONS_PATH",
    "VEYA_REMOTE_CONTROL_WAKE_PATH",
    "VEYA_REMOTE_CONTROL_FILES_PATH",
)


def default_path(env_var: str, filename: str) -> Path:
    """Resolve one ledger path: env override, else ``~/.veya/<filename>``.

    ``Path.home()`` and ``expanduser()`` are pure string operations and cost no
    direct-IO finding.
    """

    raw = os.environ.get(env_var, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".veya" / filename


def read_map(path: Path) -> dict[str, dict[str, Any]]:
    """Read one ledger. Missing / unreadable / corrupt → ``{}``."""

    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def write_map(path: Path, rows: dict[str, dict[str, Any]]) -> None:
    """Write one ledger atomically through the repository's single writer."""

    from server.bot_team.conversations import atomic_write_json

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, rows)


def update(path: Path, mutate) -> Any:
    """Read-modify-write one ledger under the package lock.

    ``mutate`` receives the whole map and returns ``(rows, result)``; ``result``
    is handed back to the caller so a store method can return its own record
    without a second read. Exceptions from ``mutate`` propagate with the file
    untouched (nothing is written until ``mutate`` returns).
    """

    with _lock:
        rows = read_map(path)
        updated, result = mutate(rows)
        write_map(path, updated)
        return result


__all__ = [
    "LEDGER_ENV_VARS",
    "default_path",
    "read_map",
    "update",
    "write_map",
]
