"""Veya Firewall: 外部不可信数据的语义隔离与提示词注入防御舱(薄适配层)。

3O 单一来源 (§1.4): 本体已固化为主库 oskill.adversarial_firewall.AdversarialFirewall
(基于 oprim._injection_scan 特征码扫描 + 隔离舱包裹)。
本层保留脚手架 API: VeyaFirewall.sanitize(raw_content, source)。
"""

from __future__ import annotations

from typing import Any

from veya.platform import oprim as _load_oprim
from veya.platform import oskill as _load_oskill

_oprim = _load_oprim()
_oskill = _load_oskill()


class VeyaFirewall:
    """外部数据防火墙: 静态特征码 + 隔离舱双重过滤(委托主库技能)。"""

    # 经典提示词注入攻击特征码(主库同源)
    INJECTION_SIGNATURES = _oprim._injection_scan.INJECTION_SIGNATURES

    def __init__(self, **kwargs: Any):
        self._skill = _oskill.adversarial_firewall.AdversarialFirewall(**kwargs)

    @classmethod
    def sanitize(cls, raw_content: str, source: str = "external_web") -> dict[str, Any]:
        """过滤并格式化外部文本(类方法兼容脚手架调用方式)。"""
        return cls()._skill.sanitize(raw_content, source=source)
