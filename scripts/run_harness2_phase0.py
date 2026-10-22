#!/usr/bin/env python3
"""Phase 0 流程验证：用 harness2 DemoProposer 在 2 个 Veya 任务上跑 k=1 全循环。

默认产出到 ``~/.veya/harness/``：

    ~/.veya/harness/base/                 只读导出的 Veya 组件树
    ~/.veya/harness/runs/<ns>/<task>/      harness2 证据树、summary.json、selection
    ~/.veya/harness/library/k1/            Phase 0 不写（experience=NO_ENTRY）

全程无模型调用：``VeyaDemoProposer`` 是脚本化 proposer，``VeyaBenchmarkAdapter``
用注入式 scripted LLM + fixture 工具面跑 Veya 的 AgentLoop 心脏。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.harness._harness2 import import_harness2_core
from server.harness.adapter import VeyaBenchmarkAdapter, VeyaDemoProposer


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
    args = parser.parse_args(argv)
    if args.k < 1:
        parser.error("--k must be positive")

    root = args.root.expanduser().resolve()
    adapter = VeyaBenchmarkAdapter(root)
    proposer = VeyaDemoProposer(adapter.spec.channels["proposer"])
    core = import_harness2_core()
    modes = ("agg", "seq") if args.mode == "both" else (args.mode,)
    result = core.schedule.run_domain(
        "veya-phase0",
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
    report = root / f"phase0-k{args.k}.json"
    report.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        f"Phase 0: {result['done']} done, {result['failed']} failed; "
        f"executions={adapter.execution_count}, proposer_calls={len(proposer._calls)}"
    )
    print(f"report: {report}")
    return int(result["failed"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
