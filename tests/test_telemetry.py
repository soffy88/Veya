"""P3-04 Telemetry v1 测试 (docs/VEYA_P1_P3_IMPLEMENTATION_SPEC.md §31)。

验证: trace_id 贯穿全链路; replay; export jsonl/otlp; 与 AuditEmitter 兼容。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.telemetry import TelemetryAPI, TelemetryEmitter, TelemetrySink


def _make_emitter(tmp_path: Path, trace_id: str = "test_trace_123") -> TelemetryEmitter:
    """创建一个指向临时目录的 emitter。"""
    emitter = TelemetryEmitter(trace_id=trace_id)
    emitter.sink.path = tmp_path / "telemetry.jsonl"
    return emitter


# ── TelemetryEmitter ──────────────────────────────────────────────────────


def test_emitter_writes_audit_events(tmp_path):
    emitter = _make_emitter(tmp_path)
    emitter.diagnose(inputs={"symptom": "slow"})
    emitter.plan(inputs={"steps": 3})
    emitter.decide(decision={"action": "optimize"})
    emitter.execute(execution={"tool": "bash", "status": "success"})
    emitter.learn(learning={"improved": True})

    events = emitter.replay()
    assert len(events) == 5
    assert all(e["trace_id"] == "test_trace_123" for e in events)
    assert [e["event_type"] for e in events] == [
        "diagnose", "plan", "decide", "execute", "learn"
    ]


def test_emitter_invalid_event_type_raises(tmp_path):
    emitter = _make_emitter(tmp_path)
    with pytest.raises(ValueError):
        emitter.emit("invalid_type")


def test_emitter_export_jsonl(tmp_path):
    emitter = _make_emitter(tmp_path)
    emitter.diagnose(inputs={"symptom": "slow"})
    emitter.execute(execution={"tool": "bash"})

    out_path = tmp_path / "export.jsonl"
    n = emitter.export_jsonl(out_path)
    assert n == 2
    assert out_path.exists()
    lines = out_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    for line in lines:
        data = json.loads(line)
        assert data["trace_id"] == "test_trace_123"


def test_emitter_export_otlp(tmp_path):
    emitter = _make_emitter(tmp_path)
    emitter.decide(decision={"action": "optimize"})

    out_path = tmp_path / "export.otlp"
    n = emitter.export_otlp(out_path)
    assert n == 1
    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert "resourceSpans" in data
    spans = data["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(spans) == 1
    assert spans[0]["name"] == "decide"


# ── TelemetryAPI ──────────────────────────────────────────────────────────


def test_api_traces_list(tmp_path):
    emitter = _make_emitter(tmp_path, "trace_a")
    emitter.diagnose(inputs={"symptom": "slow"})
    emitter = _make_emitter(tmp_path, "trace_b")
    emitter.execute(execution={"tool": "bash"})

    api = TelemetryAPI(tmp_path)
    result = api.traces(limit=10)
    assert result["total_events"] == 2
    assert len(result["traces"]) == 2
    trace_ids = {t["trace_id"] for t in result["traces"]}
    assert trace_ids == {"trace_a", "trace_b"}


def test_api_replay(tmp_path):
    emitter = _make_emitter(tmp_path, "replay_test")
    emitter.plan(inputs={"steps": 2})
    emitter.execute(execution={"tool": "bash"})

    api = TelemetryAPI(tmp_path)
    result = api.replay("replay_test")
    assert result["event_count"] == 2
    assert result["trace_id"] == "replay_test"


def test_api_replay_not_found(tmp_path):
    api = TelemetryAPI(tmp_path)
    result = api.replay("nonexistent_trace")
    assert result["event_count"] == 0


def test_api_export_jsonl(tmp_path):
    emitter = _make_emitter(tmp_path, "export_test")
    emitter.decide(decision={"action": "optimize"})

    api = TelemetryAPI(tmp_path)
    result = api.export_trace("export_test", fmt="jsonl")
    assert result["status"] == "exported"
    assert result["events"] == 1
    assert Path(result["path"]).exists()


def test_api_export_otlp(tmp_path):
    emitter = _make_emitter(tmp_path, "otlp_test")
    emitter.execute(execution={"tool": "bash"})

    api = TelemetryAPI(tmp_path)
    result = api.export_trace("otlp_test", fmt="otlp")
    assert result["status"] == "exported"
    assert result["events"] == 1
    data = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    assert "resourceSpans" in data


def test_api_export_unsupported_format(tmp_path):
    """不支持的导出格式应返回 error 状态 (不静默成功)。"""
    # 先写入一个 trace, 这样 export_trace 会先通过 not_found 检查, 再到达 fmt 检查
    from server.telemetry import TelemetryEmitter
    emitter = TelemetryEmitter(trace_id="fmt_test")
    emitter.sink.path = tmp_path / "telemetry.jsonl"
    emitter.diagnose(inputs={"symptom": "fmt_check"})

    api = TelemetryAPI(tmp_path)
    result = api.export_trace("fmt_test", fmt="unsupported")
    assert result["status"] == "error"
    assert "unsupported" in result["error"]


def test_api_metrics_has_null_rates_without_samples(tmp_path, monkeypatch):
    from server.events import event_store

    monkeypatch.setattr(event_store, "path", tmp_path / "events.jsonl")
    monkeypatch.setattr(event_store, "_known_event_ids", None)
    api = TelemetryAPI(tmp_path)
    metrics = api.metrics()
    assert metrics["task_success_rate"] is None
    assert metrics["resume_success_rate"] is None
    assert metrics["event_count"] == 0


# ── TelemetrySink ─────────────────────────────────────────────────────────


def test_sink_persistence_across_instances(tmp_path):
    sink1 = TelemetrySink(tmp_path / "test.jsonl")
    emitter = TelemetryEmitter(trace_id="persist_test")
    emitter.sink = sink1
    emitter.diagnose(inputs={"symptom": "slow"})

    # New sink reads same file
    sink2 = TelemetrySink(tmp_path / "test.jsonl")
    events = sink2.read_trace("persist_test")
    assert len(events) == 1
    assert events[0]["event_type"] == "diagnose"
