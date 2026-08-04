"""
layer4/cli/headless.py — headless 模式(无回帖,机器对机器)

结构化进 → 结构化出,无 TTY,无人类可读渲染。
协调器/分队的机器对机器接口 —— hicode 区别于聊天 Claude 的核心:
分队吃结构化命令、吐结构化结果(给协调器消费),省掉 "写成人类可读回帖" 那层。

同一套引擎,两种出口:
- 交互模式(cli/main.py、tui):结果渲染给人看
- headless(本文件):吐 JSON 给协调器/上游消费

用法:
    echo '{"text": "把 foo.py 的 X 改成 Y"}' | python -m cli.headless
    python -m cli.headless --input cmd.json --output result.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from config.loader import load_config
from server.assembly import Infra
from server.coordinator import coordinator

# =====================================================================
# headless 核心
# =====================================================================


async def headless_run(command: dict[str, Any]) -> dict[str, Any]:
    """结构化命令 → 结构化结果。无渲染。

    返回结构(协调器/上游消费):
        {
          "status": "success" | "failed",
          "error": dict | None,
          "output": Any,              # 结构化产出(diff/findings/...)
          "cost_usd": float,
          "squads": [...],            # 各分队结果(协调器模式)
        }
    """
    result = await coordinator.handle(command)
    return {
        "status": result["status"],
        "error": result.get("error"),
        "output": _extract_output(result),
        "cost_usd": result.get("cost_usd", 0.0),
        "squads": result.get("squads", []),
    }


def _extract_output(result: dict) -> Any:
    """从协调结果提取主产出(execute 分队的 output 优先)。"""
    squads = result.get("squads", [])
    for s in squads:
        if s["role"] == "execute" and s["status"] == "success":
            return s["output"]
    # 无 execute 成功:返回所有分队产出
    return {s["role"]: s["output"] for s in squads}


# =====================================================================
# 单分队 headless(orchestrator 内部调用分队时用)
# =====================================================================


async def run_squad_headless(*, role: str, command: dict[str, Any], cost_tracker) -> dict[str, Any]:
    """单个分队的 headless 执行 —— 装配对应 persona 的 agentic_loop 跑一轮任务。

    被 orchestrator 调用(协调器 → orchestrator → 本函数 → agentic_loop)。
    分队结果结构化回传,H4 PreResult hook 在引擎内已验证(如执行分队跑测试)。
    """
    from server.assembly import assemble_main_agent

    engine = assemble_main_agent(
        persona=role,
        session_ctx={"squad": True},
        cost_tracker=cost_tracker,
    )
    # 注:on_demand 引擎不调 run();直接调 run_turn
    messages = [{"role": "user", "content": command.get("text", "")}]
    context: dict[str, Any] = {}
    if command.get("_upstream"):
        context["_upstream"] = command["_upstream"]
    turn_result = await engine.run_turn(messages, context=context)
    return {
        "status": turn_result.get("status", "failed"),
        "output": turn_result.get("output") or turn_result.get("assistant_message"),
        "error": turn_result.get("error"),
        "cost_usd": turn_result.get("cost_usd", 0.0),
    }


# =====================================================================
# CLI 入口
# =====================================================================


def _read_input(args) -> dict[str, Any]:
    if args.input:
        with open(args.input) as f:
            return json.load(f)
    raw = sys.stdin.read()
    return json.loads(raw)


def _write_output(args, result: dict[str, Any]) -> None:
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w") as f:
            f.write(text)
    else:
        sys.stdout.write(text)
        sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hicode-headless", description="hicode headless: 结构化进出,无 TTY"
    )
    parser.add_argument("--input", help="命令 JSON 文件(缺省读 stdin)")
    parser.add_argument("--output", help="结果 JSON 文件(缺省写 stdout)")
    parser.add_argument("--config", help="配置文件路径")
    args = parser.parse_args(argv)

    # 初始化基础设施
    Infra.init(load_config(args.config))

    command = _read_input(args)
    result = asyncio.run(headless_run(command))
    _write_output(args, result)

    # 退出码:success → 0,failed → 1(供上游脚本/CI 判断)
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
