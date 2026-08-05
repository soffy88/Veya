"""Veya Genesis: 专属 Agent 的永久记忆管理系统 (Memory Bank / Infinite Context)。

"永远记得上下文"在工程上不能靠硬塞 Token(会破产且变傻),而是靠记忆外化。
Genesis 拥有双重记忆:

- element_ledger (结构化账本): 记录它锻造过的 3O 元素 —— 谁写的、版本号、
  描述。永不遗忘的全局视野,每次醒来注入 System Prompt 即"潜意识"。
- experience_log (经验教训): 记录踩过的坑 (mistake -> lesson),
  JSONL 追加式落盘(只增不减,崩溃不丢),注入时只取最近 N 条防止 Token 爆炸。

存储布局 (storage_dir):
- memory.json        主账本 (原子写: tmp + rename,防写坏)
- experiences.jsonl  追加式经验日志 (append-only)
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

# 注入 System Prompt 时最多携带的经验条数(防止 Token 爆炸)
_DEFAULT_MAX_EXPERIENCES = 5
# 加载 JSONL 时最多回溯的经验条数(内存驻留上限)
_MAX_LOADED_EXPERIENCES = 200

_MEMORY_FILENAME = "memory.json"
_EXPERIENCES_FILENAME = "experiences.jsonl"


class GenesisMemory:
    """Genesis 的永久记忆库: 账本 + 经验,持久化于本地磁盘。"""

    def __init__(self, storage_dir: str | Path | None = None):
        self.storage_dir = Path(storage_dir or (Path.home() / ".veya" / "genesis"))
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.memory: dict[str, Any] = self._load_memory()

    # ── 持久化 ───────────────────────────────────────────────────────
    @property
    def _memory_path(self) -> Path:
        return self.storage_dir / _MEMORY_FILENAME

    @property
    def _experiences_path(self) -> Path:
        return self.storage_dir / _EXPERIENCES_FILENAME

    def _load_memory(self) -> dict:
        """加载主账本;不存在则初始化永久记忆结构。"""
        if self._memory_path.exists():
            with open(self._memory_path, encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {}
        base = {
            "element_ledger": {},  # {"layer/name": {"description", "created_at", "updated_at", "version"}}
            "experience_log": [],  # [{"date", "mistake", "lesson", "context"}]
            "last_active": None,
        }
        base.update(data)
        # 合并 JSONL 追加式经验(主文件可能落后于追加日志)
        base["experience_log"] = self._merge_experiences(
            base.get("experience_log", []), self._load_experience_lines()
        )
        return base

    @staticmethod
    def _merge_experiences(main_log: list, jsonl_log: list) -> list:
        merged = list(main_log) + jsonl_log
        return merged[-_MAX_LOADED_EXPERIENCES:]

    def _load_experience_lines(self) -> list[dict]:
        if not self._experiences_path.exists():
            return []
        lines = self._experiences_path.read_text(encoding="utf-8").splitlines()
        entries = []
        for line in lines[-_MAX_LOADED_EXPERIENCES:]:
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 损坏的单行直接跳过,不拖垮整个记忆库
        return entries

    def save(self) -> None:
        """原子写主账本 (tmp + rename),防止中途崩溃写坏 JSON。"""
        with self._lock:
            self.memory["last_active"] = datetime.now().isoformat()
            tmp = self._memory_path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.memory, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._memory_path)

    # ── 记忆写入 ─────────────────────────────────────────────────────
    def record_element(self, layer: str, name: str, description: str) -> None:
        """记忆: 我创建/升级了某个 3O 元素(自动版本号递增)。"""
        with self._lock:
            key = f"{layer}/{name}"
            now = datetime.now().isoformat()
            entry = self.memory["element_ledger"].get(key)
            if entry is None:
                self.memory["element_ledger"][key] = {
                    "description": description,
                    "created_at": now,
                    "updated_at": now,
                    "version": 1,
                }
            else:
                entry["description"] = description
                entry["updated_at"] = now
                entry["version"] = int(entry.get("version", 1)) + 1
            self.save()

    def record_experience(self, mistake: str, lesson: str, context: str | None = None) -> None:
        """记忆: 我犯了错,我吸取了教训(JSONL 追加式落盘,崩溃不丢)。"""
        entry = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "mistake": mistake,
            "lesson": lesson,
        }
        if context:
            entry["context"] = context
        with self._lock:
            self.memory["experience_log"].append(entry)
            with open(self._experiences_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self.memory["last_active"] = datetime.now().isoformat()
            self.save()

    # ── 记忆读取 ─────────────────────────────────────────────────────
    def has_element(self, layer: str, name: str) -> bool:
        return f"{layer}/{name}" in self.memory["element_ledger"]

    def get_element(self, layer: str, name: str) -> dict | None:
        return self.memory["element_ledger"].get(f"{layer}/{name}")

    def search_elements(self, query: str) -> list[dict]:
        """按名称/描述模糊检索账本(明天问"我们有算均线的组件吗?"直接命中)。"""
        q = query.lower()
        hits = []
        for key, entry in self.memory["element_ledger"].items():
            desc = entry.get("description", "")
            if q in key.lower() or q in desc.lower():
                hits.append({"path": key, **entry})
        return hits

    def get_layer_summary(self, layer: str) -> list[str]:
        prefix = f"{layer}/"
        return sorted(k for k in self.memory["element_ledger"] if k.startswith(prefix))

    def recent_experiences(self, n: int = _DEFAULT_MAX_EXPERIENCES) -> list[dict]:
        return self.memory["experience_log"][-n:]

    def clear(self) -> None:
        """清空记忆库(测试/重置用)。"""
        with self._lock:
            self.memory = {
                "element_ledger": {},
                "experience_log": [],
                "last_active": None,
            }
            self.save()

    # ── 潜意识注入 ───────────────────────────────────────────────────
    def build_context_prompt(
        self, max_experiences: int = _DEFAULT_MAX_EXPERIENCES
    ) -> str:
        """每次 Agent 醒来时,将记忆压缩成 Prompt 注入它的潜意识。

        账本全量注入(摘要级,体积可控);经验只取最近 max_experiences 条,
        防止 Token 爆炸。
        """
        with self._lock:
            ledger = dict(self.memory["element_ledger"])
            experiences = self.memory["experience_log"][-max_experiences:]

        ledger_str = json.dumps(ledger, ensure_ascii=False, indent=2)
        exp_str = "\n".join(
            f"- [踩坑]: {e.get('mistake', '?')} -> [教训]: {e.get('lesson', '?')}"
            for e in experiences
        )

        return (
            "\n[YOUR INTERNAL MEMORY (DO NOT HALLUCINATE)]\n"
            "You currently manage the following 3O elements in your ledger:\n"
            f"{ledger_str}\n"
            "Past lessons you MUST remember:\n"
            f"{exp_str if exp_str else 'No past mistakes recorded yet.'}\n"
        )
