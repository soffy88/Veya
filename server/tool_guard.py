"""server/tool_guard — 主脑工具执行的统一守卫通道 (确定性笼子第一道闸).

主脑一切工具执行 —— 静态能力 (``MasterToolRegistry.execute``) 与动态技能
(``VeyaSkillHub.execute``) —— 收口到单一被守卫通道:

    check(策略链) → 记决策轨迹 (forensic trail) → 放行给物理实现

设计约束 (四原则):
- **功能至上 / 质量为王**: 缺省无策略 = 全放行, 对现有任一工具零行为变化;
  守卫是可加固的接缝, 不是默认拦截。策略自身异常 fail-open (告警但放行),
  绝不因守卫 bug 拖垮工具链。
- **长期主义**: 把「概率智能」的每一次物理动作收敛到一个确定性入口, 后续要接
  `system_gate_check` 强制中间件、blast-radius、因果熔断, 都在此加策略即可, 无需
  再改两处 execute()。
- **体验优先**: 决策轨迹环形缓冲, 支持取证/回放 (谁在何时调了什么工具/被谁拒)。

服务层 (SPEC §8) 资产: 放在 ``server/`` 而非 ``veya/``, 不与 3O 主库单源守卫
(``tests/guardians``) 冲突。
"""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Callable

log = logging.getLogger("veya.tool_guard")

# 策略签名: (tool_name, kwargs, source) -> None/"" 放行 | 非空 str 拒绝原因。
# 可为同步或 async (返回 awaitable); async 策略仅在 acheck() 中被 await。
Policy = Callable[[str, dict, str], "str | None"]


class ToolDenied(Exception):
    """策略拒绝一次工具执行 (笼子闭合)。宿主 execute() 转成 ToolExecutionError 回喂主脑反思。"""


class ToolGuard:
    """工具执行统一守卫: 策略链 + 决策轨迹。缺省全放行。

    每条策略带 ``enforce`` 标记 (缺省 False = observe 观察模式):
    - **observe**: 命中只记 ``observed`` 轨迹, **不拦截** (灰度采样, 零行为变化);
    - **enforce**: 命中记 deny 轨迹并 raise ToolDenied (真正闭合笼子)。
    先上 observe 采样, 确认不误伤再翻 enforce —— 满足「功能至上」的安全灰度。
    """

    def __init__(self, *, trail_max: int = 512) -> None:
        self._policies: list[tuple[str, Policy, bool]] = []
        self._trail: list[dict] = []
        self._trail_max = trail_max

    # ── 策略装配 (宿主启动期注册; 测试可注入/清空) ──────────────────────
    def register_policy(self, name: str, policy: Policy, *, enforce: bool = False) -> None:
        """追加一条守卫策略 (返回非空拒绝原因即命中)。enforce=False 为观察模式。"""
        self._policies.append((name, policy, enforce))

    def clear_policies(self) -> None:
        self._policies.clear()

    @property
    def policy_names(self) -> list[str]:
        return [n for n, _, _ in self._policies]

    def has_policy(self, name: str) -> bool:
        return any(n == name for n, _, _ in self._policies)

    # ── 决策轨迹 (取证/回放) ──────────────────────────────────────────
    def trail(self, limit: int = 50) -> list[dict]:
        """近端决策轨迹 (最新在后)。"""
        return self._trail[-limit:] if limit else list(self._trail)

    def _record(self, entry: dict) -> None:
        self._trail.append(entry)
        if len(self._trail) > self._trail_max:
            del self._trail[: len(self._trail) - self._trail_max]

    def _finalize(self, name: str, source: str, observed: list[dict]) -> None:
        """无 enforce 命中 → 记一条 allow (夹带 observe 命中, 供取证)。"""
        entry = {"ts": time.time(), "tool": name, "source": source, "decision": "allow"}
        if observed:
            entry["observed"] = observed
        self._record(entry)

    def _on_verdict(
        self, name: str, kwargs: dict, source: str, pol_name: str, enforce: bool, verdict: object
    ) -> dict | None:
        """处理一条策略结论。enforce 命中 → 记 deny 并 raise; observe 命中 → 返回观察条目。"""
        if not verdict:
            return None
        if enforce:
            self._record(
                {
                    "ts": time.time(),
                    "tool": name,
                    "source": source,
                    "decision": "deny",
                    "policy": pol_name,
                    "reason": str(verdict),
                }
            )
            raise ToolDenied(f"tool '{name}' denied by policy '{pol_name}': {verdict}")
        return {"policy": pol_name, "reason": str(verdict)}

    # ── 守卫 (执行前调用) ─────────────────────────────────────────────
    def check(self, name: str, kwargs: dict, *, source: str = "master") -> None:
        """同步守卫。async 策略在此被跳过 (fail-open + 告警) —— 请用 acheck。"""
        observed: list[dict] = []
        for pol_name, pol, enforce in self._policies:
            try:
                verdict = pol(name, kwargs, source)
                if inspect.isawaitable(verdict):
                    verdict.close()  # 关闭协程, 避免 "never awaited" 告警
                    log.warning("sync check skipped async policy %r (use acheck)", pol_name)
                    continue
            except Exception as exc:
                log.warning("tool guard policy %r errored on tool %r: %s", pol_name, name, exc)
                continue
            hit = self._on_verdict(name, kwargs, source, pol_name, enforce, verdict)
            if hit:
                observed.append(hit)
        self._finalize(name, source, observed)

    async def acheck(self, name: str, kwargs: dict, *, source: str = "master") -> None:
        """异步守卫: 支持 async 策略 (如 state_kernel 闸门)。语义同 check。"""
        observed: list[dict] = []
        for pol_name, pol, enforce in self._policies:
            try:
                verdict = pol(name, kwargs, source)
                if inspect.isawaitable(verdict):
                    verdict = await verdict
            except Exception as exc:
                log.warning("tool guard policy %r errored on tool %r: %s", pol_name, name, exc)
                continue
            hit = self._on_verdict(name, kwargs, source, pol_name, enforce, verdict)
            if hit:
                observed.append(hit)
        self._finalize(name, source, observed)


# 全局单例: 宿主装配期注册策略; 缺省无策略 = 全放行 (线上零变化)。
global_tool_guard = ToolGuard()
