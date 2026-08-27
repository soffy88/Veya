"""Veya Memory Bank: 全局偏好与习惯纠正账本 (Global Preference Ledger)。

完全独立于聊天上下文 (Context Window) 的跨会话潜意识:
- 隐式捕捉: 用户不需要刻意发指令,主脑感受到"纠正"或"习惯"时,
  静默调用内置工具把规则写进硬盘。
- 永久生效: 账本在 ~/.veya/global_memory.json,重启不丢。
- 潜意识注入: inject_subconscious() 把账本编译为每次对话必读的
  "[YOUR SUBCONSCIOUS MEMORY & USER PREFERENCES]" 指令块。
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

# 账本容量上限(防止无限膨胀;超出时裁剪最旧条目)
MAX_PREFERENCES = 200

_DEFAULT_STORAGE_PATH = str(Path.home() / ".veya" / "global_memory.json")


class VeyaMemoryBank:
    """全局偏好账本: 用户规则的永久存储与潜意识编译。"""

    def __init__(self, storage_path: str | Path | None = None):
        # env 覆盖 > 参数 > 默认 ~/.veya/global_memory.json
        self.storage_path = Path(
            storage_path or os.environ.get("VEYA_MEMORY_PATH", _DEFAULT_STORAGE_PATH)
        ).expanduser()
        self._lock = threading.RLock()
        self.memory = self._load_memory()

    # ── 持久化 ───────────────────────────────────────────────────────
    def _load_memory(self) -> dict:
        if self.storage_path.exists():
            try:
                with open(self.storage_path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get("preferences"), list):
                    return data
            except (json.JSONDecodeError, OSError):
                pass  # 损坏 → 重建空账本
        return {"preferences": []}

    def _save_memory(self) -> None:
        """原子写 (tmp + rename), 防止中途崩溃写坏 JSON。"""
        with self._lock:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.storage_path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.memory, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.storage_path)

    # ── 写入 ─────────────────────────────────────────────────────────
    def add_preference(self, rule: str, context: str = "General") -> str:
        """被大模型调用的物理写入动作(隐式捕捉的落盘点)。

        - 完全相同的规则不重复添加。
        - 账本超上限时裁剪最旧条目。
        """
        rule = str(rule).strip()
        context = str(context).strip() or "General"
        if not rule:
            return "拒绝写入: rule 不能为空。"

        with self._lock:
            # 防止重复添加相同的规则
            for p in self.memory["preferences"]:
                if p["rule"] == rule:
                    return f"Rule already exists. (ID: {p['id']})"

            entry = {
                "id": f"mem_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:4]}",
                "rule": rule,
                "context": context,
                "timestamp": datetime.now().isoformat(),
            }
            self.memory["preferences"].append(entry)

            # 容量管理: 超出上限裁剪最旧
            if len(self.memory["preferences"]) > MAX_PREFERENCES:
                dropped = self.memory["preferences"][
                    : len(self.memory["preferences"]) - MAX_PREFERENCES
                ]
                self.memory["preferences"] = self.memory["preferences"][-MAX_PREFERENCES:]
                self._save_memory()
                return f"✅ 永久记忆已更新: {rule} (账本已达上限, 已裁剪 {len(dropped)} 条最旧记忆)"

            self._save_memory()
            return f"✅ 永久记忆已更新: {rule}"

    def remove_preference(self, memory_id: str) -> str:
        """允许大模型自我清理过期的记忆。"""
        with self._lock:
            original_len = len(self.memory["preferences"])
            self.memory["preferences"] = [
                p for p in self.memory["preferences"] if p["id"] != memory_id
            ]
            if len(self.memory["preferences"]) < original_len:
                self._save_memory()
                return f"记忆 {memory_id} 已擦除。"
            return f"未找到该记忆 ID: {memory_id}"

    def clear(self) -> None:
        """清空账本(重置/测试用)。"""
        with self._lock:
            self.memory = {"preferences": []}
            self._save_memory()

    # ── 读取 ─────────────────────────────────────────────────────────
    def list_preferences(self) -> list[dict]:
        return list(self.memory.get("preferences", []))

    def search_preferences(self, query: str) -> list[dict]:
        q = query.lower()
        return [
            p
            for p in self.memory.get("preferences", [])
            if q in p["rule"].lower() or q in p["context"].lower()
        ]

    def get_stats(self) -> dict:
        return {
            "storage_path": str(self.storage_path),
            "count": len(self.memory.get("preferences", [])),
            "contexts": sorted(
                {p.get("context", "General") for p in self.memory.get("preferences", [])}
            ),
        }

    # ── 潜意识注入 (核心) ────────────────────────────────────────────
    def inject_subconscious(self) -> str:
        """【核心】: 将记忆账本编译为大模型每次对话必读的『潜意识指令』。

        空账本返回空串(不污染 System Prompt)。
        """
        prefs = self.memory.get("preferences", [])
        if not prefs:
            return ""

        lines = [
            "\n=========================================",
            "[YOUR SUBCONSCIOUS MEMORY & USER PREFERENCES]",
            "You MUST strictly obey the following rules across all sessions:",
        ]
        lines += [f"- [{p.get('context', 'General')}] {p['rule']} (ID: {p['id']})" for p in prefs]
        lines.append("=========================================\n")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {"storage_path": str(self.storage_path), "preferences": self.list_preferences()}


# 模块级单例(server 复用; 测试可注入独立实例)
memory_bank = VeyaMemoryBank()
