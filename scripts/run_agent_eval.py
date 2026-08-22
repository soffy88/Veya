#!/usr/bin/env python3
"""独立 Eval 夹具 CLI(对标"Pi"清单 P2, 见 memory project_veya_pi_gap_audit)。

跑真实 `MasterCoordinator.chat_stream()`(真实 LLM, 需要环境变量里配好 key)
过 `server/agent_eval.py::AGENT_EVAL_CASES`, 用 `oskill.eval_suite` 打分/汇总。

用法:
    # 直接跑, 打印汇总
    python scripts/run_agent_eval.py

    # 跑完存成基线, 供以后对比
    python scripts/run_agent_eval.py --save baseline.json

    # 跑一遍候选(比如切了模型/改了 system prompt), 跟基线对比显著性
    python scripts/run_agent_eval.py --compare-against baseline.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from server.agent_eval import (  # noqa: E402
    compare_runs,
    eval_run_from_dict,
    eval_run_to_dict,
    run_agent_eval_suite,
)
from server.coordinator_master import MasterCoordinator  # noqa: E402


async def _main(args: argparse.Namespace) -> int:
    coord = MasterCoordinator(max_rounds=args.max_rounds)
    run = await run_agent_eval_suite(coord, suite_name=args.suite_name)
    payload = eval_run_to_dict(run)
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))

    if args.save:
        Path(args.save).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print(f"已存基线: {args.save}", file=sys.stderr)

    if args.compare_against:
        baseline_data = json.loads(Path(args.compare_against).read_text(encoding="utf-8"))
        baseline = eval_run_from_dict(baseline_data)
        report = compare_runs(baseline, run)
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=str))
        if not report.candidate_wins and report.t_test.get("mean_diff", 0.0) < 0:
            print("警告: 本次结果显著弱于基线", file=sys.stderr)
            return 1

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--suite-name", default="agent_eval")
    parser.add_argument("--max-rounds", type=int, default=4)
    parser.add_argument("--save", help="跑完把结果存成 JSON(供以后 --compare-against 用)")
    parser.add_argument("--compare-against", help="跟一份已存的基线 JSON 做统计显著性对比")
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
