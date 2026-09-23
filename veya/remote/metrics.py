"""Lightweight per-tool latency instrumentation for the remote direct path (P0-M).

In-memory, bounded, thread-safe. Answers "what is workspace_info p50/p95 / shell
submit p50/p95 / process_status p50/p95" without adding any MCP surface.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any

_MAX_SAMPLES = 2000


class LatencyMetrics:
    def __init__(self, *, max_samples: int = _MAX_SAMPLES) -> None:
        self._samples: dict[str, deque[float]] = {}
        self._counts: dict[str, dict[str, int]] = {}
        self._lock = threading.Lock()
        self._max = max(1, int(max_samples))

    def record(self, tool: str, *, mode: str, duration_ms: float, ok: bool) -> None:
        with self._lock:
            samples = self._samples.setdefault(tool, deque(maxlen=self._max))
            samples.append(float(duration_ms))
            counts = self._counts.setdefault(tool, {"total": 0, "sync": 0, "async": 0, "error": 0})
            counts["total"] += 1
            counts[mode if mode in {"sync", "async"} else "sync"] += 1
            if not ok:
                counts["error"] += 1

    @staticmethod
    def _percentile(sorted_samples: list[float], fraction: float) -> float:
        if not sorted_samples:
            return 0.0
        index = min(len(sorted_samples) - 1, round(fraction * (len(sorted_samples) - 1)))
        return round(sorted_samples[index], 3)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            data = {tool: list(samples) for tool, samples in self._samples.items()}
            counts = {tool: dict(c) for tool, c in self._counts.items()}
        out: dict[str, Any] = {}
        for tool, samples in data.items():
            ordered = sorted(samples)
            out[tool] = {
                "count": len(ordered),
                "p50_ms": self._percentile(ordered, 0.50),
                "p95_ms": self._percentile(ordered, 0.95),
                "max_ms": round(ordered[-1], 3) if ordered else 0.0,
                **counts.get(tool, {}),
            }
        return out

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self._counts.clear()


__all__ = ["LatencyMetrics"]
