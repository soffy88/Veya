"""Fleet Observability and Metrics (spec §54).

Invariants:
- Tracks queue latency, placement latency, recovery latency, and saturation.
- Computes exact p50, p95, p99 percentiles, not just simple averages.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


def calculate_percentile(values: list[float], percentile: float) -> float:
    """Calculate exact percentile (e.g. 50, 95, 99) from sample values."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * (percentile / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    d0 = sorted_vals[int(f)] * (c - k)
    d1 = sorted_vals[int(c)] * (k - f)
    return d0 + d1


@dataclass
class MetricSummary:
    count: int
    min_val: float
    max_val: float
    avg_val: float
    p50: float
    p95: float
    p99: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "min": round(self.min_val, 4),
            "max": round(self.max_val, 4),
            "avg": round(self.avg_val, 4),
            "p50": round(self.p50, 4),
            "p95": round(self.p95, 4),
            "p99": round(self.p99, 4),
        }


class FleetMetrics:
    """Collects and summarizes operational latency and capacity metrics."""

    def __init__(self) -> None:
        self.queue_latencies_ms: list[float] = []
        self.placement_latencies_ms: list[float] = []
        self.recovery_latencies_ms: list[float] = []

    def record_queue_latency(self, latency_ms: float) -> None:
        self.queue_latencies_ms.append(max(0.0, latency_ms))

    def record_placement_latency(self, latency_ms: float) -> None:
        self.placement_latencies_ms.append(max(0.0, latency_ms))

    def record_recovery_latency(self, latency_ms: float) -> None:
        self.recovery_latencies_ms.append(max(0.0, latency_ms))

    def summarize(self, values: list[float]) -> MetricSummary:
        if not values:
            return MetricSummary(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        return MetricSummary(
            count=len(values),
            min_val=min(values),
            max_val=max(values),
            avg_val=sum(values) / len(values),
            p50=calculate_percentile(values, 50.0),
            p95=calculate_percentile(values, 95.0),
            p99=calculate_percentile(values, 99.0),
        )

    def get_summary(self) -> dict[str, Any]:
        return {
            "queue_latency_ms": self.summarize(self.queue_latencies_ms).to_dict(),
            "placement_latency_ms": self.summarize(self.placement_latencies_ms).to_dict(),
            "recovery_latency_ms": self.summarize(self.recovery_latencies_ms).to_dict(),
        }
