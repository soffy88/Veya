"""goal_run verify — 验收检查。

实现要点：
- v0.1：规则 + 一次 LLM 判定（输入：acceptance、leaf summary、可选文件存在性检查）
- pass → task completed
- fail 且 retries < max → retries++，回到 ready（可附加「上轮失败原因」进 instruction）
- fail 且用尽 → task blocked；策略 on_task_blocked：
  - stop_goal（默认）：goal blocked
  - continue：跳过依赖它的下游，标 cancelled/blocked 传播

acceptance 提到测试通过/失败数时，优先从 leaf_result 里机械抽取 "N passed, M failed" 数字判定，
不经 LLM 主观判断（autoresearch "mechanical verification only" 范式的内化）；抽不到数字信号时
退化到原有 LLM 判定，不是每条 acceptance 都能机械核实。
"""

import re
from dataclasses import dataclass

from server.goal_run.models import GoalRunResponse, GoalRunState, GoalStatus, TaskNode, TaskStatus

_TEST_HINT_RE = re.compile(r"test|测试", re.IGNORECASE)
_PASS_HINT_RE = re.compile(r"pass|通过", re.IGNORECASE)
_TEST_SUMMARY_RE = re.compile(r"(\d+)\s+passed(?:,\s*(\d+)\s+failed)?", re.IGNORECASE)


@dataclass
class VerifyResult:
    """验收结果。"""

    passed: bool
    summary: str
    reason: str | None = None


async def verify_task(
    task: TaskNode,
    leaf_result: str,
    project_root: str,
) -> VerifyResult:
    """对照 acceptance 验证叶子执行结果。

    实现要点：
    - 先做规则检查（文件存在性、可观察条件）
    - 再调用 LLM 判定（输入 acceptance + leaf summary）
    - 返回 VerifyResult
    """
    # 规则检查
    rule_passed, rule_reason = _rule_check(task, leaf_result, project_root)

    if not rule_passed:
        return VerifyResult(passed=False, summary=rule_reason or "", reason=rule_reason)

    # 机械核实：acceptance 提到测试通过/失败数且 leaf_result 里有可辨识的数字信号时，
    # 直接按数字裁决，不问 LLM 主观意见
    mechanical = _mechanical_verify(task, leaf_result)
    if mechanical is not None:
        passed, summary = mechanical
        return VerifyResult(passed=passed, summary=summary, reason=None if passed else summary)

    # LLM 判定
    llm_passed, llm_summary = await _llm_check(task, leaf_result)

    if llm_passed:
        return VerifyResult(passed=True, summary=llm_summary)
    else:
        return VerifyResult(passed=False, summary=llm_summary, reason=llm_summary)


def _rule_check(task: TaskNode, leaf_result: str, project_root: str) -> tuple[bool, str | None]:
    """规则检查：文件存在性、可观察条件。

    实现要点：
    - 若 acceptance 中提到文件路径，检查文件是否存在
    - 若提到 "exit code"、"exit 0" 等，检查 leaf_result 中是否包含
    - 返回 (passed, reason)
    """
    if not task.acceptance:
        return True, "no acceptance conditions to check"

    for condition in task.acceptance:
        # 简单规则：若条件包含文件路径，检查文件存在
        if "exists" in condition.lower() or "文件" in condition:
            # 提取路径（简化处理）
            import re

            paths = re.findall(
                r"[\w/.-]+\.(?:py|js|ts|md|json|txt|html|css|sh|yaml|yml)", condition
            )
            for p in paths:
                from pathlib import Path

                full_path = Path(project_root) / p
                if not full_path.exists():
                    return False, f"文件不存在: {p}"
        # 检查 leaf_result 中是否包含关键输出
        if (
            "exit" in condition.lower()
            and "exit 0" in condition.lower()
            and "exit 0" not in leaf_result.lower()
        ):
            return False, "exit code 不为 0"

    return True, None


def _mechanical_verify(task: TaskNode, leaf_result: str) -> tuple[bool, str] | None:
    """acceptance 提到测试通过/失败时，从 leaf_result 里机械抽取 pytest 风格 "N passed[, M
    failed]" 数字裁决。找不到可辨识的数字信号返回 None，退化到 LLM 判定。"""
    if not task.acceptance:
        return None
    if not any(_TEST_HINT_RE.search(c) and _PASS_HINT_RE.search(c) for c in task.acceptance):
        return None
    m = _TEST_SUMMARY_RE.search(leaf_result)
    if not m:
        return None
    passed_count = int(m.group(1))
    failed_count = int(m.group(2)) if m.group(2) else 0
    if failed_count == 0:
        return True, f"机械核实: {passed_count} passed, 0 failed"
    return False, f"机械核实: {passed_count} passed, {failed_count} failed"


async def _llm_check(task: TaskNode, leaf_result: str) -> tuple[bool, str]:
    """LLM 判定：输入 acceptance + leaf summary，返回是否通过。

    实现要点：
    - 调用现有 LLM 层 (veya1.1)
    - 返回 (passed, summary)
    """
    from server.project_understand import _default_llm

    system_prompt = (
        "你是验收判定器。只输出一个 JSON 对象，不输出任何其它文字。\n"
        "根据用户给定的 acceptance 条件与 leaf 执行结果，判定是否通过。\n"
        "JSON 字段: passed(bool), summary(string, 一句话总结)。"
    )

    user_prompt = (
        "## Acceptance 条件\n"
        + "\n".join(f"- {a}" for a in task.acceptance)
        + f"\n\n## Leaf 执行结果\n{leaf_result[:2000]}"
    )

    try:
        raw = await _default_llm(system_prompt, user_prompt)
        import json

        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            return False, "LLM 输出解析失败"
        data = json.loads(raw[start : end + 1])
        passed = bool(data.get("passed", False))
        summary = str(data.get("summary", ""))
        return passed, summary
    except Exception as exc:
        return False, f"LLM 判定异常: {exc}"


async def apply_block_policy(
    state: GoalRunState,
    task: TaskNode,
    policy: str = "stop_goal",
) -> GoalRunResponse:
    """应用任务阻塞策略。

    实现要点：
    - stop_goal（默认）：goal blocked，返回 block 响应
    - continue：跳过依赖它的下游，标 cancelled/blocked 传播
    """
    task.status = TaskStatus.blocked
    task.block_reason = task.block_reason or "verify failed after retries exhausted"

    if policy == "stop_goal":
        state.status = GoalStatus.blocked
        return GoalRunResponse(
            goal_id=state.goal_id,
            status=GoalStatus.blocked,
            phase="running",
            interpretation=None,
            questions=None,
            goal_counts=state.snapshot_running(),
            summary=None,
            block_reason=f"task {task.id} blocked: {task.block_reason}",
            artifacts=None,
            next_action="none",
        )

    # continue 策略：将依赖此任务的下游全部标记为 cancelled
    for tn in state.tasks.values():
        if task.id in tn.depends_on:
            tn.status = TaskStatus.cancelled
            tn.block_reason = f"upstream task {task.id} blocked"

    # 检查是否所有任务都已完成或取消
    all_terminal = all(
        tn.status in (TaskStatus.completed, TaskStatus.blocked, TaskStatus.cancelled)
        for tn in state.tasks.values()
    )
    if all_terminal:
        state.status = GoalStatus.completed
        return GoalRunResponse(
            goal_id=state.goal_id,
            status=GoalStatus.completed,
            phase="finalized",
            interpretation=None,
            questions=None,
            goal_counts=state.snapshot_running(),
            summary="All tasks completed or blocked",
            block_reason=None,
            artifacts=None,
            next_action="none",
        )

    return GoalRunResponse(
        goal_id=state.goal_id,
        status=GoalStatus.running,
        phase="running",
        interpretation=None,
        questions=None,
        goal_counts=state.snapshot_running(),
        summary=None,
        block_reason=None,
        artifacts=None,
        next_action="continue",
    )
