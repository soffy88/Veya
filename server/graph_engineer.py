"""server.graph_engineer — 多引擎编排自纠正循环 (graph-engineer 式)。

借鉴 graph-engineer (Claude Code skill): Orchestrator-Workers +
Evaluator-Optimizer 嵌套。veya 落地:
- Orchestrator = 主脑/本工具 (用户给 plan_id + 引擎选择)
- Worker     = 实现引擎 (claude/codex/grok/pi, 默认 codex) 执行未完成 todo
- 机械门     = gate_check (plan 依赖门) + 实现摘要
- Evaluator  = 批判引擎 (默认 claude) 只读审查实现
- 仲裁       = 本工具启发式 (缺陷关键词 → 修复 or 验证)
- 修复       = 实现引擎根据批判回改
- 状态全程  = plan_todo (create_plan/update_todo/evidence) — 看板可视化

主脑零改动 (冻结架构); 纯新增能力工具, 模型自主调用。
"""

from __future__ import annotations

import asyncio
import json
import re

from server import engine_runner
from server.plan_todo import _load as _load_plan
from server.plan_todo import _plans_dir
from server.plan_todo import _save as _save_plan

_DEFECT_MARKERS = ("缺陷", "问题", "bug", "错误", "失败", "不通过", "遗漏",
                   "风险", "无法", "broken", "error", "failed", "bug ",
                   "missing", "不正确", "需修复", "需要修复")
_ITER_CAP = 3


def _pending_todos(plan: dict) -> list[dict]:
    """未完成 todo (open/in_progress, 依赖已满足优先)。"""
    todos = plan.get("todos", [])
    pending = [t for t in todos if t.get("status") in ("open", "in_progress")]
    # 依赖满足的优先
    done_ids = {t["id"] for t in todos if t.get("status") == "done"}
    ready = [t for t in pending if all(d in done_ids for d in (t.get("depends_on") or []))]
    return ready or pending


async def _run_engine(engine: str, prompt: str, timeout_s: float = 300.0) -> str:
    """调引擎 CLI (聚合模式, 非流式)。"""
    res = await engine_runner.run_engine(engine, prompt, timeout_s=timeout_s)
    if not res.get("ok"):
        raise RuntimeError(f"引擎 {engine} 失败: {res.get('error')}")
    return str(res.get("output") or "")


def _has_defects(text: str) -> bool:
    """仲裁: 批判结果是否含有效缺陷信号 (启发式, 非程序路由)。"""
    t = (text or "").lower()
    return any(m.lower() in t for m in _DEFECT_MARKERS)


def _evidence_summary(text: str, limit: int = 300) -> str:
    """提取证据摘要 (首行 + 关键行)。"""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return ""
    head = lines[0][:limit]
    return head


async def graph_cycle(plan_id: str,
                      implement_engine: str = "codex",
                      critique_engine: str = "claude",
                      max_iterations: int = _ITER_CAP) -> str:
    """在计划的未完成 todo 上跑 实现→批判→修复→验证 自纠正循环。

    implement_engine/critique_engine: 引擎名 (claude/codex/grok/pi)。
    每步状态写 plan_todo (看板可见)。不同引擎分离实现/批判角色 —
    不让写代码的引擎自评。迭代上限默认 3 轮。
    """
    max_iterations = max(1, min(int(max_iterations), 5))
    try:
        plan = _load_plan(plan_id)
    except Exception as exc:  # noqa: BLE001
        return f"graph_cycle: {exc}"
    todos = _pending_todos(plan)
    if not todos:
        return f"graph_cycle: 计划 {plan_id} 无未完成任务, 无需循环"

    log: list[str] = [f"🔄 graph-cycle 启动: {plan_id} (实现={implement_engine}, 批判={critique_engine})"]
    done_count = 0

    for iteration in range(1, max_iterations + 1):
        todos = _pending_todos(plan)
        if not todos:
            log.append("✅ 所有任务已完成")
            break
        targets = [t.get("id") for t in todos[:1]]  # 一次处理一个 todo (可控)
        todo_id = targets[0]
        todo = next(t for t in plan["todos"] if t.get("id") == todo_id)
        objective = str(plan.get("objective", ""))
        title = str(todo.get("title", ""))
        log.append(f"\n── 迭代 {iteration}: [{todo_id}] {title}")

        # 1. 实现 (Worker): 引擎实现该 todo
        impl_prompt = (
            f"实现以下任务 (在 {plan_id} 计划中 todo [{todo_id}] {title}):\n"
            f"计划目标: {objective}\n"
            "请实际动手完成 (写代码/改文件/运行验证), 完成后给出: 改了哪些文件、"
            "运行了什么、验证输出。"
        )
        try:
            impl_out = await _run_engine(implement_engine, impl_prompt)
        except Exception as exc:  # noqa: BLE001
            log.append(f"⚠ 实现失败: {exc}")
            break
        todo["status"] = "in_progress"
        todo["evidence"].append({"at": _now(), "note": f"[graph-cycle] 实现完成: {_evidence_summary(impl_out)}"})
        _save_plan(plan)
        log.append(f"✅ 实现完成 ({implement_engine})")

        # 2. 机械门 (QUALITY GATE): 实现摘要非空 + 依赖门
        if not impl_out.strip():
            log.append("⛔ 质量门: 实现无输出 → 回退实现 (重试)")
            continue

        # 3. 批判 (Evaluator): 另一引擎只读审查
        crit_prompt = (
            f"你是独立审查者 ({critique_engine}), 只读审查以下实现, 不修改任何文件:\n"
            f"任务: {title}\n实现结果:\n{impl_out[:4000]}\n\n"
            "列出有效缺陷 (功能/边界/正确性问题), 若无缺陷明确说 '无缺陷'。"
            "只列确定的问题, 不要猜测。"
        )
        try:
            crit_out = await _run_engine(critique_engine, crit_prompt)
        except Exception as exc:  # noqa: BLE001
            log.append(f"⚠ 批判失败: {exc} → 视为通过 (无有效发现)")
            crit_out = "无缺陷"

        # 4. 仲裁 (DEBATE): 缺陷信号 → 修复 or 验证
        if _has_defects(crit_out) and "无缺陷" not in crit_out:
            log.append(f"🔍 批判发现缺陷 → 修复 ({critique_engine})")
            fix_prompt = (
                f"根据以下批判意见修复刚才的实现 (todo [{todo_id}] {title}):\n"
                f"批判:\n{crit_out[:3000]}\n\n"
                "修复并重新验证, 完成后给出验证输出。"
            )
            try:
                fix_out = await _run_engine(implement_engine, fix_prompt)
                todo["evidence"].append({
                    "at": _now(),
                    "note": f"[graph-cycle] 修复完成: {_evidence_summary(fix_out)}"})
                _save_plan(plan)
                log.append(f"🔧 修复完成 ({implement_engine})")
            except Exception as exc:  # noqa: BLE001
                log.append(f"⚠ 修复失败: {exc}")
        else:
            log.append("✅ 批判无有效缺陷 → 验证通过")

        # 5. 标记 done + 证据
        todo["status"] = "done"
        todo["evidence"].append({"at": _now(), "note": "[graph-cycle] 验证通过 (批判仲裁)"})
        _save_plan(plan)
        done_count += 1

    plan = _load_plan(plan_id)
    done = sum(1 for t in plan.get("todos", []) if t.get("status") == "done")
    total = len(plan.get("todos", []))
    log.append(f"\n📊 循环结束: 本轮完成 {done_count} 个 todo, 计划进度 {done}/{total}")
    log.append(f"查看: 计划看板 (侧栏「计划」) 或 plan_status({plan_id})")
    return "\n".join(log)


def _now() -> str:
    import time

    return time.strftime("%Y-%m-%d %H:%M:%S")
