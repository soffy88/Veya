"""Veya Observer — 沙箱推演引擎(薄适配层)。

3O 单一来源 (§1.4): 管线本体已固化为主库 omodul.observer.run_observer_lookahead
(组合 oprim._actions 可逆性闸门 + _snapshot 快照 + _sandbox 预热池 +
_reward 稠密探针 + _lookahead 并行 rollout)。
本层保留脚手架 API: VeyaObserver.lookahead(plans, base_dir, ...)。

沙箱隔离: 默认 unshare -Urn(user + network namespace, 切断网络) +
环境变量全清空(API key 不泄入) + PYTHONHASHSEED=0/TZ=UTC 冻结不确定性。
"""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from server.notification_center import global_notifier
from veya.platform import omodul as _load_omodul
from veya.platform import oprim as _load_oprim

_omodul = _load_omodul()
_oprim = _load_oprim()


class VeyaObserver:
    """观察者: 破坏性动作落地前, 先在隔离沙箱里真跑一遍再打分。"""

    def __init__(self, snapshot_dir: str | Path | None = None):
        self.snapshot_dir = Path(snapshot_dir or tempfile.mkdtemp(prefix="veya-obs-")).expanduser()
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)

    async def lookahead(
        self,
        plans: Sequence[dict[str, Any]],
        base_dir: str | Path,
        probes: Sequence[Any] | None = None,
        *,
        min_reward: float = 0.999,
        stability_check: bool = False,
        max_parallel: int = 4,
        divergences: Sequence[dict[str, str]] = (),
        notify: bool = True,
    ) -> dict[str, Any]:
        """对候选方案做单步推演。

        plans: [{"id", "actions": [{"id", "kind", "payload",
                "reversibility"?, "compensation"?}], "prior"?, "rationale"?}]
        probes: 缺省 = 语法 gate + unittest 通过率 + diff 体积(宿主侧基线)。
        divergences: [{"kind", "detail", "severity"}] 沙箱与生产的已知差异(调用方声明)。
        """
        base_dir = Path(base_dir)
        store = _oprim.SnapshotStore(str(self.snapshot_dir))
        pool = _oprim.SandboxPool(size=max(1, min(max_parallel, 4)),
                                  base_dir=str(self.snapshot_dir / "pool"),
                                  isolation="netns")

        action_plans = []
        for p in plans:
            acts = []
            for a in p.get("actions", []):
                rev = a.get("reversibility", "reversible")
                comp = a.get("compensation")
                acts.append(_oprim.Action(
                    id=a["id"], kind=a["kind"], payload=a.get("payload", {}),
                    reversibility=_oprim.Reversibility(rev),
                    compensation=_oprim.Action(**comp) if comp else None,
                    description=a.get("description", ""),
                ))
            action_plans.append(_oprim.ActionPlan(
                id=p["id"], actions=acts,
                prior=float(p.get("prior", 1.0)),
                rationale=p.get("rationale", ""),
            ))

        # 缺省探针链: 冻结基线哈希(宿主侧) + 语法门 + unittest + diff 体积
        if probes is None:
            baseline: dict[str, str] = {}
            frozen: dict[str, str] = {}
            for f in base_dir.rglob("*.py"):
                rel = str(f.relative_to(base_dir))
                content = f.read_text(encoding="utf-8", errors="replace")
                baseline[rel] = content
                if rel.startswith("test_"):
                    frozen[rel] = hashlib.sha256(content.encode()).hexdigest()
            probes = [
                _oprim.py_syntax_gate([str(f.relative_to(base_dir))
                                       for f in base_dir.rglob("*.py")][:8]),
                _oprim.FileFrozenProbe(frozen) if frozen else None,
                _oprim.unittest_probe(weight=3.0),
                _oprim.DiffSizeProbe(baseline, weight=1.0),
            ]
            probes = [p for p in probes if p is not None]

        dv = [_oprim.Divergence(**d) for d in divergences]
        cfg = _omodul.ObserverConfig(min_reward=min_reward,
                                     stability_check=stability_check,
                                     max_parallel=max_parallel)

        with pool:
            verdict = _omodul.run_observer_lookahead(
                action_plans, str(base_dir), store, pool, probes,
                config=cfg, divergences=dv,
            )

        out: dict[str, Any] = {
            "decision_id": verdict.decision_id,
            "ok": verdict.ok,
            "base_digest": verdict.base_digest,
            "summary": verdict.summary(),
            "ranked": [
                {"plan_id": r.plan_id, "reward": r.reward, "gated": r.gated,
                 "gate_failed": r.gate_failed, "stable": r.stable,
                 "result_digest": r.result_digest, "elapsed_ms": r.elapsed_ms,
                 "error": r.error}
                for r in verdict.ranked
            ],
            "escalations": [e.__dict__ for e in verdict.escalations],
            "divergences": [d.__dict__ for d in verdict.divergences],
            "budget": verdict.budget,
        }
        if notify:
            self._notify(verdict, out)
        return out

    def _notify(self, verdict, out: dict[str, Any]) -> None:
        """推演结束 → 悬浮窗播报(SUCCESS 或 HITL 升级)。"""
        try:
            if verdict.chosen is not None:
                global_notifier.push(
                    "SUCCESS",
                    "✅ 沙箱推演通过",
                    f"选中 {verdict.chosen.plan_id}, reward={verdict.chosen.reward:.3f}",
                    {"decision_id": verdict.decision_id},
                )
            else:
                global_notifier.push(
                    "HITL_REQUIRED",
                    "⚠️ 推演未达置信阈值",
                    f"{verdict.summary()} 请人工裁决",
                    {"decision_id": verdict.decision_id,
                     "escalations": out["escalations"]},
                )
        except Exception:  # pragma: no cover - 通知失败不阻断推演结果
            pass
