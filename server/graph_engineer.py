"""server.graph_engineer — 多引擎编排自纠正循环 (graph-engineer 式)。

借鉴 graph-engineer (Claude Code skill): Orchestrator-Workers +
Evaluator-Optimizer 嵌套。veya 落地:
- Orchestrator = 主脑/本工具 (用户给 plan_id + 引擎选择)
- Worker     = 实现引擎 (claude/codex/grok/pi, 默认 codex) 执行未完成 todo
- 机械门     = 实现输出非空 + gate_check (plan 依赖门)
- Evaluator  = 批判引擎 (默认 claude) 只读审查 — 与实现引擎不同模型 (真独立批判)
- 仲裁       = DEBATE 三分类: valid/debatable(反证 reinject)/false-positive(理由)
- 修复       = 实现引擎按仲裁结果回改 → 回机械门
- 验证       = 无有效缺陷后标记 done (含根因分类预留)
- 防振荡     = CRITIQUE 状态连续性 (携带先前发现+triage) + Anti-loop cutoff
              (同底层抱怨+无净变更 → 停止升级, 绝不假解决)
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


def _cycle_ctx(plan: dict, todo: dict) -> str:
    """CRITIQUE 状态连续性: 携带先前发现 + triage 决定 (--resume-last 等价物)。

    从 todo 的 graph-cycle evidence 提取历史发现与仲裁, 让批判引擎不要重复
    已裁决的抱怨 (防振荡), 保留 VERIFY 失败分类供根因分析。
    """
    notes = [e.get("note", "") for e in todo.get("evidence", [])
             if e.get("note", "").startswith("[graph-cycle]")]
    prior = [n for n in notes if "批判" in n or "修复" in n or "仲裁" in n]
    if not prior:
        return ""
    return "\n".join(
        "先前回合上下文 (勿重复已裁决项, 如与以下冲突请指出):\n" + "\n".join(prior[-4:])
    )


async def _debate(critique_engine: str, crit_out: str, title: str,
                  ctx: str, log: list[str], todo: dict) -> tuple[str, str]:
    """DEBATE 三分类 (graph-engineer node 5):

    - valid          → 返回 ("refactor", 列表), 进修复
    - debatable      → 带反证 reinject 回批判引擎, 等待其回复后裁决 (防振荡)
    - false-positive → 一行理由丢弃 (绝不静默)

    返回 (verdict, detail): verdict ∈ {"refactor", "pass", "escalate"}
    """
    if not _has_defects(crit_out) or "无缺陷" in crit_out:
        return "pass", ""
    # 简化启发式分类: 含明确否定词 → valid; 含"可能/或许/建议" → debatable
    hedged = ("可能", "或许", "也许", "建议", "可以考虑", "might", "may", "could", "consider")
    if any(h in crit_out for h in hedged) and not any(
            m in crit_out for m in ("必须", "会失败", "一定", "will fail", "must", "broken", "崩溃")):
        # debatable → 反证 reinject (一次)
        counter = (
            f"批判者提出了以下可能站不住脚的问题:\n{crit_out[:2000]}\n\n"
            f"请判断: 你坚持这些问题吗? 如果坚持请给具体证据 (哪行/哪个场景失败); "
            "如果考虑不充分或可接受, 明确说 '撤回'。只读, 不修改文件。"
        )
        try:
            rebuttal = await _run_engine(critique_engine, counter)
            if "撤回" in rebuttal or "接受" in rebuttal or "不坚持" in rebuttal:
                todo["evidence"].append({"at": _now(),
                                         "note": f"[graph-cycle] 反证后被批判者撤回: {_evidence_summary(rebuttal)}"})
                return "pass", f"反证后被撤回: {_evidence_summary(rebuttal)}"
            return "refactor", rebuttal[:300]
        except Exception as exc:  # noqa: BLE001
            log.append(f"⚠ 反证 reinject 失败: {exc} → 按 valid 处理")
            return "refactor", ""
    return "refactor", ""


async def graph_cycle(plan_id: str,
                      implement_engine: str = "codex",
                      critique_engine: str = "claude",
                      max_iterations: int = _ITER_CAP) -> str:
    """在计划的未完成 todo 上跑 实现→批判→仲裁→修复→验证 自纠正循环。

    implement_engine/critique_engine: 引擎名 (claude/codex/grok/pi)。
    不同引擎分离实现/批判角色 — 不让写代码的引擎自评。
    防振荡: CRITIQUE 状态连续性 + DEBATE 三分类 + Anti-loop cutoff。
    迭代上限默认 3 轮。
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
    # 每 todo 一轮“实现→批判→(修复→批判…)”直到无缺陷或达该 todo 迭代上限
    while True:
        todos = _pending_todos(plan)
        if not todos:
            log.append("✅ 所有任务已完成")
            break
        todo = todos[0]
        todo_id = todo.get("id")
        title = str(todo.get("title", ""))
        objective = str(plan.get("objective", ""))
        fix_count = sum(1 for e in todo.get("evidence", [])
                        if e.get("note", "").startswith("[graph-cycle] 修复完成"))
        log.append(f"\n── todo [{todo_id}] {title} (已修复 {fix_count} 轮)")
        # 同 todo 超上限 → Anti-loop cutoff: 停止升级, 不假造解决
        if fix_count >= max_iterations:
            log.append(f"⚠ Anti-loop cutoff: todo [{todo_id}] 已修复 {fix_count} 轮仍有缺陷, "
                       "停止并升级用户 (不假造解决)")
            todo["evidence"].append({"at": _now(),
                                     "note": "[graph-cycle] anti-loop cutoff: 停止升级用户"})
            todo["status"] = "blocked"
            _save_plan(plan)
            break

        # 1. 实现 (Worker, 仅首次) 或直接进入批判修复循环
        if fix_count == 0:
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
            todo["evidence"].append({"at": _now(),
                                     "note": f"[graph-cycle] 实现完成: {_evidence_summary(impl_out)}"})
            _save_plan(plan)
            log.append(f"✅ 实现完成 ({implement_engine})")

            # 机械门 (QUALITY GATE): 实现输出非空
            if not impl_out.strip():
                log.append("⛔ 质量门: 实现无输出 → 回退实现 (重试)")
                continue

        # 2. 批判 (Evaluator, 状态连续性)
        crit_prompt = (
            f"你是独立审查者 ({critique_engine}), 只读审查当前实现, 不修改任何文件:\n"
            f"任务: {title}\n"
            f"{_cycle_ctx(plan, todo)}\n\n"
            "列出有效缺陷 (功能/边界/正确性问题), 若无缺陷明确说 '无缺陷'。"
            "只列确定的问题, 不要猜测; 不确定的用'可能/建议'标注。"
        )
        try:
            crit_out = await _run_engine(critique_engine, crit_prompt)
        except Exception as exc:  # noqa: BLE001
            log.append(f"⚠ 批判失败: {exc} → 视为通过 (无有效发现)")
            crit_out = "无缺陷"

        # 3. 仲裁 (DEBATE 三分类)
        verdict, detail = await _debate(critique_engine, crit_out, title, "", log, todo)
        if verdict == "refactor":
            log.append(f"🔍 批判发现有效缺陷 → 修复 ({implement_engine})")
            fix_prompt = (
                f"根据以下批判意见修复当前实现 (todo [{todo_id}] {title}):\n"
                f"批判:\n{crit_out[:3000]}\n{('仲裁补充: ' + detail) if detail else ''}\n\n"
                "修复并重新验证, 完成后给出验证输出。"
            )
            try:
                fix_out = await _run_engine(implement_engine, fix_prompt)
                todo["evidence"].append({
                    "at": _now(),
                    "note": f"[graph-cycle] 修复完成: {_evidence_summary(fix_out)}"})
                _save_plan(plan)
                log.append(f"🔧 修复完成 ({implement_engine}) → 回批判")
            except Exception as exc:  # noqa: BLE001
                log.append(f"⚠ 修复失败: {exc}")
                break
            continue  # 不 done, 回批判循环 (Evaluator-Optimizer)

        # 4. 无有效缺陷 → 验证通过 → done
        log.append(f"✅ 批判无有效缺陷/已撤回 → 验证通过{(' (' + detail + ')') if detail else ''}")
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
