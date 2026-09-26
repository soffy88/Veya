"""Event/metric collector + failure-trace preservation.

Every harness event is appended to ``events.jsonl`` (one JSON object per
line, with a monotonic sequence number and wall-clock timestamp) and mirrored
into in-memory metric counters.  On any failure the orchestrator calls
``write_failure_trace`` so the full context (traceback, metrics, event tail)
is preserved to disk even when assertions never run.
"""

from __future__ import annotations

import json
import time
import traceback
from pathlib import Path


class EventCollector:
    def __init__(self, run_dir: str | Path):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.run_dir / "events.jsonl"
        self.metrics_path = self.run_dir / "metrics.json"
        self._seq = 0
        self._started = time.time()
        self.metrics: dict = {
            "work_cycles": 0,
            "tool_executions": 0,
            "context_appends": 0,
            "compactions": 0,
            "provider_calls": 0,
            "provider_failovers": 0,
            "replans": 0,
            "checkpoints": 0,
            "restores": 0,
            "side_effect_commits": 0,
            "side_effect_resume_hits": 0,
            "duplicate_side_effects": 0,
            "verifier_runs": 0,
            "verifier_fails": 0,
            "verifier_passes": 0,
            "scenario_events": {},
        }

    def emit(self, event_type: str, **data) -> dict:
        self._seq += 1
        event = {
            "seq": self._seq,
            "t": round(time.time() - self._started, 3),
            "type": event_type,
            **data,
        }
        with self.events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        return event

    def count(self, metric: str, delta: int = 1) -> int:
        self.metrics[metric] = int(self.metrics.get(metric, 0)) + delta
        return self.metrics[metric]

    def scenario(self, name: str, status: str, **data) -> dict:
        slot = self.metrics.setdefault("scenario_events", {}).setdefault(name, {})
        slot["status"] = status
        slot.update(data)
        return self.emit("scenario", scenario=name, status=status, **data)

    def flush_metrics(self) -> Path:
        snapshot = {
            "elapsed_s": round(time.time() - self._started, 3),
            "events": self._seq,
            **self.metrics,
        }
        self.metrics_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        return self.metrics_path

    def elapsed(self) -> float:
        return time.time() - self._started

    def event_tail(self, n: int = 200) -> list:
        try:
            lines = self.events_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines[-n:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def write_failure_trace(self, exc: BaseException, *, phase: str) -> Path:
        """Preserve the failure context; never raises."""
        try:
            path = self.run_dir / "failure_trace.json"
            payload = {
                "phase": phase,
                "error_class": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exception(exc),
                "elapsed_s": round(self.elapsed(), 3),
                "metrics": self.metrics,
                "event_tail": self.event_tail(),
            }
            path.write_text(json.dumps(payload, indent=2)[:2_000_000], encoding="utf-8")
            return path
        except Exception:
            fallback = self.run_dir / "failure_trace.txt"
            fallback.write_text(f"{phase}: {exc!r}", encoding="utf-8")
            return fallback
