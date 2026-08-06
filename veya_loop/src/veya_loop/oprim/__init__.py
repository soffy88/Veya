"""veya_loop.oprim — 3O oprim 主库元素的装配面 (单一来源, 只转发不实现)。

使用说明 (性能调试):
    from veya_loop.oprim._inference_cache import get_intervention_cache
    get_intervention_cache().stats()   # → {"hits": N, "misses": M, "hit_rate": ...}
"""

from .._assembly import oprim as _load_oprim

_oprim = _load_oprim()
