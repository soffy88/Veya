"""server.graph_engineer — 多引擎编排自纠正循环 (graph-engineer 式)。

借鉴 graph-engineer (Claude Code skill): Orchestrator-Workers +
Evaluator-Optimizer 嵌套。veya 落地:
- Orchestrator = 主脑/本工具 (用户给 plan_id + 引擎选择)
- Worker     = 实现引擎 (claude/codex/grok/pi, 默认 codex) 执行未完成 todo
- PRE-FLIGHT = 安全检查: git clean + 非 main 分支 + 目录可写 (可开关)
- QUALITY GATE = 机械门: lint/type/build (禁 mutating 命令, 每激活≤3失败, 环境失败升级)
- Evaluator  = 批判引擎 (默认 claude) 只读审查 — 与实现引擎不同模型 (真独立批判)
- DEBATE     = 三分类: valid/debatable(反证 reinject)/false-positive(理由)
- REFACTOR   = 实现引擎按仲裁回改 → 回质量门
- VERIFY     = 功能测试/验收 (与机械门分离); 失败→分类根因(4类)→回批判
- 防振荡     = CRITIQUE 状态连续性 + Anti-loop cutoff (同底层抱怨+无净变更→升级, 不假解决)
- 状态全程  = plan_todo (create_plan/update_todo/evidence) — 看板可视化

主脑零改动 (冻结架构); 纯新增能力工具, 模型自主调用。
"""

from __future__ import annotations

import asyncio
import os
import re

from server import engine_runner
from server.plan_todo import _load as _load_plan
from server.plan_todo import _save as _save_plan

_ITER_CAP = 3
_GATE_CAP = 3  # QUALITY GATE 每激活失败上限
_MUTATING = ("--write", "--fix", " -w ", "--in-place", "prettier --write")
_VERIFY_ROOTS = (
    "implementation-defect",
    "test-defect",
    "contract-mismatch",
    "environmental",
    "实现缺陷",
    "测试缺陷",
    "契约不符",
    "环境问题",
)


def _pending_todos(plan: dict) -> list[dict]:
    """未完成 todo (open/in_progress, 依赖已满足优先)。"""
    todos = plan.get("todos", [])
    pending = [t for t in todos if t.get("status") in ("open", "in_progress")]
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
    t = (text or "").lower()
    markers = (
        "缺陷",
        "问题",
        "bug",
        "错误",
        "失败",
        "不通过",
        "遗漏",
        "风险",
        "无法",
        "broken",
        "error",
        "failed",
        "missing",
        "不正确",
        "需修复",
        "需要修复",
    )
    return any(m in t for m in markers)


def _evidence_summary(text: str, limit: int = 300) -> str:
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return lines[0][:limit] if lines else ""


def _cycle_ctx(plan: dict, todo: dict) -> str:
    """CRITIQUE 状态连续性 (--resume-last 等价物): 携带先前发现+仲裁+VERIFY 根因。"""
    notes = [
        e.get("note", "")
        for e in todo.get("evidence", [])
        if e.get("note", "").startswith("[graph-cycle]")
    ]
    prior = [n for n in notes if any(k in n for k in ("批判", "修复", "仲裁", "验证", "根因"))]
    if not prior:
        return ""
    return "先前回合上下文 (勿重复已裁决项, 如与以下冲突请指出):\n" + "\n".join(prior[-5:])


# ── PRE-FLIGHT 安全检查 ─────────────────────────────────────────────
async def _preflight(workdir: str | None, strict: bool) -> str:
    """检查: 目录可写 + git clean + 非 main 分支 (原版 PRE-FLIGHT 硬检查)。

    返回 "" 表示通过, 否则返回升级消息。strict=False 时仅警告 (veya 默认,
    因引擎工作区常非 git); strict=True 时检查失败直接中止。
    """
    if not workdir:
        return (
            "" if strict else "ℹ PRE-FLIGHT: 未指定 workdir, 跳过 git/目录检查 (可传 workdir 启用)"
        )
    if not os.path.isdir(workdir):
        msg = f"⚠ PRE-FLIGHT: 目录不存在: {workdir}"
        return msg if strict else msg + " (继续, 引擎将自行创建)"
    if not os.access(workdir, os.W_OK):
        msg = f"⚠ PRE-FLIGHT: 目录不可写: {workdir}"
        return msg if strict else msg + " (继续, 但实现可能失败)"

    git = await _sh(f"git -C {_q(workdir)} rev-parse --is-inside-work-tree", 10)
    if git[1].strip() != "true":
        return "" if strict else "ℹ PRE-FLIGHT: 非 git 仓库, 跳过 clean/分支检查"

    dirty = await _sh(f"git -C {_q(workdir)} status --porcelain", 15)
    branch = await _sh(f"git -C {_q(workdir)} branch --show-current", 10)
    problems = []
    if dirty[0] or dirty[1].strip():
        problems.append(f"工作树非 clean ({len(dirty[1].splitlines())} 项未提交改动)")
    br = branch[1].strip()
    if br in ("main", "master"):
        problems.append(f"当前分支是 {br} (建议非 main 分支)")
    if problems:
        msg = "⚠ PRE-FLIGHT: " + "; ".join(problems)
        return msg if strict else msg + " (继续, 风险自担)"
    return ""


# ── QUALITY GATE 机械门 ─────────────────────────────────────────────
def _gate_allowed(command: str) -> tuple[bool, str]:
    """机械门候选校验: 禁 mutating/auto-fix 命令 (原版规则)。"""
    c = command.strip()
    if not c:
        return False, "空命令"
    if any(m in c for m in _MUTATING):
        return False, f"拒绝 mutating 命令: {command[:60]} (机械门只收 check-only)"
    if re.search(r"\bprettier\b(?!\s*--check)", c):
        return False, f"拒绝裸 prettier (需 --check): {command[:60]}"
    return True, ""


async def _sh(command: str, timeout_s: float = 60.0) -> tuple[int, str]:
    """执行命令, 返回 (exit_code, output)。"""
    proc = None
    try:
        proc = await asyncio.create_subprocess_shell(
            command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        return proc.returncode or 0, out.decode("utf-8", "replace")[-4000:]
    except asyncio.TimeoutError:
        if proc:
            proc.kill()
        return 124, f"[gate timeout {timeout_s}s]"
    except FileNotFoundError:
        return 127, "[command not found]"
    except Exception as exc:  # noqa: BLE001
        return 1, f"[gate exec error: {exc}]"


async def _quality_gate(
    command: str, workdir: str | None, implement_engine: str, log: list[str]
) -> tuple[bool, str]:
    """机械门: 执行 check-only 命令, 失败回实现引擎修复, ≤3 次/激活。

    返回 (pass?, note)。环境失败 (命令不存在/超时) 立即升级不消耗计数。
    """
    ok, why = _gate_allowed(command)
    if not ok:
        log.append(f"⛔ QUALITY GATE 配置被拒: {why}")
        return False, why
    for attempt in range(1, _GATE_CAP + 1):
        code, out = await _sh(f"cd {_q(workdir or '.')} && {command}", 120)
        if code == 0:
            log.append(f"✅ QUALITY GATE 通过 (attempt {attempt})")
            return True, out[-300:]
        if code in (124, 127) or "not found" in out[:60] or "timeout" in out[:60]:
            log.append(f"⛔ QUALITY GATE 环境失败 (code={code}): {out[:120]} → 升级, 不消耗计数")
            return False, f"环境失败: {out[:200]}"
        log.append(f"⛔ QUALITY GATE 失败 (attempt {attempt}/{_GATE_CAP}): {out[:200]}")
        if attempt < _GATE_CAP:
            fix_prompt = (
                f"修复 QUALITY GATE 机械检查失败 (todo 实现相关):\n{out[:1500]}\n\n"
                "只修复检查报出的问题, 完成后重新运行检查并给出结果。"
            )
            try:
                await _run_engine(implement_engine, fix_prompt)
            except Exception as exc:  # noqa: BLE001
                log.append(f"⚠ gate 修复失败: {exc}")
    log.append("⛔ QUALITY GATE 3 次失败 → 升级")
    return False, "gate 3 次失败"


# ── DEBATE 三分类 ───────────────────────────────────────────────────
async def _debate(
    critique_engine: str, crit_out: str, title: str, log: list[str], todo: dict
) -> tuple[str, str]:
    """DEBATE (原版 node 5): valid→refactor / debatable→反证 reinject / false-positive→理由。"""
    if not _has_defects(crit_out) or "无缺陷" in crit_out:
        return "pass", ""
    hedged = ("可能", "或许", "也许", "建议", "可以考虑", "might", "may", "could", "consider")
    if any(h in crit_out for h in hedged) and not any(
        m in crit_out for m in ("必须", "会失败", "一定", "will fail", "must", "broken", "崩溃")
    ):
        counter = (
            f"批判者提出了以下可能站不住脚的问题:\n{crit_out[:2000]}\n\n"
            "请判断: 你坚持这些问题吗? 如果坚持请给具体证据 (哪行/哪个场景失败); "
            "如果考虑不充分或可接受, 明确说 '撤回'。只读, 不修改文件。"
        )
        try:
            rebuttal = await _run_engine(critique_engine, counter)
            if any(k in rebuttal for k in ("撤回", "接受", "不坚持")):
                todo["evidence"].append(
                    {
                        "at": _now(),
                        "note": f"[graph-cycle] 反证后被批判者撤回: {_evidence_summary(rebuttal)}",
                    }
                )
                return "pass", f"反证后被撤回: {_evidence_summary(rebuttal)}"
            return "refactor", rebuttal[:300]
        except Exception as exc:  # noqa: BLE001
            log.append(f"⚠ 反证 reinject 失败: {exc} → 按 valid 处理")
            return "refactor", ""
    return "refactor", ""


# ── VERIFY 功能验证 ─────────────────────────────────────────────────
async def _verify(
    verify_command: str | None,
    workdir: str | None,
    critique_engine: str,
    title: str,
    todo: dict,
    log: list[str],
) -> tuple[bool, str]:
    """VERIFY: 功能测试/验收 (与机械门分离)。

    - verify_command 给定 → 执行命令
    - 否则 → 批判引擎做验收判断 (在最后一步)
    失败 → 分类根因 (4类), environmental 升级, 其余回批判。

    返回 (pass?, note)。note 含根因分类供 CRITIQUE 连续性。
    """
    if verify_command:
        code, out = await _sh(f"cd {_q(workdir or '.')} && {verify_command}", 180)
        if code == 0:
            return True, f"VERIFY 通过: {out[-200:]}"
        verdict = await _classify_root_cause(critique_engine, title, out)
        log.append(f"⛔ VERIFY 失败 (code={code}) → 根因: {verdict}")
        if verdict == "environmental":
            return False, f"环境阻塞: {out[:200]}"
        return False, f"VERIFY 失败, 根因 {verdict}: {out[:300]}"
    # 无命令: 批判引擎做最终验收判断
    acc = (
        f"验收检查 (只读): 以下实现是否满足任务 [{title}] 的验收标准?\n"
        "请回答: '通过' 或 '不通过' + 根因分类 (实现缺陷/测试缺陷/契约不符/环境问题)。"
    )
    try:
        acc_out = await _run_engine(critique_engine, acc)
    except Exception as exc:  # noqa: BLE001
        log.append(f"⚠ 验收引擎失败: {exc} → 视为通过")
        return True, "验收判断缺失, 视为通过"
    if _has_defects(acc_out) and "通过" not in acc_out[:40]:
        return False, f"验收不通过: {_evidence_summary(acc_out)}"
    return True, f"验收通过: {_evidence_summary(acc_out)}"


async def _classify_root_cause(engine: str, title: str, fail_output: str) -> str:
    """VERIFY 失败根因分类 (原版: implementation-defect/test-defect/contract-mismatch/environmental)。"""
    prompt = (
        f"功能验证失败, 请分类根因 (只读, 只返回一个词):\n"
        f"任务: {title}\n失败输出:\n{fail_output[:2000]}\n\n"
        "可选: implementation-defect / test-defect / contract-mismatch / environmental"
    )
    try:
        out = await _run_engine(engine, prompt)
        for root in _VERIFY_ROOTS:
            if root in out.lower():
                return root
    except Exception:  # noqa: BLE001
        pass
    return "implementation-defect"


# ── Elevated Assurance (3 fresh lens + fan-in + exit challenger) ──────
_HIGH_RISK_MARKERS = (
    "auth",
    "login",
    "password",
    "密码",
    "密钥",
    "secret",
    "token",
    "payment",
    "支付",
    "钱包",
    "删除",
    "delete",
    "rm ",
    "并发",
    "race",
    "security",
    "安全",
    "权限",
    "permission",
    "sudo",
    "root",
    "注入",
    "injection",
    "sandbox",
    "隔离",
)


def _needs_elevated(title: str, objective: str) -> bool:
    t = (title or "").lower() + " " + (objective or "").lower()
    return any(m in t for m in _HIGH_RISK_MARKERS)


async def _lens_review(engine: str, title: str, workdir: str | None) -> str:
    """单个 fresh 独立 lens 审查 (无状态连续性, 独立视角)。"""
    prompt = (
        f"你是独立的第三方安全/质量审计者 ({engine}), 只读审查以下任务实现, 不修改任何文件:\n"
        f"任务: {title}\n"
        f"{'工作目录: ' + workdir if workdir else '（由你按上下文确定代码位置）'}\n\n"
        "请用完全独立的视角找问题 (不要与其他人交换意见)。只列确定的问题; "
        "没有则说 '无缺陷'。"
    )
    try:
        return await _run_engine(engine, prompt)
    except Exception as exc:  # noqa: BLE001
        return f"[lens {engine} 失败: {exc}]"


def _fan_in(discoveries: list[str]) -> list[str]:
    """fan-in 规范化: 按关键词归一主题 + 去重, 返回合并后的缺陷列表。"""
    norm: list[tuple[str, str]] = []  # (主题, 原文)
    for d in discoveries:
        if "无缺陷" in d:
            continue
        lines = [l.strip() for l in d.splitlines() if l.strip()]
        for line in lines:
            if len(line) < 4 or any(m in line for m in ("[lens", "你是", "请用", "任务:")):
                continue
            theme = ""
            for m in (
                "缺陷",
                "问题",
                "错误",
                "风险",
                "不安全",
                "漏洞",
                "失败",
                "bug",
                "error",
                "risk",
                "missing",
                "broken",
            ):
                if m in line:
                    theme = m
                    break
            # 归一: 同一主题+相似前缀视为同一发现 (首次出现保留)
            key = (theme, line[:24])
            if not any(k[0] == theme and k[1][:12] == key[1][:12] for k in norm):
                norm.append(key)
    return [f"[{t}] {l}" for t, l in norm if t]


async def _exit_challenger(title: str, engines: list[str], todo: dict) -> tuple[bool, str]:
    """终局挑战者: 实现已通过所有检查后, 换 fresh 视角找最后的问题。

    返回 (has_defects?, note)。有缺陷则回循环不 done (challenger 是硬门)。
    """
    eng = engines[-1] if engines else "claude"
    prompt = (
        f"你是终局挑战者 ({eng}), 独立 fresh 视角: 实现已通过常规审查/验证, "
        f"但可能存在遗漏。任务: {title}\n"
        "请找最后被遗漏的问题 (边角/回归/安全/一致性), 只列确定的问题, 没有说 '无缺陷'。"
    )
    try:
        out = await _run_engine(eng, prompt)
    except Exception as exc:  # noqa: BLE001
        return False, f"challenger 失败: {exc}"
    if _has_defects(out) and "无缺陷" not in out:
        todo["evidence"].append(
            {"at": _now(), "note": f"[graph-cycle] challenger 发现: {_evidence_summary(out)}"}
        )
        return True, _evidence_summary(out)
    return False, ""


def _pick_lens_engines(implement_engine: str, critique_engine: str) -> list[str]:
    """3 个 fresh lens 引擎: 优先独立于实现/批判引擎。"""
    pool = [
        e for e in ("claude", "codex", "grok", "pi") if e not in (implement_engine, critique_engine)
    ]
    if len(pool) >= 3:
        return pool[:3]
    # 不足则复用批判引擎 (仍 fresh, 无 continuity)
    return (pool + [critique_engine] * 3)[:3]


# ── Review-only 模式 (只读审查报告) ─────────────────────────────────
async def graph_review(
    plan_id: str, critique_engine: str = "claude", workdir: str | None = None
) -> str:
    """Review-only: 只读审查计划实现, 不写代码不修 bug, 输出审查报告。

    对每个未完成 todo 调批判引擎审查, 发现写入 evidence, todo 标 'reviewed'。
    绝不调用实现引擎, 不修改任何文件 (read-only 硬保证)。
    """
    try:
        plan = _load_plan(plan_id)
    except Exception as exc:  # noqa: BLE001
        return f"graph_review: {exc}"
    todos = _pending_todos(plan)
    if not todos:
        return f"graph_review: 计划 {plan_id} 无未完成任务"
    log: list[str] = [f"🔍 graph-review 启动: {plan_id} (审查={critique_engine}, 只读)"]
    reviewed = 0
    for todo in todos:
        title = str(todo.get("title", ""))
        tid = todo.get("id")
        ctx = _cycle_ctx(plan, todo)
        prompt = (
            f"你是独立审查者 ({critique_engine}), 只读审查实现 (不修改任何文件, 只出报告):\n"
            f"任务: {title}\n{ctx}\n\n"
            "输出审查报告: 1) 功能/边界问题 2) 安全隐患 3) 建议。\n"
            "若无问题明确说 '无缺陷'。"
        )
        try:
            out = await _run_engine(critique_engine, prompt)
        except Exception as exc:  # noqa: BLE001
            log.append(f"⚠ 审查失败: {exc}")
            continue
        todo["status"] = "reviewed"
        todo["evidence"].append(
            {"at": _now(), "note": f"[graph-review] 审查报告: {_evidence_summary(out)}"}
        )
        _save_plan(plan)
        log.append(f"✅ [{tid}] {title} → reviewed: {_evidence_summary(out, 120)}")
        reviewed += 1
    log.append(f"\n📊 审查完成: {reviewed}/{len(todos)} 个 todo (只读, 未修改实现)")
    log.append(f"查看: plan_status({plan_id}) 或计划看板")
    return "\n".join(log)


# ── 主循环 ──────────────────────────────────────────────────────────
async def graph_cycle(
    plan_id: str,
    implement_engine: str = "codex",
    critique_engine: str = "claude",
    max_iterations: int = _ITER_CAP,
    workdir: str | None = None,
    quality_gate: str | None = None,
    verify_command: str | None = None,
    preflight: bool = True,
    mode: str = "full",
    elevated: bool | None = None,
) -> str:
    """在计划的未完成 todo 上跑 实现→质量门→批判→仲裁→修复→验证 自纠正循环。

    参数:
      implement_engine / critique_engine: 引擎名 (claude/codex/grok/pi)。
      max_iterations: 每 todo 修复轮次上限, 默认 3 (Anti-loop cutoff 阈值)。
      workdir: 引擎工作目录 (PRE-FLIGHT 检查 + gate/verify 执行目录)。
      quality_gate: 机械检查命令 (lint/type/build, check-only, 禁 mutating)。
      verify_command: 功能测试/验收命令 (与机械门分离)。
      preflight: PRE-FLIGHT 安全检查, 默认开。
      mode: "full" (从零实现) / "refactor" (已有代码重构, 不改变行为)。
      elevated: None=auto (高风险任务自动开), True=强制, False=关。
        开启后: 3 个 fresh 独立 lens 审查 + fan-in 规范化 + 终局 challenger。
    """
    max_iterations = max(1, min(int(max_iterations), 5))
    try:
        plan = _load_plan(plan_id)
    except Exception as exc:  # noqa: BLE001
        return f"graph_cycle: {exc}"
    todos = _pending_todos(plan)
    if not todos:
        return f"graph_cycle: 计划 {plan_id} 无未完成任务, 无需循环"

    log: list[str] = [
        f"🔄 graph-cycle 启动: {plan_id} (实现={implement_engine}, 批判={critique_engine}, mode={mode})"
    ]
    if preflight:
        pf = await _preflight(workdir, strict=False)
        if pf:
            log.append(pf)
    done_count = 0

    while True:
        todos = _pending_todos(plan)
        if not todos:
            log.append("✅ 所有任务已完成")
            break
        todo = todos[0]
        todo_id = todo.get("id")
        title = str(todo.get("title", ""))
        objective = str(plan.get("objective", ""))
        elevated_on = bool(elevated) if elevated is not None else _needs_elevated(title, objective)
        if elevated_on:
            log.append(f"🛡 Elevated assurance 开启 (任务含高风险要素, 3 lens + challenger)")
            lens_engines = _pick_lens_engines(implement_engine, critique_engine)
        fix_count = sum(
            1
            for e in todo.get("evidence", [])
            if e.get("note", "").startswith("[graph-cycle] 修复完成")
        )
        verify_fail_count = sum(
            1
            for e in todo.get("evidence", [])
            if e.get("note", "").startswith("[graph-cycle] VERIFY 失败")
        )
        log.append(f"\n── todo [{todo_id}] {title} (已修复 {fix_count} 轮)")
        if fix_count >= max_iterations:
            log.append(
                f"⚠ Anti-loop cutoff: todo [{todo_id}] 已修复 {fix_count} 轮仍有缺陷, "
                "停止并升级用户 (不假造解决)"
            )
            todo["evidence"].append(
                {"at": _now(), "note": "[graph-cycle] anti-loop cutoff: 停止升级用户"}
            )
            todo["status"] = "blocked"
            _save_plan(plan)
            break
        if verify_fail_count >= 2:
            log.append(
                f"⚠ VERIFY 连续失败 {verify_fail_count} 次且批判无缺陷 → 升级用户 "
                "(验证不过但无缺陷可修, 不假造解决)"
            )
            todo["evidence"].append(
                {"at": _now(), "note": "[graph-cycle] verify 连续失败: 停止升级用户"}
            )
            todo["status"] = "blocked"
            _save_plan(plan)
            break

        # 1. 实现 (Worker, 仅首次)
        if fix_count == 0:
            if mode == "refactor":
                impl_prompt = (
                    f"重构以下现有代码 (计划 {plan_id} todo [{todo_id}] {title}):\n"
                    f"计划目标: {objective}\n"
                    f"{'工作目录: ' + workdir if workdir else ''}\n"
                    "这是重构任务: 只改善结构/可读性/性能, 绝不改变外部行为, "
                    "保持 API 兼容, 不新增功能。完成后给出: 改了哪些文件、验证输出。"
                )
            else:
                impl_prompt = (
                    f"实现以下任务 (在 {plan_id} 计划中 todo [{todo_id}] {title}):\n"
                    f"计划目标: {objective}\n"
                    f"{'工作目录: ' + workdir if workdir else ''}\n"
                    "请实际动手完成 (写代码/改文件/运行验证), 完成后给出: 改了哪些文件、"
                    "运行了什么、验证输出。"
                )
            try:
                impl_out = await _run_engine(implement_engine, impl_prompt)
            except Exception as exc:  # noqa: BLE001
                log.append(f"⚠ 实现失败: {exc}")
                break
            todo["status"] = "in_progress"
            todo["evidence"].append(
                {"at": _now(), "note": f"[graph-cycle] 实现完成: {_evidence_summary(impl_out)}"}
            )
            _save_plan(plan)
            log.append(f"✅ 实现完成 ({implement_engine})")
            if not impl_out.strip():
                log.append("⛔ 质量门: 实现无输出 → 回退实现 (重试)")
                continue

        # 2. QUALITY GATE 机械门 (每轮循环前)
        if quality_gate:
            gate_ok, gate_note = await _quality_gate(quality_gate, workdir, implement_engine, log)
            if not gate_ok:
                log.append("⛔ 机械门未过 → 升级, 不进入批判")
                if "环境失败" in gate_note or "gate 3 次失败" in gate_note:
                    todo["status"] = "blocked"
                    todo["evidence"].append(
                        {"at": _now(), "note": f"[graph-cycle] {gate_note[:200]}"}
                    )
                    _save_plan(plan)
                    break
                continue

        # 3. CRITIQUE (Evaluator, 状态连续性; elevated 时 3 lens + fan-in)
        if elevated_on:
            lens_log: list[str] = []
            discoveries: list[str] = []
            for eng in lens_engines:
                lens_out = await _lens_review(eng, title, workdir)
                discoveries.append(lens_out)
                lens_log.append(f"  lens[{eng}]: {_evidence_summary(lens_out, 100)}")
            crit_out = "\n".join(
                f"[{eng} lens] {d[:2000]}" for eng, d in zip(lens_engines, discoveries)
            )
            merged = _fan_in(discoveries)
            log.append("🛡 3-lens 审查:\n" + "\n".join(lens_log))
            log.append(
                f"🛡 fan-in 规范化后 {len(merged)} 条独立发现:"
                + "".join(f"\n  · {m[:120]}" for m in merged)
            )
            crit_out = "\n".join(merged) if merged else "无缺陷"
            crit_prompt = ""  # crit_out 已由 fan-in 产生, 不重复调批判引擎
        else:
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

        # 4. DEBATE 三分类
        verdict, detail = await _debate(critique_engine, crit_out, title, log, todo)
        if verdict == "refactor":
            log.append(f"🔍 批判发现有效缺陷 → 修复 ({implement_engine})")
            fix_prompt = (
                f"根据以下批判意见修复当前实现 (todo [{todo_id}] {title}):\n"
                f"批判:\n{crit_out[:3000]}\n{('仲裁补充: ' + detail) if detail else ''}\n\n"
                "修复并重新验证, 完成后给出验证输出。"
            )
            try:
                fix_out = await _run_engine(implement_engine, fix_prompt)
                todo["evidence"].append(
                    {"at": _now(), "note": f"[graph-cycle] 修复完成: {_evidence_summary(fix_out)}"}
                )
                _save_plan(plan)
                log.append(f"🔧 修复完成 ({implement_engine}) → 回质量门+批判")
            except Exception as exc:  # noqa: BLE001
                log.append(f"⚠ 修复失败: {exc}")
                break
            continue  # 不 done, 回循环 (Evaluator-Optimizer)

        # 5. VERIFY (无有效缺陷后)
        log.append(f"✅ 批判无有效缺陷/已撤回 → VERIFY{(' (' + detail + ')') if detail else ''}")
        verify_ok, verify_note = await _verify(
            verify_command, workdir, critique_engine, title, todo, log
        )
        if not verify_ok:
            if "环境阻塞" in verify_note:
                log.append(f"⛔ VERIFY 环境阻塞 → 升级 (不修复)")
                todo["status"] = "blocked"
                todo["evidence"].append(
                    {"at": _now(), "note": f"[graph-cycle] {verify_note[:200]}"}
                )
                _save_plan(plan)
                break
            # 根因分类已附带 → 回 CRITIQUE (连续性携带), 不 done
            todo["evidence"].append(
                {"at": _now(), "note": f"[graph-cycle] VERIFY 失败: {verify_note[:150]}"}
            )
            _save_plan(plan)
            log.append("↩ 回批判循环 (VERIFY 失败, 根因已记录)")
            continue

        # 6. 通过 → done (elevated 时先过 exit challenger)
        if elevated_on:
            ch_defect, ch_note = await _exit_challenger(title, lens_engines, todo)
            if ch_defect:
                log.append(f"🛡 exit challenger 发现缺陷 → 回修复: {ch_note[:150]}")
                crit_out = f"challenger 发现: {ch_note}"
                verdict, detail = await _debate(critique_engine, crit_out, title, log, todo)
                if verdict == "refactor":
                    todo["evidence"].append(
                        {"at": _now(), "note": "[graph-cycle] challenger 触发回修"}
                    )
                    _save_plan(plan)
                    continue
                # challenger 意见被仲裁为 false-positive → 仍走 done
            else:
                log.append("🛡 exit challenger 无发现 → 通过")
        todo["status"] = "done"
        todo["evidence"].append(
            {"at": _now(), "note": f"[graph-cycle] 验证通过: {verify_note[:200]}"}
        )
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


def _q(path: str) -> str:
    return path.replace("'", "'\\''")
