"""officecli-financial-model 场景层技能包 — 财务模型 xlsx (financial model)。

场景层继承基座 officecli (路径白名单/审计/L1-L3 分层/help 动态 schema),
附加场景规则见 scene_rules.md (与 OfficeCLI 场景层同模式: 基座规则继承,
本文件只加场景专属)。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# 继承基座 main (同仓库 templates/skills/officecli/run.py)
_BASE = Path(__file__).resolve().parents[1] / "officecli"
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

from run import main as _base_main  # noqa: E402


def scene_rules() -> str:
    """场景规则文本 (供 LLM 注入)。"""
    path = Path(__file__).parent / "scene_rules.md"
    return path.read_text(encoding="utf-8")


def main(**kwargs: Any) -> dict[str, Any]:
    """执行场景操作: 基座语义 + 场景层标记。"""
    result = _base_main(**kwargs)
    result["scene"] = "officecli-financial-model"
    result["scene_rules"] = scene_rules()[:2000]
    return result
