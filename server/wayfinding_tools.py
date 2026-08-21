"""server.wayfinding_tools — Wayfinding + StatefulProcedure 的 MasterAgent 工具面。

主脑设置零改动 (冻结架构): 只新增能力工具, 模型自主调用。
薄封装 —— 业务逻辑在 ``omodul.wayfinding`` (探路: Map/Ticket/DecisionGist,
事件溯源) 和 ``obase.orchestrator`` (执行: Runbook 图状态机), 这里只是把
结构化返回值格式化成给模型看的字符串, 照抄 ``server/state_kernel.py`` 的
``todo_claim``/``gate_check`` 风格: 成功用 ✅, 失败给可读原因, 不抛异常
往外传 (工具执行失败会被 MasterToolRegistry 当异常回喂反思, 这里的错误更
适合直接读)。

用法顺序: wayfind_chart → wayfind_add_ticket (可选 wayfind_add_fog) →
wayfind_frontier → wayfind_claim → (做调查/原型/沟通) → wayfind_resolve →
... 重复直到 wayfind_complete → wayfind_compile_runbook → stateful_start →
stateful_goto (反复) → stateful_history。

``stateful_*`` 系列不持久化 Runbook 本身 —— 每次都用 map_id 重新跑
``decisions_to_runbook`` (纯函数, 只要 decisions_so_far 没变就是同一个
Runbook/同一个 content_hash), 只有 RunState 落盘。这样不需要另外管理
"这个 run 对应哪份 YAML" 的映射。
"""

from __future__ import annotations

from obase.hierarchical_context import HierarchicalContextStore
from obase.orchestrator import runbook_current, runbook_goto, runbook_history, start_runbook
from obase.runbook_runtime import default_hook_runner, make_default_check_runner
from omodul.wayfinding import (
    WayfindingKernel,
    decisions_to_runbook,
    load_kernel,
    new_map_id,
    wayfinding_store,
)


async def wayfind_chart(destination: str, notes: str = "") -> str:
    """开一张新的探路图 (目标模糊 / 需要先聚焦范围时用, 不要直接开始执行)。

    destination: 1-2 句话说清楚要去哪 (不必是最终方案, 是"往哪个方向探")。
    notes: 领域背景/已知偏好/约束 (可选)。
    返回 map_id —— 后续 wayfind_add_ticket/frontier/claim/resolve 都要传它。
    """
    map_id = new_map_id()
    kernel = WayfindingKernel(wayfinding_store(map_id), map_id=map_id)
    m = await kernel.create_map(destination, notes=notes)
    return f"✅ 已开图 map_id={map_id}\n目的地: {m.destination}\n下一步: wayfind_add_ticket 加待澄清的问题"


async def wayfind_add_ticket(
    map_id: str, title: str, question: str, ticket_type: str = "task"
) -> str:
    """给探路图加一张待澄清问题的 ticket。

    ticket_type: research(可 AFK 自己查) / prototype(要做个東西给人看) /
    grilling(纯对话澄清, 不能替人决定) / task(阻塞性前置工作)。
    """
    try:
        kernel = load_kernel(map_id)
        t = await kernel.add_ticket(title, question, ticket_type)
    except Exception as exc:
        return f"add_ticket: {exc}"
    return f"✅ 已加 ticket {t.id}: {t.title} (type={t.type})"


async def wayfind_wire_blocking(map_id: str, from_ticket: str, to_ticket: str) -> str:
    """声明 to_ticket 依赖 from_ticket 先解决 (from 未关闭前 to 不会出现在 frontier)。"""
    try:
        kernel = load_kernel(map_id)
        await kernel.wire_blocking([(from_ticket, to_ticket)])
    except Exception as exc:
        return f"wire_blocking: {exc}"
    return f"✅ 已声明 {to_ticket} 依赖 {from_ticket}"


async def wayfind_frontier(map_id: str) -> str:
    """看当前能认领的 ticket (open + 未阻塞 + 未认领)。"""
    try:
        kernel = load_kernel(map_id)
    except Exception as exc:
        return f"frontier: {exc}"
    tickets = kernel.frontier()
    if not tickets:
        return "frontier 为空 (无可认领 ticket)"
    lines = [f"- {t.id}: {t.title} [{t.type}] — {t.question}" for t in tickets]
    return "当前 frontier:\n" + "\n".join(lines)


async def wayfind_claim(map_id: str, ticket_id: str, claimed_by: str = "veya") -> str:
    """认领一张 ticket。已被别人认领会失败 (不可抢占), 认领后才能 resolve。"""
    try:
        kernel = load_kernel(map_id)
        r = await kernel.claim_ticket(ticket_id, claimed_by)
    except Exception as exc:
        return f"claim: {exc}"
    if not r["ok"]:
        return f"claim 失败: {r['reason']}"
    return f"✅ 已认领 ticket {ticket_id}"


async def wayfind_resolve(map_id: str, ticket_id: str, resolution: str, gist: str) -> str:
    """解决一张已认领的 ticket, 写下决策要点 (gist 会进 decisions_so_far)。

    resolution: 完整说明 (做了什么/为什么)。gist: 一句话结论, 会出现在
    map.md 和 decisions_to_runbook 编译出的执行节点里。
    """
    try:
        kernel = load_kernel(map_id)
        r = await kernel.resolve_ticket(ticket_id, resolution=resolution, gist=gist)
    except Exception as exc:
        return f"resolve: {exc}"
    if not r["ok"]:
        return f"resolve 失败: {r['reason']}"
    return f"✅ 已解决 ticket {ticket_id}: {gist}"


async def wayfind_rule_out_of_scope(map_id: str, ticket_id: str, reason: str) -> str:
    """把一张 ticket 标记为不在本次范围内: 关闭, 且不会再出现在 frontier。"""
    try:
        kernel = load_kernel(map_id)
        r = await kernel.rule_out_of_scope(ticket_id, reason)
    except Exception as exc:
        return f"out_of_scope: {exc}"
    if not r["ok"]:
        return f"out_of_scope 失败: {r['reason']}"
    return f"✅ ticket {ticket_id} 标记为 out_of_scope: {reason}"


async def wayfind_add_fog(map_id: str, patch: str) -> str:
    """记一块还说不清楚的模糊地带, 之后用 wayfind_graduate_fog 拆成具体 ticket。"""
    try:
        kernel = load_kernel(map_id)
        await kernel.add_fog(patch)
    except Exception as exc:
        return f"add_fog: {exc}"
    return f"✅ 已记录待澄清的模糊地带: {patch}"


async def wayfind_graduate_fog(map_id: str, patch: str, new_ticket_titles: list[str]) -> str:
    """把一块模糊地带拆成具体 ticket (每个标题一张 task 类型 ticket, 问题同标题)。"""
    try:
        kernel = load_kernel(map_id)
        created = await kernel.graduate_fog(
            patch, [{"title": t, "question": t, "type": "task"} for t in new_ticket_titles]
        )
    except Exception as exc:
        return f"graduate_fog: {exc}"
    return f"✅ fog '{patch}' 已拆成 {len(created)} 张 ticket: " + ", ".join(t.id for t in created)


async def wayfind_decisions(map_id: str) -> str:
    """列出这张探路图目前已经写下的所有决策。"""
    try:
        kernel = load_kernel(map_id)
    except Exception as exc:
        return f"decisions: {exc}"
    gists = kernel.decisions_so_far()
    if not gists:
        return "还没有已解决的决策"
    return "\n".join(f"- [{g.title}]({g.link}): {g.gist}" for g in gists)


async def wayfind_complete(map_id: str) -> str:
    """frontier 和 fog 都清空时, 把地图标记为 completed。没清空会告诉你还剩什么。"""
    try:
        kernel = load_kernel(map_id)
        done = await kernel.complete_if_clear()
    except Exception as exc:
        return f"complete: {exc}"
    if not done:
        remaining = len(kernel.frontier())
        fog_left = len(kernel.map.not_yet_specified)
        return f"还没清空: frontier 剩 {remaining} 项, fog 剩 {fog_left} 项"
    return f"✅ 地图 {map_id} 已完成, 下一步: wayfind_compile_runbook"


async def wayfind_compile_runbook(map_id: str) -> str:
    """把已完成探路图的决策编译成 Runbook 预览 (不会自动开始执行)。"""
    try:
        kernel = load_kernel(map_id)
        rb = decisions_to_runbook(kernel.map)
    except Exception as exc:
        return f"compile_runbook: {exc}"
    node_list = "\n".join(f"  {i + 1}. {nid}" for i, nid in enumerate(rb.nodes))
    return (
        f"✅ 已编译 runbook (name={rb.name}, {len(rb.nodes)} 个节点):\n{node_list}\n"
        f"下一步: stateful_start(map_id='{map_id}')"
    )


async def stateful_start(map_id: str, run_id: str | None = None) -> str:
    """从一张已完成的探路图编译 Runbook, 开始 (或续跑) 执行。"""
    try:
        kernel = load_kernel(map_id)
        rb = decisions_to_runbook(kernel.map)
        state = start_runbook(rb, run_id=run_id or f"map-{map_id}")
    except Exception as exc:
        return f"stateful_start: {exc}"
    node = rb.nodes[state.current_node]
    return f"✅ run_id={state.run_id} 当前节点={state.current_node}\n{node.prompt}"


async def stateful_current(map_id: str, run_id: str) -> str:
    """看某次执行当前停在哪个节点, 以及能转去哪些节点。"""
    try:
        kernel = load_kernel(map_id)
        rb = decisions_to_runbook(kernel.map)
        cur = runbook_current(rb, run_id)
    except Exception as exc:
        return f"stateful_current: {exc}"
    return f"节点={cur['node']} 状态={cur['state']}\n{cur['prompt']}\n可转到: {cur['allowed_next']}"


async def stateful_goto(
    map_id: str,
    run_id: str,
    target: str,
    workspace_root: str = ".",
    confirm_items: list[str] | None = None,
) -> str:
    """尝试把执行转移到 target 节点。转移前的 check 不过会留在原节点并给出原因。

    decisions_to_runbook 编译出的每个节点都带一条 checklist check ("Decision
    '<title>' applied / verified") 门住转移 —— 真的把该决策落实了 (改了代码/
    发了消息/等等) 之后, 把这条 checklist 文案原样传进 confirm_items 才能过。
    stateful_current 能看到当前节点的 prompt, 里面就是决策原文。

    走到终态 (completed/blocked) 时会自动把这次执行的 trajectory 投影进
    3o://user/veya/memories/trajectories/<run_id> (HierarchicalContext)。
    """
    try:
        kernel = load_kernel(map_id)
        rb = decisions_to_runbook(kernel.map)
        check_runner = make_default_check_runner(workspace_root=workspace_root)
        agent_context = {"checklist_confirmed": confirm_items or []}
        result = runbook_goto(
            rb,
            run_id,
            target,
            check_runner=check_runner,
            hook_runner=default_hook_runner,
            agent_context=agent_context,
            context_store=HierarchicalContextStore(),
        )
    except Exception as exc:
        return f"stateful_goto: {exc}"
    if not result.get("ok"):
        return f"❌ 未通过: {result.get('reason')}\n{result.get('failed_check', '')}"
    return f"✅ 已转到 {result['to']}\n{result['prompt']}"


async def stateful_history(run_id: str, tail: int = 20) -> str:
    """看某次执行的转移历史 (最近 tail 条)。"""
    try:
        history = runbook_history(run_id, tail=tail)
    except Exception as exc:
        return f"stateful_history: {exc}"
    if not history:
        return "暂无转移历史"
    return "\n".join(f"{h['from']} -> {h['to']}" for h in history)
