#!/usr/bin/env python3
"""Phase 1 真实 proposer：在 2 个 Veya 任务上跑 k=1 parallel 全循环。

与 Phase 0 的差别只有一处：proposer 从 ``VeyaDemoProposer``（无模型调用）换成
``VeyaProposer``（走 Veya 现有 provider 配置的真实模型）。默认关闭：

    VEYA_HARNESS2_REAL_PROPOSER=1 python3 scripts/run_harness2_phase1.py

``--dry-run`` 在 flag 开启的前提下只解析通道、导出 base harness，不发任何模型
请求，用来确认"模型选对了、凭据可解析"再花钱。

产出到 ``~/.veya/harness/``（可用 ``--root`` 覆盖）：

    ~/.veya/harness/base/                  只读导出的 Veya 组件树
    ~/.veya/harness/snapshot/              完整只读快照
    ~/.veya/harness/runs/<ns>/<task>/      harness2 证据树、summary.json、selection
    ~/.veya/harness/phase1-k1.json         本轮报告（含 proposer 模型与成本）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.harness._harness2 import import_harness2_core
from server.harness.adapter import VeyaBenchmarkAdapter
from server.harness.channel import candidate_channels, select_channel
from server.harness.proposer import (
    FLAG_NAME,
    ProposerDisabled,
    ProposerError,
    VeyaProposer,
    proposer_enabled,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.home() / ".veya" / "harness",
        help="harness root (default: ~/.veya/harness)",
    )
    parser.add_argument("--k", type=int, default=1)
    parser.add_argument("--mode", choices=("agg", "seq", "both"), default="agg")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve the channel and export the base harness without any model call",
    )
    parser.add_argument(
        "--audit-dir",
        type=Path,
        default=None,
        help="proposer audit directory (default: <root>/proposer-audit)",
    )
    args = parser.parse_args(argv)
    if args.k < 1:
        parser.error("--k must be positive")

    root = args.root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    modes = ("agg", "seq") if args.mode == "both" else (args.mode,)

    if not proposer_enabled():
        print(
            f"[veya-phase1] {FLAG_NAME} is OFF — refusing to run a real proposer.\n"
            f"[veya-phase1] set {FLAG_NAME}=1 to opt in, or use "
            f"scripts/run_harness2_phase0.py for the no-model path."
        )
        return 2

    adapter = VeyaBenchmarkAdapter(root)
    channel, records = select_channel()
    for line in records:
        print(f"[veya-phase1] candidate: {line}")
    if channel is None:
        print("[veya-phase1] refusing to run: no usable proposer channel")
        return 2
    print(f"[veya-phase1] proposer channel: {channel.label} (source: {channel.source})")

    if args.dry_run:
        report = {
            "dry_run": True,
            "flag": FLAG_NAME,
            "proposer_provider": channel.provider,
            "proposer_model": channel.model,
            "proposer_source": channel.source,
            "candidates": [c.label for c in candidate_channels()],
            "channel_records": records,
            "base_harness": str(adapter.base_harness()),
            "tasks": adapter.tasks(),
            "modes": list(modes),
            "k": args.k,
        }
        path = root / "phase1-dry-run.json"
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"[veya-phase1] dry run ok; report: {path}")
        return 0

    core = import_harness2_core()
    try:
        proposer = VeyaProposer(
            adapter.spec.channels["proposer"],
            audit_dir=args.audit_dir or root / "proposer-audit",
        )
    except (ProposerDisabled, ProposerError) as exc:
        print(f"[veya-phase1] proposer unavailable: {exc}")
        return 2

    result = core.schedule.run_domain(
        "veya-phase1",
        adapter.tasks(),
        args.k,
        adapter=adapter,
        prop=proposer,
        runs_root=adapter.spec.runs_root,
        library_root=root / "library" / f"k{args.k}",
        modes=modes,
    )
    result["execution_count"] = adapter.execution_count
    result["proposer_calls"] = len(proposer._calls)
    result["proposer_usd"] = round(proposer.total_usd, 6)
    result["proposer_tool_calls"] = proposer.total_tool_calls
    result["proposer_provider"] = proposer.channel.provider
    result["proposer_model"] = proposer.channel.model
    result["proposer_source"] = proposer.channel.source
    result["proposer_channel_records"] = list(getattr(proposer, "channel_records", []))
    result["flag"] = FLAG_NAME
    result["library_written"] = False  # experience=NO_ENTRY, same as Phase 0

    report = root / f"phase1-k{args.k}.json"
    report.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        f"Phase 1: {result['done']} done, {result['failed']} failed; "
        f"executions={adapter.execution_count}, proposer_calls={len(proposer._calls)}, "
        f"proposer_usd={result['proposer_usd']}"
    )
    print(f"report: {report}")
    return int(result["failed"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
