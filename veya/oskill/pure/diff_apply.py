"""3O-PURE — diff_apply: 标准 unified diff 生成与应用。

纯函数实现（difflib 标准库）：
- ``make_unified_diff``: 旧/新文本 → unified diff 字符串；
- ``apply_unified_diff``: 源文本 + diff → (ok, 结果文本)（解析 hunk 应用）；
- ``diff_stats``: 增删行统计。

用于 omodul_evidence_refine 与代码生成回写：模型产出 → 静态检查 →
diff 评审 → 应用（全链路纯函数，I/O 只发生在 oprim 层）。
"""

from __future__ import annotations

import difflib
import re


def make_unified_diff(old: str, new: str, *, label: str = "file") -> str:
    """生成 unified diff（3 行上下文）。"""
    if not isinstance(old, str) or not isinstance(new, str):
        raise ValueError("old/new 必须是字符串")
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=label,
        tofile=label,
        n=3,
    )
    return "".join(diff)


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _parse_hunks(diff: str) -> list[tuple[int, int, int, int, list[str]]]:
    """解析 unified diff 为 hunks: [(old_start, old_count, new_start, new_count, lines)]。"""
    hunks: list[tuple[int, int, int, int, list[str]]] = []
    current: list[str] = []
    meta: tuple[int, int, int, int] | None = None
    for line in diff.splitlines(keepends=True):
        if line.startswith("@@"):
            if current:
                assert meta is not None
                hunks.append((*meta, current))
                current = []
            m = _HUNK_RE.match(line)
            if not m:
                continue
            old_start = int(m.group(1))
            old_count = int(m.group(2) or 1)
            new_start = int(m.group(3))
            new_count = int(m.group(4) or 1)
            meta = (old_start, old_count, new_start, new_count)
        elif meta is not None and line.startswith((" ", "+", "-")):
            current.append(line)
    if current and meta is not None:
        hunks.append((*meta, current))
    return hunks


def apply_unified_diff(src: str, diff: str) -> tuple[bool, str]:
    """把 unified diff 应用到源文本。返回 (ok, 结果或错误信息)。

    支持上下文行与增删行（3 上下文 n=3 兼容任何 n>=0）；diff 与源不符时
    返回 (False, 具体错误位置)，不产生部分应用结果。
    """
    hunks = _parse_hunks(diff)
    if not hunks:
        return False, "diff 中无有效 hunk"

    lines = src.splitlines(keepends=True)
    # 逐 hunk 从后往前应用（行号不受前面修改影响）
    for old_start, old_count, _new_start, new_count, hunk_lines in reversed(hunks):
        idx = old_start - 1
        # 校验旧侧匹配（不含 + 行）
        cursor = idx
        ok = True
        for hline in hunk_lines:
            if hline.startswith(("-", " ")):
                if cursor >= len(lines):
                    ok = False
                    break
                want = hline[1:]
                if lines[cursor] != want:
                    ok = False
                    break
                cursor += 1
        if not ok:
            return False, f"hunk @@ -{old_start},{old_count} 与源不匹配"
        # 构建新块（保留 + 行, 跳过 - 行, 空格行保留）
        new_block: list[str] = []
        for hline in hunk_lines:
            if hline.startswith("-"):
                continue
            new_block.append(hline[1:])
        lines = lines[:idx] + new_block + lines[cursor:]
        # 校验新侧行数
        actual_new = sum(1 for h in hunk_lines if h.startswith(("+", " ")))
        # 末行无换行的 diff 边界: 行数可以 ±1
        if new_count != 0 and actual_new != new_count and abs(actual_new - new_count) > 1:
            return (
                False,
                f"hunk @@ -{old_start},{old_count} +{_new_start},{new_count} 新侧行数不一致",
            )
    return True, "".join(lines)


def diff_stats(diff: str) -> dict:
    """diff 统计: {added, removed, hunks}（纯函数, 不计 ---/+++ 头行）。"""
    added = removed = hunks = 0
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("@@"):
            in_hunk = True
            hunks += 1
        elif in_hunk and line.startswith("+"):
            added += 1
        elif in_hunk and line.startswith("-"):
            removed += 1
    return {"added": added, "removed": removed, "hunks": hunks}


__all__ = [
    "apply_unified_diff",
    "diff_stats",
    "make_unified_diff",
]
