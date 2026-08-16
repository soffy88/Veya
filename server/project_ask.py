"""project_ask — 项目任务的唯一对外入口 (M2 + M3 dsh adapter)。

硬约束 (2026-08-15 审查结论): Coordinator 只调用这一个 tool；self-do
(builtin) vs. 派工 (hicode / dsh) 的决策在本函数内部完成，不得拆成多个
tool 让 Coordinator 在中间做分支路由。权威任务调度仍是 server.hicode_queue
.HicodeTaskQueue（不新建第二套队列），本模块只负责：

  1. 决定 builtin（只记录到 DECISIONS.md，不改代码/不执行任何命令）还是
     派工执行（hicode 或 dsh）——builtin 的语义边界必须清楚: 调用方以为
     「ask 了就等于改完代码」是误用, tool description 里已写明。
  2. 派工时把 .veya-project/ 记忆（STATE/DECISIONS/LESSONS）拼进任务上下文；
  3. 用 server.project_store.to_project_status 把 HicodeTaskQueue 的内部
     状态收敛成项目侧终态 completed | blocked（不允许静默丢失 —— 派工/等待
     过程中的任何异常、dsh 不可用/超时/无 verdict，都收敛为 blocked + 原因，
     而不是让异常裸露给调用方，也不会隐式 fallback 到另一个 worker
     ——避免同一请求被两个 worker 双跑）；
  4. 写回 .veya-project/（DECISIONS.md 或队列镜像），保证下次调用能看到历史。

注意：hicode 执行仍受 HICODE_WORKSPACE 沙箱边界约束（server.hicode_agent
._resolve_workspace）——如果 project_root 不在该沙箱根内，派工会以
blocked + 明确原因收场，这是既有安全边界的正常体现，不在本模块里绕过；
调用方需要传一个落在 HICODE_WORKSPACE 内的 project_root。

assignee_hint 是白名单枚举: None（自动启发）| builtin | hicode | dsh；
其余任何值一律直接 blocked，不落到启发式或任何 worker。

Understand 门禁 (docs/PROJECT_AGENT.md §7, 2026-08-16 补齐): 在上述派工决策之前
先跑一次 server.project_understand.understand() 判定。判定为 ask → 只追问、
早退（不建业务副作用、不派工），phase=understood_ask；判定为 act → 把
interpretation/assumptions 前置进 hicode/dsh 的 brief，再走既有执行腿，
phase=executed。mode=act_eager 跳过判定直接执行；mode=ask_only 强制只追问、
永不执行。默认 mode 由 PROJECT_ASK_DEFAULT_MODE（缺省 auto）决定。
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import shutil
import time
from typing import Any

from server.events import fire_step
from server.project_store import ProjectAskResponse, ProjectStore, to_project_status
from server.project_understand import (
    UnderstandResult,
    eager_act_result,
    force_confirm_ask,
    understand,
)

logger = logging.getLogger("veya.project_ask")

_VALID_MODES = {"auto", "act_eager", "ask_only"}

_EXEC_HINTS = (
    "修复",
    "实现",
    "重构",
    "写代码",
    "改代码",
    "跑测试",
    "部署",
    "调试",
    "fix",
    "implement",
    "refactor",
    "bug",
    "test",
    "build",
    "deploy",
    "debug",
)

_VALID_HINTS = {"builtin", "hicode", "dsh"}


def _decide_assignee(request: str, hint: str | None) -> str:
    """builtin（只记录）/ hicode / dsh（派工执行）。

    hint 显式优先（已在 project_ask() 里做过白名单校验); 没给 hint 时按
    关键词启发 —— 启发式只在 builtin/hicode 间选, dsh 只能靠显式 hint 触发
    （新 worker 上线期先不参与自动判断, 降低误伤面）。
    """
    if hint in _VALID_HINTS:
        return hint
    low = request.lower()
    if any(k in request or k in low for k in _EXEC_HINTS):
        return "hicode"
    return "builtin"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_task_id() -> str:
    return f"pa_{int(time.time() * 1000) % 10_000_000:07d}"


def _context_prefix(store: ProjectStore) -> str:
    """拼装项目记忆作为派工上下文前缀（截断，避免撑爆 token）。"""
    state = store.read_state()[:2000]
    decisions = store.read_decisions()[-1500:]
    lessons = store.read_lessons()[-1500:]
    parts = [f"## Authoritative project memory (.veya-project/)\n\n### PROJECT_STATE.md\n{state}"]
    if decisions.strip():
        parts.append(f"### DECISIONS.md (recent)\n{decisions}")
    if lessons.strip():
        parts.append(f"### LESSONS.md (recent)\n{lessons}")
    return "\n\n".join(parts)


def _load_chain(store: ProjectStore, parent_task_id: str | None) -> list[dict[str, Any]]:
    """读取 parent_task_id 对应的 understand.json（续答链，单层：只看直接 parent）。"""
    if not parent_task_id:
        return []
    parent = store.read_understand(parent_task_id)
    return [parent] if parent else []


def _understand_prefix(u: UnderstandResult, request: str, chain: list[dict[str, Any]]) -> str:
    """Act 阶段写进 brief.md 顶部的 Understand 摘要 (PROJECT_AGENT.md §7.5)。"""
    parts = [f"## Understand\n- Interpretation: {u.interpretation}"]
    if u.assumptions:
        parts.append("- Assumptions:\n" + "\n".join(f"  - {a}" for a in u.assumptions))
    parts.append(f"- Original user request: {request}")
    for turn in chain:
        parts.append(f"- Prior round request: {turn.get('request', '')}")
        parts.append(f"- Prior round interpretation: {turn.get('interpretation', '')}")
    return "\n".join(parts)


def _run_builtin(store: ProjectStore, task_id: str, request: str) -> ProjectAskResponse:
    """builtin：不执行，只把请求记成一条决策条目。"""
    store.append_decision(
        f"## {_now()} — project_ask (builtin)\n- Request: {request}\n- Task: {task_id}"
    )
    return ProjectAskResponse(
        task_id=task_id,
        status="completed",
        summary=f"已记录到 DECISIONS.md（builtin，未执行代码变更）: {request[:200]}",
    )


async def _run_hicode(
    store: ProjectStore,
    task_id: str,
    project_root: str,
    request: str,
    understand_prefix: str = "",
) -> ProjectAskResponse:
    """hicode：派给既有 HicodeTaskQueue 执行，异常一律收敛为 blocked。

    force_cli=True：强制走 CLI 路径而不是 hicode serve。原因 (2026-08-15
    真机 smoke 验证发现)：hicode serve 是单一持久会话, 不接受按任务传入的
    workspace, 传入的 workspace 只用于任务前 git 快照——如果不强制 CLI,
    不同 project_root 的任务会全部在 serve 那一个固定目录里执行, 而不是
    调用方指定的 project_root, 彻底破坏 project_ask 的多项目隔离前提。
    """
    from server.hicode_queue import hicode_task_queue

    parts = [p for p in (understand_prefix, _context_prefix(store)) if p]
    brief = "\n\n".join([*parts, f"## Task\n{request}"])
    run_dir = store.run_dir(task_id)
    (run_dir / "brief.md").write_text(brief, encoding="utf-8")

    try:
        worker_tid = await hicode_task_queue.submit(
            brief,
            workspace=project_root,
            meta={"project_ask": task_id, "force_cli": True},
        )
        rec = await hicode_task_queue.wait(worker_tid)
    except Exception as exc:  # noqa: BLE001 — 派工/等待异常也要收敛成 blocked, 不裸露给调用方
        logger.exception("project_ask hicode 派工异常 %s", task_id)
        (run_dir / "worker.log").write_text(f"dispatch error: {exc}", encoding="utf-8")
        return ProjectAskResponse(
            task_id=task_id, status="blocked", block_reason=f"dispatch error: {exc}"
        )

    status, reason = to_project_status(rec.status, rec.error)
    (run_dir / "worker.log").write_text(rec.summary or rec.error or "", encoding="utf-8")
    return ProjectAskResponse(
        task_id=task_id,
        status=status,
        summary=rec.summary,
        block_reason=reason,
        artifacts=[str(run_dir / "worker.log")],
    )


_DSH_TIMEOUT_S = 1800  # 与 hicode 默认超时对齐
_DSH_PROMPT_CHAR_LIMIT = 6000  # headless 任务是 argv 位置参数, 截断避免 ARG_MAX/超长上下文


def _resolve_dsh_bin() -> str | None:
    """DSH_BIN 环境变量可覆盖, 默认在 PATH 里找 'dsh'; 找不到 → None (不可用)。"""
    return shutil.which(os.environ.get("DSH_BIN", "dsh"))


async def _dsh_exec(bin_path: str, prompt: str, cwd: str, timeout_s: int) -> tuple[int, str, str]:
    """拉起 dsh headless 子进程执行 prompt。

    官方形态 (apps/cli/README, 2026-08 核实): `dsh --profile headless "<task>"`
    ——一次性任务: 新会话 → 跑完 → 打印最终回答 → 退出; 没有 `run --brief-file`
    这种子命令, 任务是位置参数字符串, 不是文件 flag。cwd=项目根 (workspace root)。
    独立小函数, 便于测试直接 monkeypatch 掉, 不依赖真 dsh 二进制。
    """
    proc = await asyncio.create_subprocess_exec(
        bin_path,
        "--profile",
        "headless",
        prompt,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return (
        proc.returncode or 0,
        out_b.decode("utf-8", "replace"),
        err_b.decode("utf-8", "replace"),
    )


def _parse_dsh_verdict(stdout: str) -> tuple[str | None, str]:
    """宽松解析 dsh 输出里的 VERDICT/SUMMARY 行（软性约定，官方 headless 不保证输出这个页脚）。

    找不到合法 VERDICT 行 → 返回 (None, "")；调用方按 exit code + stdout 兜底判定
    (见 _run_dsh)，不做隐式 fallback 到 hicode。
    """
    verdict: str | None = None
    summary = ""
    for line in stdout.splitlines():
        line = line.strip()
        if line.upper().startswith("VERDICT:"):
            v = line.split(":", 1)[1].strip().lower()
            if v in ("completed", "blocked"):
                verdict = v
        elif line.upper().startswith("SUMMARY:"):
            summary = line.split(":", 1)[1].strip()
    return verdict, summary


async def _run_dsh(
    store: ProjectStore,
    task_id: str,
    project_root: str,
    request: str,
    understand_prefix: str = "",
) -> ProjectAskResponse:
    """dsh：外部 CLI worker (headless profile)。不可用 / 超时一律 blocked；

    exit 0 且有输出 → completed (headless 本就不保证 VERDICT 页脚, 靠这个兜底);
    exit ≠ 0 → blocked + stderr。不自动 fallback 到 hicode
    (同一请求被两个 worker 双跑, 比等一次人工重试更危险)。
    """
    run_dir = store.run_dir(task_id)
    parts = [p for p in (understand_prefix, _context_prefix(store)) if p]
    brief = "\n\n".join(
        [
            *parts,
            f"## Task\n{request}\n\n"
            "如果方便，请在最后一行输出 VERDICT: completed|blocked（可选，不保证遵守）。",
        ]
    )
    (run_dir / "brief.md").write_text(brief, encoding="utf-8")  # 完整版落盘, 便于审计
    prompt = brief
    if len(prompt) > _DSH_PROMPT_CHAR_LIMIT:
        prompt = prompt[:_DSH_PROMPT_CHAR_LIMIT] + "\n...[truncated]"

    bin_path = _resolve_dsh_bin()
    if not bin_path:
        reason = "dsh not available (binary not found on PATH)"
        (run_dir / "worker.log").write_text(reason, encoding="utf-8")
        return ProjectAskResponse(task_id=task_id, status="blocked", block_reason=reason)

    try:
        code, out, err = await _dsh_exec(bin_path, prompt, project_root, _DSH_TIMEOUT_S)
    except asyncio.TimeoutError:
        reason = f"dsh timed out after {_DSH_TIMEOUT_S}s"
        (run_dir / "worker.log").write_text(reason, encoding="utf-8")
        return ProjectAskResponse(task_id=task_id, status="blocked", block_reason=reason)
    except Exception as exc:  # noqa: BLE001 — 派工异常也要收敛成 blocked, 不裸露给调用方
        logger.exception("project_ask dsh 派工异常 %s", task_id)
        reason = f"dispatch error: {exc}"
        (run_dir / "worker.log").write_text(reason, encoding="utf-8")
        return ProjectAskResponse(task_id=task_id, status="blocked", block_reason=reason)

    (run_dir / "worker.log").write_text(f"$ exit={code}\n{out}\n{err}", encoding="utf-8")
    artifacts = [str(run_dir / "worker.log")]
    verdict, summary = _parse_dsh_verdict(out)
    if verdict == "completed":
        return ProjectAskResponse(
            task_id=task_id,
            status="completed",
            summary=summary or out.strip()[-400:],
            artifacts=artifacts,
        )
    if verdict == "blocked":
        return ProjectAskResponse(
            task_id=task_id,
            status="blocked",
            block_reason=summary or "dsh reported blocked",
            artifacts=artifacts,
        )
    # 无 VERDICT 页脚 (headless 常态) → 按 exit code 兜底判定, 不是 fallback 到另一个 worker
    if code == 0 and out.strip():
        return ProjectAskResponse(
            task_id=task_id,
            status="completed",
            summary=out.strip()[-400:],
            artifacts=artifacts,
        )
    reason = (err.strip() or out.strip() or f"dsh exited {code} with no output")[:400]
    return ProjectAskResponse(
        task_id=task_id, status="blocked", block_reason=reason, artifacts=artifacts
    )


def _write_back(store: ProjectStore, resp: ProjectAskResponse, request: str, assignee: str) -> None:
    mirror = store.load_queue_mirror()
    tasks = mirror.setdefault("tasks", [])
    tasks.append(
        {
            "id": resp.task_id,
            "assignee": assignee,
            "status": resp.status,
            "phase": resp.phase,
            "request": request[:200],
            "summary": (resp.summary or "")[:400],
            "block_reason": resp.block_reason,
            "updated_at": _now(),
        }
    )
    mirror["tasks"] = tasks[-200:]  # 限长, 镜像非权威不需要无限增长
    store.save_queue_mirror(mirror)


def _render(resp: ProjectAskResponse) -> str:
    if resp.phase == "understood_ask":
        lines = [
            f"❓ #{resp.task_id} need_clarification: {resp.interpretation or '(no interpretation)'}"
        ]
        lines.extend(f"  {i}. {q}" for i, q in enumerate(resp.questions, 1))
        lines.append(f"  (续答请带 parent_task_id={resp.task_id})")
        return "\n".join(lines)
    if resp.status == "completed":
        return f"✅ #{resp.task_id} completed: {resp.summary or '(no summary)'}"
    return f"⛔ #{resp.task_id} blocked: {resp.block_reason or 'unknown reason'}"


def project_status(project_root: str, limit: int = 5) -> str:
    """只读查看某个项目的 .veya-project/ 记忆状态。不派发任何任务、不修改代码、

    也不会仅因为查询就把 .veya-project/ 建出来 —— 与 project_ask 是唯一允许
    并存的第二入口，因为它不做任何 self-do/派工的决策，只是展示。
    """
    store = ProjectStore(project_root)
    if not store.dir.exists():
        return f"{store.dir} 尚不存在 —— 该项目还没有被 project_ask 处理过。"

    mirror = store.load_queue_mirror()
    tasks = mirror.get("tasks", [])
    recent = tasks[-limit:] if limit > 0 else []

    lines = [
        f"项目记忆: {store.dir}",
        f"任务记录: 共 {len(tasks)} 条（镜像，非权威；权威源是 HicodeTaskQueue 内存态）",
    ]
    if recent:
        lines.append(f"最近 {len(recent)} 条:")
        for t in reversed(recent):
            status = t.get("status")
            icon = "✅" if status == "completed" else "⛔" if status == "blocked" else "…"
            lines.append(
                f"  {icon} #{t.get('id')} [{t.get('assignee')}] {status} — "
                f"{(t.get('request') or '')[:60]}"
            )
    else:
        lines.append("暂无任务记录。")
    return "\n".join(lines)


async def project_ask(
    project_root: str,
    request: str,
    assignee_hint: str | None = None,
    parent_task_id: str | None = None,
    mode: str | None = None,
) -> str:
    """项目任务唯一入口：先过 Understand 门禁，再决定 builtin/hicode/dsh，写回项目记忆。

    Understand 门禁 (docs/PROJECT_AGENT.md §7)：mode=auto（默认）时先判定能否确信
    实现方案与验收标准——判不定 → 只追问、早退（不建业务副作用、不派工）；判定得了
    → 带着 interpretation/assumptions 走既有执行腿。mode=act_eager 跳过判定；
    mode=ask_only 强制只追问、永不执行。parent_task_id 续答上一轮 need_clarification
    的 task，把上一轮 interpretation/questions 拼进本轮判定。

    终态只有 completed 或 blocked（need_clarification 是 blocked 的一种，语义是
    「等人」而非执行失败）；派工/等待期间的任何异常、dsh 不可用/超时/无 verdict，
    都会被捕获并收敛为 blocked + 原因，不会把裸异常抛回调用方，也不会在 worker
    之间隐式 fallback（避免同一请求被双跑）。
    assignee_hint 是白名单枚举 (None|builtin|hicode|dsh)；其余值直接 blocked。
    """
    store = ProjectStore(project_root)
    store.ensure_layout()
    task_id = _new_task_id()
    mode = mode or os.environ.get("PROJECT_ASK_DEFAULT_MODE", "auto")

    if assignee_hint is not None and assignee_hint not in _VALID_HINTS:
        resp = ProjectAskResponse(
            task_id=task_id,
            status="blocked",
            phase="rejected",
            block_reason=(
                f"unknown assignee_hint {assignee_hint!r}, must be one of {sorted(_VALID_HINTS)}"
            ),
            parent_task_id=parent_task_id,
        )
        _write_back(store, resp, request, assignee_hint or "unknown")
        return _render(resp)

    if mode not in _VALID_MODES:
        resp = ProjectAskResponse(
            task_id=task_id,
            status="blocked",
            phase="rejected",
            block_reason=f"unknown mode {mode!r}, must be one of {sorted(_VALID_MODES)}",
            parent_task_id=parent_task_id,
        )
        _write_back(store, resp, request, "unknown")
        return _render(resp)

    # 1. Understand
    chain: list[dict[str, Any]] = []
    if mode == "act_eager":
        u = eager_act_result(request)
    else:
        memory = _context_prefix(store)
        chain = _load_chain(store, parent_task_id)
        u = await understand(request, memory, chain)
        if mode == "ask_only" and u.decision == "act":
            u = force_confirm_ask(u)

    store.write_understand(
        task_id,
        {
            "task_id": task_id,
            "parent_task_id": parent_task_id,
            "request": request,
            "decision": u.decision,
            "confidence": u.confidence,
            "interpretation": u.interpretation,
            "assumptions": u.assumptions,
            "questions": u.questions,
            "risk_flags": u.risk_flags,
            "reasons": u.reasons,
        },
    )

    if u.decision == "ask":
        resp = ProjectAskResponse(
            task_id=task_id,
            status="blocked",
            phase="understood_ask",
            block_reason="need_clarification",
            interpretation=u.interpretation,
            assumptions=u.assumptions,
            questions=u.questions,
            confidence=u.confidence,
            parent_task_id=parent_task_id,
        )
        _write_back(store, resp, request, "understand")
        # 结构化事件: 主脑执行结果本身不会流给前端 (成功的 tool 只喂回模型上下文,
        # 见 oservi.MasterAgent.chat_stream), 这里额外推一条, 让前端能渲染澄清卡片
        # 而不必靠模型转述原文。fire_step 无监听方时是 no-op (无 SSE 会话时安全)。
        fire_step(
            {
                "type": "project_understand_ask",
                "task_id": resp.task_id,
                "parent_task_id": parent_task_id,
                "interpretation": resp.interpretation,
                "assumptions": resp.assumptions,
                "questions": resp.questions,
                "confidence": resp.confidence,
            }
        )
        return _render(resp)

    # 2. Act — existing builtin/hicode/dsh paths, brief 前置 Understand 摘要
    assignee = _decide_assignee(request, assignee_hint)
    understand_prefix = _understand_prefix(u, request, chain)

    if assignee == "builtin":
        resp = _run_builtin(store, task_id, request)
    elif assignee == "hicode":
        resp = await _run_hicode(store, task_id, project_root, request, understand_prefix)
    else:
        resp = await _run_dsh(store, task_id, project_root, request, understand_prefix)

    resp.phase = "executed"
    resp.interpretation = u.interpretation
    resp.assumptions = u.assumptions
    resp.confidence = u.confidence
    resp.parent_task_id = parent_task_id

    _write_back(store, resp, request, assignee)
    return _render(resp)


# ── 注册: project_ask（派工唯一入口）+ project_status（只读，唯一允许并存的第二入口）──
# 硬约束: 不得再新增其它 tool 让 Coordinator 在派工方式之间做分支路由。


def wire_master_tools() -> int:
    """把 project_ask / project_status 注册进 master_tools (幂等)。"""
    from server.tool_registry import master_tools

    added = 0
    added += _wire_project_ask(master_tools)
    added += _wire_project_status(master_tools)
    return added


def _wire_project_ask(master_tools: Any) -> int:
    if master_tools.has("project_ask"):
        return 0
    master_tools.register(
        "project_ask",
        "项目级任务的唯一入口：先澄清再执行。在指定项目目录内完成一次请求，并把结果写回"
        "项目记忆（.veya-project/PROJECT_STATE.md 等）。默认 mode=auto 会先判定这次请求是否"
        "足够明确：不明确 → 只返回 1-3 个追问、不产生任何业务副作用（不改代码、不跑命令）；"
        "明确 → 才会真正处理，三种处理方式：builtin 只把请求记入 DECISIONS.md，**不执行任何"
        "命令、不改任何代码**；hicode / dsh 才会真正执行代码变更。涉及改代码/跑测试/修 bug/"
        "部署等执行类请求会自动派给 hicode（除非显式 assignee_hint 指定 dsh）；纯记录/更新状态"
        "类请求走 builtin。若上一次调用返回了追问，把用户的回答作为新 request、"
        "parent_task_id 设为上次返回的 task_id 再调一次即可续答。project_root 必须落在 "
        "HICODE_WORKSPACE 内，否则派工会 blocked。终态只有 completed 或 blocked"
        "（等待用户回答追问也算 blocked，原因是 need_clarification，不是执行失败）。",
        {
            "type": "object",
            "properties": {
                "project_root": {
                    "type": "string",
                    "description": "项目根目录绝对路径（须位于 HICODE_WORKSPACE 内，否则派工会 blocked）。",
                },
                "request": {
                    "type": "string",
                    "description": "要完成的项目任务，用自然语言描述目标；续答追问时传用户对追问的回答。",
                },
                "assignee_hint": {
                    "type": "string",
                    "enum": ["builtin", "hicode", "dsh"],
                    "description": (
                        "可选。强制指定处理方式：builtin=只记录不执行任何命令/不改代码，"
                        "hicode=派工执行代码变更（默认执行类请求走这里），"
                        "dsh=派给 dsh 外部 worker 执行（仅显式指定才会用，不参与自动判断）。"
                        "缺省按内容自动在 builtin/hicode 间判断。传其它值会直接 blocked。"
                    ),
                },
                "parent_task_id": {
                    "type": "string",
                    "description": (
                        "可选。续答上一轮追问时，传上一次 project_ask 返回的 task_id，"
                        "本轮判定会看到上一轮的 interpretation/questions。"
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["auto", "act_eager", "ask_only"],
                    "description": (
                        "可选，默认 auto（先澄清再执行）。act_eager=跳过澄清直接执行"
                        "（明确知道要做什么、想省一轮判定时才用，风险自负）；"
                        "ask_only=只澄清、永不执行（用于确认理解，不想真的动手时）。"
                    ),
                },
            },
            "required": ["project_root", "request"],
        },
        project_ask,
        max_result_chars=4000,
    )
    return 1


def _wire_project_status(master_tools: Any) -> int:
    if master_tools.has("project_status"):
        return 0
    master_tools.register(
        "project_status",
        "只读查看某个项目的 .veya-project/ 记忆状态：最近的任务记录（completed/blocked）与"
        "该项目是否已被 project_ask 处理过。不派发任何任务、不执行代码、不修改任何文件——"
        "诊断/展示用，不是 project_ask 的替代品。",
        {
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "项目根目录绝对路径。"},
                "limit": {"type": "integer", "description": "可选。返回最近任务条数，默认 5。"},
            },
            "required": ["project_root"],
        },
        project_status,
        max_result_chars=2000,
    )
    return 1


__all__ = ["project_ask", "project_status", "wire_master_tools"]
