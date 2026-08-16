"""ReasoningBank — Veya 统一流水线的记忆面 (时间维度: 历史避坑规则)。

装配层(veya): 用 obase 的向量记忆存 OpenRSI 演化归纳出的经验三元组, 供下次任务
开局检索 (Phase 1c) 并在收尾时反思落盘 (Phase 3)。

  - obase.VectorMemory   经验存储 + BM25/embedding 检索

Phase 1c  search_experience(task) → 历史教训 → Rule Constraint Block 注入 System Prompt
Phase 3   induce(task, trajectory, llm) → 对比死亡分支/成功分支 → 三元组规则 → 落盘
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from veya.platform import load as _load_3o

_obase = _load_3o("obase")
VectorMemory = _obase.VectorMemory

_COLLECTION = "reasoning_bank"

# 生成契约同 openrsi: 同步 llm(prompt) -> str
Llm = Any


@dataclass
class Experience:
    """一条经验三元组: 什么场景(situation) 会踩什么坑(pitfall), 怎么规避(fix)。"""

    situation: str
    pitfall: str
    fix: str
    task: str = ""

    def to_rule(self) -> str:
        return f"When {self.situation}: {self.pitfall} — instead, {self.fix}."


class ReasoningBank:
    def __init__(self, base_dir: str | Path | None = None) -> None:
        self._mem = VectorMemory(base_dir=str(base_dir) if base_dir else None)

    # --- Phase 1c: 检索 --------------------------------------------------
    def search_experience(self, task: str, k: int = 3) -> list[Experience]:
        rows = self._mem.query([task], collection=_COLLECTION, n_results=k)
        out: list[Experience] = []
        for row in rows:
            try:
                data = json.loads(row.get("result", "{}"))
                out.append(
                    Experience(
                        **{f: data.get(f, "") for f in ("situation", "pitfall", "fix", "task")}
                    )
                )
            except (json.JSONDecodeError, TypeError):
                continue
        return out

    def as_rule_block(self, lessons: list[Experience]) -> str:
        """渲染注入主脑的 Rule Constraint Block (markdown)。"""
        if not lessons:
            return ""
        lines = ["## RULE CONSTRAINTS (learned from past tasks)"]
        for e in lessons:
            lines.append(f"- {e.to_rule()}")
        return "\n".join(lines)

    # --- Phase 3: 反思归纳 + 落盘 ---------------------------------------
    def induce(
        self, task: str, trajectory: list[dict[str, Any]], llm: Llm, target: str | None = None
    ) -> Experience | None:
        """对比演化轨迹里的死亡分支与成功分支, 提炼一条可复用规则并落盘。"""
        # 通关(测试全绿) vs 死亡分支, 按 solved 标志切分 (复合分含 diff 惩罚, 不可靠)
        success = [t for t in trajectory if t.get("solved")]
        deaths = [t for t in trajectory if not t.get("solved") and t.get("op") != "root"]
        if not success or not deaths:
            return None  # 没有对照 → 无因果可归纳
        win = max(success, key=lambda t: t["reward"])
        worst = min(deaths, key=lambda t: t["reward"])
        tgt = target or next(iter(win.get("files", {})), "")
        prompt = (
            f"# TASK\n{task}\n\n"
            "Two approaches were tried in a sandbox. Extract ONE reusable lesson as a "
            "JSON object with keys situation, pitfall, fix (each one short sentence).\n"
            f"\n# FAILED (reward={worst['reward']:.2f}, op={worst['op']})\n"
            f"```python\n{worst.get('files', {}).get(tgt, '')}```\n"
            f"feedback: {worst.get('feedback', '')[:600]}\n"
            f"\n# SUCCEEDED (reward={win['reward']:.2f})\n"
            f"```python\n{win.get('files', {}).get(tgt, '')}```\n"
            "\nReturn only the JSON object."
        )
        raw = llm(prompt)
        exp = self._parse(raw, task)
        if exp:
            self.store(exp)
        return exp

    def store(self, exp: Experience) -> int:
        payload = json.dumps(
            {
                "situation": exp.situation,
                "pitfall": exp.pitfall,
                "fix": exp.fix,
                "task": exp.task,
            }
        )
        return self._mem.add_query(
            exp.situation or exp.task, payload, collection=_COLLECTION, metadata={"task": exp.task}
        )

    def count(self) -> int:
        return self._mem.count(collection=_COLLECTION)

    @staticmethod
    def _parse(raw: str, task: str) -> Experience | None:
        text = raw.strip()
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
        if not any(data.get(f) for f in ("situation", "pitfall", "fix")):
            return None
        return Experience(
            situation=str(data.get("situation", "")),
            pitfall=str(data.get("pitfall", "")),
            fix=str(data.get("fix", "")),
            task=task,
        )
