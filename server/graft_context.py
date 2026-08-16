"""Graft 上下文装配 — Veya 统一流水线 Phase 1 (空间维度: 代码依赖地图)。

装配层(veya): 用 oskill 的确定性代码图 (Graft/CRG tree-sitter 机制, 零 LLM) 把
"重构 X 模块"里的实体解析成"入口定义 + 上游调用方(爆炸半径) + 下游被调方(依赖)",
渲染成注入主脑 System Prompt 的 markdown 上下文块。

  - oskill.parse_code            源码 → CodeTree (符号 + 调用边), 标准库 ast, 确定性
  - CodeTree.callers_of/callees_of  上游爆炸半径 / 下游依赖

内容哈希缓存 → 未变文件跳过重解析 (spec 的 "$0 毫秒级增量")。
时间维度的历史避坑规则由 server.reasoning_bank 提供, 二者在流水线里合成超级 prompt。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from veya.platform import load as _load_3o

_oskill = _load_3o("oskill")
parse_code = _oskill.parse_code


def _content_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


@dataclass
class SyncStats:
    rebuilt: list[str] = field(default_factory=list)
    cached: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return f"rebuilt={len(self.rebuilt)} cached={len(self.cached)}"


@dataclass
class Hit:
    module: str
    kind: str
    line: int
    callers: list[str]  # 上游: 谁调用了它 (爆炸半径)
    callees: list[str]  # 下游: 它调用了谁 (依赖)


class GraftContext:
    """代码依赖图 + 内容哈希增量缓存。sync → find → assemble。"""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[str, Any]] = {}  # path → (hash, CodeTree)

    def sync(self, files: dict[str, str]) -> SyncStats:
        """增量同步: 只重解析内容变更的文件 (含未 commit 的改动)。"""
        stats = SyncStats()
        seen = set()
        for path, content in files.items():
            seen.add(path)
            h = _content_hash(content)
            cached = self._cache.get(path)
            if cached and cached[0] == h:
                stats.cached.append(path)
                continue
            tree = parse_code(content, module=path)
            self._cache[path] = (h, tree)
            stats.rebuilt.append(path)
        for path in list(self._cache):  # 删除已消失的文件
            if path not in seen:
                del self._cache[path]
        return stats

    @property
    def _trees(self) -> list[Any]:
        return [tree for _, tree in self._cache.values()]

    def find(self, entity: str) -> list[Hit]:
        """按符号名定位实体, 聚合跨模块的上下游调用关系。"""
        target = entity.strip()
        hits: list[Hit] = []
        for tree in self._trees:
            for sym in tree.symbols:
                if sym.name == target or sym.name.lower() == target.lower():
                    hits.append(
                        Hit(
                            module=sym.module or tree.module,
                            kind=sym.kind,
                            line=sym.line,
                            callers=self._all_callers(target),
                            callees=self._all_callees(target),
                        )
                    )
        return hits

    def _all_callers(self, name: str) -> list[str]:
        out: list[str] = []
        for tree in self._trees:
            out.extend(tree.callers_of(name))
        return sorted(set(out))

    def _all_callees(self, name: str) -> list[str]:
        out: list[str] = []
        for tree in self._trees:
            out.extend(tree.callees_of(name))
        return sorted(set(out))

    def assemble(self, entities: list[str]) -> str:
        """渲染注入主脑的 System Context Block (markdown)。"""
        lines = ["## CODE CONTEXT (Graft dependency map)"]
        found = False
        for entity in entities:
            hits = self.find(entity)
            if not hits:
                continue
            found = True
            lines.append(f"\n### `{entity}`")
            for h in hits:
                lines.append(f"- **defined**: {h.module}:{h.line} ({h.kind})")
                if h.callers:
                    lines.append(f"- **callers (blast radius)**: {', '.join(h.callers)}")
                if h.callees:
                    lines.append(f"- **calls (dependencies)**: {', '.join(h.callees)}")
        if not found:
            lines.append("\n(no matching symbols in the indexed workspace)")
        return "\n".join(lines)


def extract_entities(task: str) -> list[str]:
    """从任务描述里粗抽候选实体名 (标识符/驼峰/带下划线的词)。启发式, 供 sync 后 find。"""
    import re

    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", task)
    _STOP = {
        "the",
        "and",
        "for",
        "with",
        "class",
        "function",
        "module",
        "refactor",
        "add",
        "fix",
        "remove",
        "update",
        "code",
        "test",
        "into",
        "from",
        "this",
    }
    seen: list[str] = []
    for w in words:
        if w.lower() not in _STOP and w not in seen:
            seen.append(w)
    return seen
