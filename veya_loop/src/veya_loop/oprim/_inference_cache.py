"""veya_loop.oprim._inference_cache — 进程级推理缓存 (单一来源转发)。

本体在主库 oprim._inference_cache; 本模块只 re-export,
保证文档里的 import 路径可用:
    from veya_loop.oprim._inference_cache import get_intervention_cache
"""

from .._assembly import oprim as _load_oprim

_oprim = _load_oprim()

InferenceCache = _oprim._inference_cache.InferenceCache
get_intervention_cache = _oprim._inference_cache.get_intervention_cache
set_intervention_cache_capacity = _oprim._inference_cache.set_intervention_cache_capacity
graph_fingerprint = _oprim._inference_cache.graph_fingerprint
count_simple_paths_dag = _oprim._inference_cache.count_simple_paths_dag
path_frequency_counts = _oprim._inference_cache.path_frequency_counts

__all__ = ["InferenceCache", "count_simple_paths_dag", "get_intervention_cache",
           "graph_fingerprint", "path_frequency_counts",
           "set_intervention_cache_capacity"]
