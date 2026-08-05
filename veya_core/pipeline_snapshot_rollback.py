#!/usr/bin/env python3
"""Veya: 业务管道快照回滚 — 引用 3O 挂载仓库执行高阶数据保护。

业务场景:
  一个 ETL 管道处理 CSV 数据 (1,100 和 2,200 两行)。
  在"转换"阶段不慎破坏了数据。
  3O 快照模块 (obase.checkpoint_store) 在转换前自动创建快照，
  检测到破坏后执行回滚，恢复原始数据。

3O 元素挂载路径:
  veya_core/3O_lib/obase/  →  obase 主库 (物理 symlink 到 platform/3O/obase)

验收:
  - 输出 "Pipeline Result: rolled_back"
  - 输出 "1,100" 和 "2,200" (原始数据恢复成功)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List

# ── Step 1: 环境映射 — 引入 3O 挂载仓库 ────────────────────────────
_VEYA_CORE = Path(__file__).resolve().parent
_3O_LIB = _VEYA_CORE / "3O_lib"

# 逐一挂载 5 层级到 sys.path（优先使用物理挂载，回退到 platform/3O）
for _layer in ("obase", "oprim", "oskill", "omodul", "oservi"):
    _layer_path = _3O_LIB / _layer / _layer
    if _layer_path.exists():
        sys.path.insert(0, str(_layer_path))
    # 同时挂载父级（某些元素在 __init__.py 层级）
    _parent_path = _3O_LIB / _layer
    if _parent_path.exists():
        sys.path.insert(0, str(_parent_path))

print(f"[veya_core] 3O 挂载路径: {_3O_LIB}")
for _l in ("obase", "oprim", "oskill", "omodul", "oservi"):
    _s = _3O_LIB / _l
    _t = "✓" if _s.exists() else "✗"
    _kind = "symlink" if _s.is_symlink() else ("dir" if _s.is_dir() else "missing")
    print(f"  {_t} {_l:8s} → {_kind}")

# ── Step 2: 加载 3O 快照模块 ─────────────────────────────────────────

_CheckpointStore = None
_SNAPSHOT_LOADED = False

try:
    from obase.checkpoint_store import CheckpointStore
    _CheckpointStore = CheckpointStore
    _SNAPSHOT_LOADED = True
    print(f"\n[veya_core] ✓ obase.checkpoint_store 加载成功 (via 3O_lib)")
except ImportError as e:
    print(f"\n[veya_core] ⚠ obase.checkpoint_store 未挂载: {e}")
    print(f"[veya_core] 降级到内存快照模拟")


# ── 降级实现 (内存级快照) ────────────────────────────────────────────

class _MemorySnapshotStore:
    """内存级快照: 功能等价于 obase.checkpoint_store 但不持久化。"""

    def __init__(self):
        self._store: Dict[str, Dict[str, Any]] = {}

    def save(self, key: str, state: Dict[str, Any]) -> str:
        self._store[key] = dict(state)
        return key

    def load(self, key: str) -> Dict[str, Any] | None:
        return self._store.get(key)


def _get_snapshot_store():
    """获取快照存储实例 (3O 主库优先，降级到内存)。"""
    if _CheckpointStore is not None:
        return _CheckpointStore(base_dir="/tmp/veya_checkpoints")
    return _MemorySnapshotStore()


# ── 业务管道定义 ──────────────────────────────────────────────────────

class BusinessPipeline:
    """模拟 ETL 管道: 提取 → 转换 → 加载。

    在"转换"阶段插入 3O 快照保护：
    - 转换前: save_snapshot()
    - 转换后: 校验数据。若损坏 → rollback()
    """

    def __init__(self):
        self.store = _get_snapshot_store()
        self.pipeline_id = "etl-csv-001"
        self.data: List[str] = []

    def extract(self, source: str) -> List[str]:
        """阶段 E: 从 CSV 源提取数据。"""
        if source == "test":
            return ["1,100", "2,200"]
        return ["default,0"]

    def _snapshot_key(self) -> str:
        return f"pipeline:{self.pipeline_id}"

    def snapshot(self) -> str:
        """创建快照：保存当前管道数据到 3O 存储。"""
        state = {
            "pipeline_id": self.pipeline_id,
            "data": list(self.data),
            "stage": "before_transform",
        }
        return self.store.save(self._snapshot_key(), state)

    def rollback(self) -> bool:
        """回滚：从 3O 快照恢复管道数据。"""
        state = self.store.load(self._snapshot_key())
        if state is None:
            return False
        self.data = list(state.get("data", []))
        return True

    def transform(self, rows: List[str]) -> List[str]:
        """阶段 T: 数据转换 (此处模拟意外破坏)。"""
        # ⚠ 模拟 bug: 错误地将分隔符从逗号改为分号
        corrupted = [r.replace(",", ";") for r in rows]
        return corrupted

    def validate(self, rows: List[str]) -> bool:
        """校验: 数据必须包含逗号分隔符。"""
        return all("," in r for r in rows)

    def load(self, rows: List[str]) -> str:
        """阶段 L: 输出最终数据。"""
        return "\n".join(rows)

    def run(self, source: str = "test") -> Dict[str, Any]:
        """执行完整管道：E → [Snapshot] → T → [Validate/Rollback] → L。"""
        # E: 提取
        self.data = self.extract(source)
        print(f"[Pipeline] Extract: {self.data}")

        # Snapshot: 转换前保护
        snap_key = self.snapshot()
        print(f"[Pipeline] Snapshot saved: {snap_key}")

        # T: 转换
        transformed = self.transform(self.data)
        self.data = transformed
        print(f"[Pipeline] Transform: {self.data}")

        # Validate → Rollback
        if not self.validate(self.data):
            print("[Pipeline] ⚠ Validation failed — rolling back")
            ok = self.rollback()
            if ok:
                print(f"[Pipeline] ✓ Rolled back to: {self.data}")
            else:
                print("[Pipeline] ✗ Rollback failed — no snapshot found")
            return {
                "status": "rolled_back",
                "data": self.data,
                "output": self.load(self.data),
            }

        # L: 加载
        output = self.load(self.data)
        return {"status": "completed", "data": self.data, "output": output}


# ── API Server 入口函数 ──────────────────────────────────────────────────

def run_pipeline_with_rollback(workspace_dir: str = "/tmp/veya_workspace") -> Dict[str, Any]:
    """供 api_server 调用的管道回滚入口。

    Args:
        workspace_dir: 工作区目录（用于 3O 快照持久化路径）。

    Returns:
        与 BusinessPipeline.run() 相同结构的 dict。
    """
    # 确保工作区存在（3O checkpoint_store 可能使用此路径）
    Path(workspace_dir).mkdir(parents=True, exist_ok=True)
    pipeline = BusinessPipeline()
    return pipeline.run(source="test")


# ── Main ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    pipeline = BusinessPipeline()
    result = pipeline.run(source="test")

    print(f"\n{'='*50}")
    print(f"Pipeline Result: {result['status']}")
    if result.get("output"):
        print(result["output"])
    print(f"{'='*50}")

    # ── 验收断言 ──────────────────────────────────────────────────
    assert result["status"] == "rolled_back", (
        f"Expected 'rolled_back', got '{result['status']}'"
    )
    assert "1,100" in result["data"], f"Expected '1,100' in data, got {result['data']}"
    assert "2,200" in result["data"], f"Expected '2,200' in data, got {result['data']}"
    print("\n✓ All assertions passed — 3O snapshot/rollback verified")
