"""Graft 上下文装配 — Veya 统一流水线 Phase 1 (空间维度: 代码依赖地图)。

装配层(veya): 用 oskill 的确定性代码图 (Graft/CRG tree-sitter 机制, 零 LLM) 把
"重构 X 模块"里的实体解析成"入口定义 + 上游调用方(爆炸半径) + 下游被调方(依赖)",
渲染成注入主脑 System Prompt 的 markdown 上下文块。

  - oskill.parse_code            源码 → CodeTree (符号 + 调用边), 标准库 ast, 确定性
  - CodeTree.callers_of/callees_of  上游爆炸半径 / 下游依赖

内容哈希缓存 → 未变文件跳过重解析 (spec 的 "$0 毫秒级增量"); 缓存落盘
(~/.veya/graft_cache.json), 进程重启后同内容文件仍命中, 省去冷启动重复解析
(nanonets/graft 磁盘持久化缓存的内化)。
时间维度的历史避坑规则由 server.reasoning_bank 提供, 二者在流水线里合成超级 prompt。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from veya.platform import load as _load_3o

_oskill = _load_3o("oskill")
parse_code = _oskill.parse_code

_DEFAULT_CACHE_PATH = Path.home() / ".veya" / "graft_cache.json"


def _content_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _tree_to_dict(tree: Any) -> dict[str, Any]:
    return tree.to_dict()


def _tree_from_dict(data: dict[str, Any]) -> Any:
    return _oskill.CodeTree(
        module=data.get("module", ""),
        symbols=[_oskill.CodeSymbol(**s) for s in data.get("symbols", [])],
        calls=[_oskill.CallEdge(**c) for c in data.get("calls", [])],
        imports=list(data.get("imports", [])),
    )


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
    """代码依赖图 + 内容哈希增量缓存(内存 + 落盘)。sync → find → assemble。"""

    def __init__(self, cache_path: str | Path | None = None) -> None:
        self._cache: dict[str, tuple[str, Any]] = {}  # path → (hash, CodeTree)
        self._runtime_edges: list[tuple[str, str]] = []  # trace_runtime_calls 并入的真实调用边
        self._cache_path = Path(cache_path) if cache_path else _DEFAULT_CACHE_PATH
        self._load_disk_cache()

    def _load_disk_cache(self) -> None:
        """冷启动读盘: 与 ReasoningBank/BoardStore 同惯例, 缺失/损坏都静默忽略。"""
        try:
            raw = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for path, entry in raw.items():
            try:
                self._cache[path] = (entry["hash"], _tree_from_dict(entry["tree"]))
            except (KeyError, TypeError):
                continue

    def _save_disk_cache(self) -> None:
        """落盘当前缓存; 失败不影响内存态 (磁盘只是加速, 不是唯一来源)。"""
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                path: {"hash": h, "tree": _tree_to_dict(tree)}
                for path, (h, tree) in self._cache.items()
            }
            self._cache_path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            pass

    def sync(self, files: dict[str, str]) -> SyncStats:
        """增量同步: 只重解析内容变更的文件 (含未 commit 的改动、含跨进程重启的磁盘缓存命中)。"""
        stats = SyncStats()
        seen = set()
        changed = False
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
            changed = True
        for path in list(self._cache):  # 删除已消失的文件
            if path not in seen:
                del self._cache[path]
                changed = True
        if changed:
            self._save_disk_cache()
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
        out.extend(caller for caller, callee in self._runtime_edges if callee == name)
        return sorted(set(out))

    def _all_callees(self, name: str) -> list[str]:
        out: list[str] = []
        for tree in self._trees:
            out.extend(tree.callees_of(name))
        out.extend(callee for caller, callee in self._runtime_edges if caller == name)
        return sorted(set(out))

    def merge_runtime_edges(self, edges: set[tuple[str, str]] | list[tuple[str, str]]) -> None:
        """并入 trace_runtime_calls 捕获的真实调用边, 补齐 importlib/getattr 反射派发的静态盲区
        (code-graph-rag Runtime Call Tracing 的内化)。不落盘、不新增符号, 只补边。"""
        self._runtime_edges.extend(edges)

    def dead_code(self, entry_points: list[str]) -> list[str]:
        """从入口符号 BFS 遍历调用图 (含 runtime 补边), 返回未被触达的函数/方法名
        (code-graph-rag 死代码检测的内化)。诊断用, 不删除, 调用方自行核实。"""
        all_names = {
            s.name
            for tree in self._trees
            for s in tree.symbols
            if s.kind in (_oskill.SYMBOL_FUNCTION, _oskill.SYMBOL_METHOD)
        }
        seen: set[str] = {e for e in entry_points if e in all_names}
        frontier = list(seen)
        while frontier:
            nxt: list[str] = []
            for name in frontier:
                for callee in self._all_callees(name):
                    if callee in all_names and callee not in seen:
                        seen.add(callee)
                        nxt.append(callee)
            frontier = nxt
        return sorted(all_names - seen)

    def _def_index(self) -> dict[str, set[str]]:
        """name → {定义它的模块集}, 供边置信标注 (重名 = 跨文件解析歧义)。"""
        symbols_by_module: dict[str, list[str]] = {}
        for tree in self._trees:
            for sym in tree.symbols:
                mod = sym.module or tree.module
                symbols_by_module.setdefault(mod, []).append(sym.name)
        return _oskill.build_definition_index(symbols_by_module)

    @staticmethod
    def _render_edges(names: list[str], def_index: dict[str, set[str]]) -> str:
        """渲染一组边, 低置信 (重名/未解析) 的加 ⚠inferred 标注 —— 不当铁事实喂主脑。"""
        parts = []
        for name, conf in _oskill.annotate_edges(names, def_index):
            parts.append(name if conf == _oskill.EXTRACTED else f"{name} ⚠inferred")
        return ", ".join(parts)

    def assemble(self, entities: list[str]) -> str:
        """渲染注入主脑的 System Context Block (markdown)。

        调用边带置信标注: 名字唯一定义 = 可信; 重名/未解析 = ⚠inferred (graphify
        EXTRACTED/INFERRED 范式), 让主脑对低置信依赖保持警惕, 而非照单全收。
        """
        def_index = self._def_index()
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
                    lines.append(
                        f"- **callers (blast radius)**: {self._render_edges(h.callers, def_index)}"
                    )
                if h.callees:
                    lines.append(
                        f"- **calls (dependencies)**: {self._render_edges(h.callees, def_index)}"
                    )
        if not found:
            lines.append("\n(no matching symbols in the indexed workspace)")
        else:
            lines.append(
                "\n_(⚠inferred = name resolved across files but ambiguous; verify before trusting.)_"
            )
        return "\n".join(lines)


def trace_runtime_calls(func: Any, *args: Any, **kwargs: Any) -> tuple[Any, set[tuple[str, str]]]:
    """跑一次 func(*args, **kwargs), 用 sys.setprofile 记录真实 (caller, callee) 调用边。

    静态 ast 解析看不到 importlib/getattr 反射派发的边 (如 skill_hub 动态加载技能模块);
    跑一遍真实调用能补上这些边。返回 (func 的返回值, 捕获的边集合), 边集合可喂给
    GraftContext.merge_runtime_edges。诊断/一次性用途, 不常驻 (setprofile 有性能开销)。
    """
    import sys

    edges: set[tuple[str, str]] = set()
    stack: list[str] = []

    def _profiler(frame: Any, event: str, arg: Any) -> None:
        if event == "call":
            name = frame.f_code.co_name
            if stack:
                edges.add((stack[-1], name))
            stack.append(name)
        elif event == "return" and stack:
            stack.pop()

    prev = sys.getprofile()
    sys.setprofile(_profiler)
    try:
        result = func(*args, **kwargs)
    finally:
        sys.setprofile(prev)
    return result, edges


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
