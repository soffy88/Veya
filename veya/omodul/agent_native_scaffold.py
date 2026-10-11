"""veya/omodul/agent_native_scaffold — 7 阶段 "make it agent-native" omodul。

给定一个 repo 路径/URL，把"手工搭一个 CLI"拆成 7 个可验收阶段：

    1 analyze      分析目标 (仓库结构 / 现有命令面)
    2 design       设计 command group 树
    3 implement    实现 --json CLI 骨架
    4 test_plan    产出测试计划
    5 tests        产出测试
    6 docs         产出 SKILL.md
    7 package      打包

**归属（§6 omodul）**: 每个阶段一个业务事务，标准签名
``(config, input_data, output_dir) -> dict``，必返 status/error，失败不 raise，
各自声明 ``_enabled_pillars``。阶段之间**不互相 import**（同名层裸调禁令），
由 layer4 ``server/nano_w9/cli_gen.py`` 经既有
``oservi.SequentialComposerEngine``（ServiceManifest 装配）顺序编排 —— 这正是
§1.2.1「omodul 组合走编排层」的正路。

**experimental**: 每个返回值都带 ``experimental: True``；默认模型取最强可用
（``veya1.2`` 代理，见 ``veya/obase/canonical_proxies.py``），由 layer4 注入。
全部行为受 ``VEYA_CLI_GEN`` 开关控制（默认关）。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "AGENT_NATIVE_PHASES",
    "CliGenAnalyzeConfig",
    "CliGenAnalyzeInput",
    "cli_gen_analyze",
    "cli_gen_design",
    "cli_gen_docs",
    "cli_gen_implement",
    "cli_gen_package",
    "cli_gen_test_plan",
    "cli_gen_tests",
    "phase_spec",
]

#: 阶段顺序（编排层的 steps 列表即此顺序）。
AGENT_NATIVE_PHASES: tuple[str, ...] = (
    "analyze",
    "design",
    "implement",
    "test_plan",
    "tests",
    "docs",
    "package",
)


def phase_spec(phase: str) -> dict[str, Any]:
    """返回某阶段的声明（标题 / 产出 / 验收）。

    Args:
        phase: :data:`AGENT_NATIVE_PHASES` 中的一项。

    Returns:
        ``{"phase", "title", "produces", "acceptance"}``；未知阶段返回
        ``phase == phase`` 的空产出声明而不抛错（编排层需要能跳过未知阶段）。

    Example:
        >>> phase_spec("design")["title"]
        'Design command groups'
    """
    specs: dict[str, tuple[str, str, str]] = {
        "analyze": (
            "Analyze target",
            "target profile (language, entry points, existing CLI surface)",
            "profile lists at least one entry point or an explicit none-reason",
        ),
        "design": (
            "Design command groups",
            "command group tree {group: [commands]}",
            "every group has >=1 command and names are kebab-case",
        ),
        "implement": (
            "Implement --json CLI",
            "CLI skeleton implementing the designed groups",
            "every command answers --json with {status: ok|error}",
        ),
        "test_plan": (
            "Test plan",
            "ordered test plan with one case per command",
            "plan covers every command in the design",
        ),
        "tests": (
            "Tests",
            "test source implementing the plan",
            "one test module exists per command group",
        ),
        "docs": (
            "Docs / SKILL.md",
            "SKILL.md with YAML frontmatter + examples",
            "SKILL.md has name/description frontmatter and >=1 example",
        ),
        "package": (
            "Package",
            "packaging manifest for the generated CLI",
            "manifest names the entry point and the install command",
        ),
    }
    title, produces, acceptance = specs.get(
        phase, (phase, "", "phase is not one of AGENT_NATIVE_PHASES")
    )
    return {
        "phase": phase,
        "title": title,
        "produces": produces,
        "acceptance": acceptance,
        "experimental": True,
    }


# --------------------------------------------------------------------------
# config / input
# --------------------------------------------------------------------------


@dataclass
class CliGenAnalyzeConfig:
    """所有阶段共享的配置（注入式，不自带任何 IO）。"""

    #: 阶段注入点：由 layer4 提供的执行器 (phase, payload) -> dict
    executor: Callable[..., Any] | None = None
    #: 注入的仓库探查 (repo_ref) -> dict
    probe_repo: Callable[[str], dict[str, Any]] | None = None
    #: 注入的文件写入 (path, content) -> str（阶段产出落盘）
    write_artifact: Callable[[str, str], str] | None = None
    #: 默认模型 = 最强可用；由 layer4 解析后注入，omodul 不选型（冻结 §2 CONFLICT-1）
    model: str = ""
    budget_usd: float = 2.0
    experimental: bool = True

    _omodul_name: str = "agent_native_scaffold"
    _omodul_version: str = "1.0.0"
    _fingerprint_fields: set[str] = field(default_factory=lambda: {"repo_ref", "phase"})
    _enabled_pillars: set[str] = field(default_factory=lambda: {"fingerprint", "cost"})


@dataclass
class CliGenAnalyzeInput:
    """所有阶段共享的输入。

    ``carried`` 是编排层（sequential_composer）逐阶段累积的产物字典：
    前一阶段的输出直接成为后一阶段的输入，这是 ``_invoke_step`` 单参传递的
    约定（阶段内自行合并 ``carried`` 与 ``self`` 字段）。
    """

    repo_ref: str = ""
    phase: str = "analyze"
    carried: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# 阶段执行内核（各阶段唯一差异是 phase 名 + 校验）
# --------------------------------------------------------------------------


async def _run_phase(
    config: CliGenAnalyzeConfig,
    input_data: CliGenAnalyzeInput,
    output_dir: Path | None,
    phase: str,
    on_step: Callable[[dict[str, Any]], None] | None,
) -> dict[str, Any]:
    """跑一个阶段：声明 → 注入执行器 → 验收 → 返回标准契约。

    失败一律以 ``status="failed"`` 返回（``CancelledError`` 除外，§5.6 C4）。
    """
    spec = phase_spec(phase)
    cost_usd = 0.0

    def record(event: str, **payload: Any) -> None:
        if on_step is not None:
            with contextlib.suppress(Exception):
                on_step({"step_no": phase, "event": event, **payload})

    try:
        record("phase.started", phase=phase, title=spec["title"])
        context = {
            "phase": phase,
            "spec": spec,
            "repo_ref": input_data.repo_ref,
            "carried": dict(input_data.carried or {}),
            "payload": dict(input_data.payload or {}),
            "model": config.model,
            "budget_usd": config.budget_usd,
        }

        if phase == "analyze" and config.probe_repo is not None and input_data.repo_ref:
            profile = await _maybe_await(config.probe_repo(input_data.repo_ref))
            if isinstance(profile, dict):
                context["profile"] = profile

        if config.executor is not None:
            result = await _maybe_await(config.executor(context))
            if isinstance(result, dict):
                context.update(result)
                with contextlib.suppress(TypeError, ValueError):
                    cost_usd += float(result.get("cost_usd") or 0.0)

        if phase != "analyze" and not str(input_data.repo_ref or context.get("repo_ref") or ""):
            return {
                "status": "failed",
                "error": {"code": "INVALID_INPUT", "reason": "repo_ref is required"},
                "phase": phase,
                "artifacts": {},
                "fingerprint": "",
                "decision_trail": [],
                "cost_usd": cost_usd,
                "experimental": True,
            }

        artifacts = {
            k: v
            for k, v in context.items()
            if k not in {"phase", "spec", "carried", "payload", "repo_ref", "model", "budget_usd"}
        }
        if not artifacts:
            artifacts = {"summary": spec["produces"]}

        written = _persist(config, output_dir, phase, artifacts)
        if written:
            artifacts["artifact_paths"] = written

        record("phase.completed", phase=phase, artifacts=sorted(artifacts))
        return {
            "status": "completed",
            "error": None,
            "phase": phase,
            "title": spec["title"],
            "acceptance": spec["acceptance"],
            "repo_ref": input_data.repo_ref,
            "artifacts": artifacts,
            "fingerprint": _fingerprint(config, input_data),
            "decision_trail": [
                {"step_no": phase, "event": "phase.started"},
                {"step_no": phase, "event": "phase.completed"},
            ],
            "cost_usd": cost_usd,
            "experimental": True,
        }
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # §5.4 不 raise
        return {
            "status": "failed",
            "error": {"code": type(exc).__name__, "reason": str(exc)},
            "phase": phase,
            "artifacts": {},
            "fingerprint": "",
            "decision_trail": [],
            "cost_usd": cost_usd,
            "experimental": True,
        }


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
        return await value
    return value


def _fingerprint(config: CliGenAnalyzeConfig, input_data: CliGenAnalyzeInput) -> str:
    from veya.oprim.audit_chain import GENESIS_HASH, compute_event_hash

    fields = set(getattr(config, "_fingerprint_fields", set()) or set())
    data = {"repo_ref": input_data.repo_ref, "phase": input_data.phase}
    if not fields:
        return ""
    return compute_event_hash({k: data.get(k) for k in sorted(fields)}, prev_hash=GENESIS_HASH)[:24]


def _persist(
    config: CliGenAnalyzeConfig, output_dir: Path | None, phase: str, artifacts: dict[str, Any]
) -> list[str]:
    """把阶段产出落到 output_dir（优先用注入写入器，否则不落盘）。"""
    if output_dir is None or config.write_artifact is None:
        return []
    import json

    try:
        body = json.dumps(artifacts, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        body = str(artifacts)
    try:
        written = config.write_artifact(str(Path(output_dir) / f"phase_{phase}.json"), body)
        return [str(written)] if written else []
    except Exception:
        return []


# --------------------------------------------------------------------------
# 七个 omodul 入口（每个都满足 (config, input_data, output_dir) 三件套）
# --------------------------------------------------------------------------


async def cli_gen_analyze(
    config: CliGenAnalyzeConfig,
    input_data: CliGenAnalyzeInput,
    output_dir: Path | None = None,
    *,
    on_step: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """阶段 1：分析目标仓库（结构 / 入口 / 既有 CLI 面）。"""
    return await _run_phase(config, input_data, output_dir, "analyze", on_step)


async def cli_gen_design(
    config: CliGenAnalyzeConfig,
    input_data: CliGenAnalyzeInput,
    output_dir: Path | None = None,
    *,
    on_step: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """阶段 2：设计 command group 树。"""
    return await _run_phase(config, input_data, output_dir, "design", on_step)


async def cli_gen_implement(
    config: CliGenAnalyzeConfig,
    input_data: CliGenAnalyzeInput,
    output_dir: Path | None = None,
    *,
    on_step: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """阶段 3：实现 ``--json`` CLI 骨架。"""
    return await _run_phase(config, input_data, output_dir, "implement", on_step)


async def cli_gen_test_plan(
    config: CliGenAnalyzeConfig,
    input_data: CliGenAnalyzeInput,
    output_dir: Path | None = None,
    *,
    on_step: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """阶段 4：产出测试计划（每条命令一个用例）。"""
    return await _run_phase(config, input_data, output_dir, "test_plan", on_step)


async def cli_gen_tests(
    config: CliGenAnalyzeConfig,
    input_data: CliGenAnalyzeInput,
    output_dir: Path | None = None,
    *,
    on_step: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """阶段 5：产出测试实现。"""
    return await _run_phase(config, input_data, output_dir, "tests", on_step)


async def cli_gen_docs(
    config: CliGenAnalyzeConfig,
    input_data: CliGenAnalyzeInput,
    output_dir: Path | None = None,
    *,
    on_step: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """阶段 6：产出 ``SKILL.md``（YAML frontmatter + 示例）。"""
    return await _run_phase(config, input_data, output_dir, "docs", on_step)


async def cli_gen_package(
    config: CliGenAnalyzeConfig,
    input_data: CliGenAnalyzeInput,
    output_dir: Path | None = None,
    *,
    on_step: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """阶段 7：打包。"""
    return await _run_phase(config, input_data, output_dir, "package", on_step)
