#!/usr/bin/env python3
"""路由 traces 分析 — 从 llm-router.jsonl 识别成本分布与固定流程候选。

行动指南落地 (主线一铺路): 高频低成本成功路线 = "固定流程" 候选
(未来可固化走 RL Post-Training 专用小模型)。

用法: python scripts/analyze_router_traces.py [audit.jsonl] [--top N]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_AUDIT = Path.home() / ".veya" / "audit" / "llm-router.jsonl"


def analyze(path: str, top: int = 10) -> dict:
    audit = Path(path)
    if not audit.exists():
        return {"error": f"审计文件不存在: {audit}", "entries": 0}

    per_route: Counter = Counter()
    per_provider_model: Counter = Counter()
    route_provider: defaultdict = defaultdict(Counter)
    gate_upgrades = 0
    parallel_calls = 0
    total = 0

    for line in audit.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        total += 1
        if "action" in e and e["action"] == "gate_upgrade":
            gate_upgrades += 1
            continue
        route = e.get("route", "?")
        provider = e.get("provider", "?")
        model = e.get("model", "?")
        per_route[route] += 1
        per_provider_model[f"{provider}/{model}"] += 1
        route_provider[route][f"{provider}/{model}"] += 1
        if e.get("parallel"):
            parallel_calls += 1

    # 固定流程候选: 同 route × provider/model 高频 (≥ 5 次) 且占比高
    candidates = []
    for route, pm_counter in route_provider.items():
        for pm, cnt in pm_counter.items():
            if cnt >= 5:
                candidates.append({
                    "route": route, "provider_model": pm, "count": cnt,
                    "share": round(cnt / max(1, per_route[route]), 3),
                })
    candidates.sort(key=lambda c: -c["count"])

    return {
        "entries": total,
        "route_distribution": dict(per_route.most_common(top)),
        "provider_model_top": dict(per_provider_model.most_common(top)),
        "gate_upgrades": gate_upgrades,
        "parallel_calls": parallel_calls,
        "fixed_flow_candidates": candidates[:top],
        "summary": (
            f"总路由 {total} 次 · 闸门升级 {gate_upgrades} 次 · 并行分派 {parallel_calls} 次 · "
            f"固定流程候选 {len(candidates)} 个"
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="路由 traces 分析")
    ap.add_argument("audit", nargs="?", default=str(DEFAULT_AUDIT))
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    result = analyze(args.audit, args.top)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(f"=== 路由 traces 分析: {args.audit} ===")
    print(result["summary"])
    print("\n[按档位分布]")
    for k, v in result["route_distribution"].items():
        print(f"  {k:<10} {v}")
    print("\n[按 provider/model Top]")
    for k, v in result["provider_model_top"].items():
        print(f"  {k:<40} {v}")
    if result["fixed_flow_candidates"]:
        print("\n[固定流程候选 (可固化走专用小模型)]")
        for c in result["fixed_flow_candidates"]:
            print(f"  {c['route']:<10} {c['provider_model']:<40} "
                  f"count={c['count']} share={c['share']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
