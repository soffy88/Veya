"""veya_loop.oprim._coupling_flow — 条件仿射耦合机制 (单一来源转发)。"""

from .._assembly import oprim as _load_oprim

_oprim = _load_oprim()

ConditionalCouplingMechanism = _oprim._coupling_flow.ConditionalCouplingMechanism
CouplingLayer = _oprim._coupling_flow.CouplingLayer

__all__ = ["ConditionalCouplingMechanism", "CouplingLayer"]
