"""server.wayfinding_github_tools — GitHub Issues MapStore 的 MasterAgent 工具面。

跟 server/wayfinding_tools.py 并存: 那边是本地事件溯源版 (wayfind_*), 这边
封装 omodul.wayfinding_github —— map = GitHub issue (label wayfinder:map),
ticket = 原生 sub-issue, blocking = 原生 issue dependency。人能直接在
GitHub 网页上看到 frontier (sub-issues 进度条 + blocked 徽标), 不需要额外
工具才能看见地图长什么样, 这正是原版 wayfinder skill 要的"native... 渲染
在 tracker 自己 UI 里"。

同步函数, 不是 async: 内部是 gh CLI 子进程调用 (阻塞 I/O), MasterToolRegistry
对同步 func 会丢进线程池执行 (run_sync_in_daemon_thread) —— 写成 async def
反而会在事件循环里阻塞其他并发任务。
"""

from __future__ import annotations

from omodul import wayfinding_github as wg


def wayfind_gh_chart(repo: str, destination: str, notes: str = "") -> str:
    """在 repo 里开一张新的探路图 (GitHub issue, label wayfinder:map)。"""
    try:
        issue = wg.chart_map(repo, destination, notes=notes)
    except Exception as exc:
        return f"wayfind_gh_chart: {exc}"
    return f"✅ 已开图 #{issue['number']} {issue['url']}\n下一步: wayfind_gh_add_ticket"


def wayfind_gh_add_ticket(
    repo: str, map_number: int, title: str, question: str, ticket_type: str = "task"
) -> str:
    """给探路图加一张 ticket (GitHub 原生 sub-issue)。"""
    try:
        issue = wg.add_ticket(repo, map_number, title, question, ticket_type)
    except Exception as exc:
        return f"wayfind_gh_add_ticket: {exc}"
    return f"✅ 已加 ticket #{issue['number']} {issue['url']}"


def wayfind_gh_wire_blocking(repo: str, from_number: int, to_number: int) -> str:
    """声明 to_number 依赖 from_number 先解决 (GitHub 原生 blocked-by)。"""
    try:
        wg.wire_blocking(repo, from_number, to_number)
    except Exception as exc:
        return f"wayfind_gh_wire_blocking: {exc}"
    return f"✅ 已声明 #{to_number} blocked by #{from_number}"


def wayfind_gh_frontier(repo: str, map_number: int) -> str:
    """看当前能认领的 ticket (open + 未阻塞 + 未认领)。"""
    try:
        tickets = wg.frontier(repo, map_number)
    except Exception as exc:
        return f"wayfind_gh_frontier: {exc}"
    if not tickets:
        return "frontier 为空 (无可认领 ticket)"
    lines = [f"- #{t['number']} {t['title']} [{t['type']}]" for t in tickets]
    return "当前 frontier:\n" + "\n".join(lines)


def wayfind_gh_claim(repo: str, ticket_number: int, login: str | None = None) -> str:
    """认领一张 ticket (assignee = claim, 已被别人认领会失败)。"""
    try:
        r = wg.claim_ticket(repo, ticket_number, login=login)
    except Exception as exc:
        return f"wayfind_gh_claim: {exc}"
    if not r["ok"]:
        who = f" (claimed_by={r['claimed_by']})" if "claimed_by" in r else ""
        return f"claim 失败: {r['reason']}{who}"
    return f"✅ 已认领 #{ticket_number} (claimed_by={r['claimed_by']})"


def wayfind_gh_resolve(
    repo: str, map_number: int, ticket_number: int, resolution: str, gist: str
) -> str:
    """解决一张已认领的 ticket: 评论+关闭 issue, 决策写进地图的 Decisions so far。"""
    try:
        r = wg.resolve_ticket(repo, map_number, ticket_number, resolution=resolution, gist=gist)
    except Exception as exc:
        return f"wayfind_gh_resolve: {exc}"
    if not r["ok"]:
        return f"resolve 失败: {r['reason']}"
    return f"✅ 已解决 #{ticket_number}: {gist}"


def wayfind_gh_rule_out_of_scope(
    repo: str, map_number: int, ticket_number: int, reason: str
) -> str:
    """把一张 ticket 标记为不在本次范围内: 关闭并记入地图的 Out of scope。"""
    try:
        r = wg.rule_out_of_scope(repo, map_number, ticket_number, reason)
    except Exception as exc:
        return f"wayfind_gh_rule_out_of_scope: {exc}"
    if not r["ok"]:
        return f"out_of_scope 失败: {r['reason']}"
    return f"✅ #{ticket_number} 标记为 out_of_scope: {reason}"


def wayfind_gh_add_fog(repo: str, map_number: int, patch: str) -> str:
    """记一块还说不清楚的模糊地带, 之后用 wayfind_gh_graduate_fog 拆成具体 ticket。"""
    try:
        wg.add_fog(repo, map_number, patch)
    except Exception as exc:
        return f"wayfind_gh_add_fog: {exc}"
    return f"✅ 已记录 fog: {patch}"


def wayfind_gh_graduate_fog(
    repo: str, map_number: int, patch: str, new_ticket_titles: list[str]
) -> str:
    """把一块模糊地带拆成具体 ticket (每个标题一张 task 类型 sub-issue)。"""
    try:
        created = wg.graduate_fog(
            repo,
            map_number,
            patch,
            [{"title": t, "question": t, "type": "task"} for t in new_ticket_titles],
        )
    except Exception as exc:
        return f"wayfind_gh_graduate_fog: {exc}"
    nums = ", ".join(f"#{c['number']}" for c in created)
    return f"✅ fog '{patch}' 已拆成 {len(created)} 张 ticket: {nums}"


def wayfind_gh_decisions(repo: str, map_number: int) -> str:
    """列出这张探路图目前已经写下的所有决策 (从地图 issue body 读)。"""
    try:
        decisions = wg.decisions_so_far(repo, map_number)
    except Exception as exc:
        return f"wayfind_gh_decisions: {exc}"
    if not decisions:
        return "还没有已解决的决策"
    return "\n".join(f"- [{d['title']}]({d['link']}): {d['gist']}" for d in decisions)


def wayfind_gh_complete(repo: str, map_number: int) -> str:
    """frontier 和 fog 都清空时关闭地图 issue。没清空会告诉你还没关。"""
    try:
        done = wg.complete_if_clear(repo, map_number)
    except Exception as exc:
        return f"wayfind_gh_complete: {exc}"
    if not done:
        return "还没清空: frontier 或 fog 里还有内容, 用 wayfind_gh_frontier 看剩什么"
    return f"✅ 地图 #{map_number} 已完成并关闭"
