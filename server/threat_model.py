"""Veya ThreatModel — 威胁模型闭环演化(薄适配层)。

3O 单一来源 (§1.4): 事务本体已固化为主库 omodul.threat_model_evolve
(复用 oskill.BayesianBeliefUpdater 贝叶斯 ToM + 蜜罐信号似然表 + 隔离决策)。
本层保留脚手架 API: VeyaThreatModel.evolve(signals, ...)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from veya.platform import omodul as _load_omodul

_omodul = _load_omodul()


class VeyaThreatModel:
    """威胁画像演化: 蜜罐敌对信号 → Bayesian ToM 后验 → 隔离决策 → 持久化。"""

    def __init__(self, output_dir: str | Path = "~/.veya/threat_model"):
        self.output_dir = Path(output_dir).expanduser()

    async def evolve(
        self,
        signals: list[dict[str, Any]],
        *,
        prior: list[float] | None = None,
        quarantine_threshold: float = 0.7,
        profile_path: str | None = None,
        entity: str = "default",
    ) -> dict[str, Any]:
        """对一串蜜罐/遥测信号执行贝叶斯更新。

        signals: [{"kind": "probe|credential_stuffing|...", "severity": 0.0~1.0}]。
        profile_path: 持久化画像路径; 传了会先读旧后验作为本次先验(威胁记忆),
                      并写回新后验。缺省写到 output_dir/threat_profile.json。

        Returns:
            {status: quarantined|monitoring, posterior, hostile_prob,
             quarantined, signal_trail, profile_path, ...}
        """
        inp = _omodul.ThreatModelInput(
            signals, prior=prior, profile_path=profile_path, entity=entity
        )
        cfg = _omodul.ThreatModelConfig(quarantine_threshold=quarantine_threshold)
        return await _omodul.threat_model_evolve(cfg, inp, self.output_dir)
