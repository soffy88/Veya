"""server/nano_w9/cli_gen — "make it agent-native" GoalRun template (addendum F).

Seven phases (analyze → design → implement → test_plan → tests → docs → package)
run as **omodul phases orchestrated by the existing**
``oservi.SequentialComposerEngine`` through a ``ServiceManifest`` — the canonical
way to compose multiple omodul (SPEC §1.2.1).  No new runner, no new scheduler.

The whole flow is registered as a routine/GoalRun template on the **existing**
machinery (``RoutineSpec.goal_template`` + ``project_run_goal``), is marked
``experimental``, and is gated by ``VEYA_CLI_GEN`` (default OFF).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from veya.omodul.agent_native_scaffold import (
    AGENT_NATIVE_PHASES,
    CliGenAnalyzeConfig,
    CliGenAnalyzeInput,
    cli_gen_analyze,
    cli_gen_design,
    cli_gen_docs,
    cli_gen_implement,
    cli_gen_package,
    cli_gen_test_plan,
    cli_gen_tests,
    phase_spec,
)

__all__ = [
    "AGENT_NATIVE_PHASES",
    "CLI_GEN_GOAL_TEMPLATE",
    "PHASE_FUNCTIONS",
    "build_cli_gen_manifest",
    "default_model",
    "phase_spec",
    "run_cli_gen",
]

#: phase name -> omodul callable with the (config, input_data, output_dir) contract.
PHASE_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "analyze": cli_gen_analyze,
    "design": cli_gen_design,
    "implement": cli_gen_implement,
    "test_plan": cli_gen_test_plan,
    "tests": cli_gen_tests,
    "docs": cli_gen_docs,
    "package": cli_gen_package,
}

#: The GoalRun objective template registered on the existing routine machinery.
CLI_GEN_GOAL_TEMPLATE = (
    "Make this repository agent-native.\n"
    "Run the 7-phase CLI generation flow: analyze -> design -> implement "
    "(--json CLI) -> test_plan -> tests -> docs/SKILL.md -> package.\n"
    "Every command must answer --json with a stable schema and a non-zero exit on error.\n"
    "Repo: {repo_ref}\n"
    "Experimental: this template is marked experimental and is gated by VEYA_CLI_GEN."
)


def default_model() -> str:
    """The strongest available model proxy.

    Delegates to the frozen canonical proxy table (``veya1.2``), so W9 does not
    perform task-semantic model selection (frozen §2 / CONFLICT-1).
    """
    try:
        from veya.obase.canonical_proxies import DEFAULT_PROXY

        return str(DEFAULT_PROXY)
    except Exception:
        return "veya1.2"


def build_cli_gen_manifest(
    *,
    config: CliGenAnalyzeConfig,
    name: str = "agent-native-cli-gen",
    repo_ref: str = "",
) -> Any:
    """Assemble the sequential_composer manifest over the 7 phase omodul.

    Args:
        config: the shared phase config (executor, probe_repo, write_artifact).
        name: manifest name.
        repo_ref: the target repo, used as the composer's initial input.

    Returns:
        A ``ServiceManifest`` over ``oservi.SequentialComposerEngine``, or ``None``
        when the external oservi library is unmounted (the caller then runs the
        phases sequentially in :func:`run_cli_gen`, with identical semantics).
    """
    try:
        from veya.platform import load

        oservi = load("oservi")
    except Exception:
        return None
    return oservi.ServiceManifest(
        name=name,
        skeleton="sequential_composer",
        inject={
            "steps": [PHASE_FUNCTIONS[phase] for phase in AGENT_NATIVE_PHASES],
            "router": [_carry_forward],
        },
        trigger={"on_demand": True},
        config={"repo_ref": repo_ref, "model": config.model, "experimental": True},
    )


def _carry_forward(payload: dict[str, Any]) -> dict[str, Any]:
    """Router: merge the previous phase's artifacts into the next input.

    ``SequentialComposerEngine`` routes ``(input_data, step_no)`` through the
    optional layer4 router; this keeps the 7 phases chained without any omodul
    importing its sibling.
    """
    routed: dict[str, Any] = dict(payload or {})
    raw_artifacts = routed.get("artifacts")
    previous: dict[str, Any] = dict(raw_artifacts) if isinstance(raw_artifacts, dict) else {}
    routed["carried"] = {**routed.get("carried", {}), **previous}
    routed["phase"] = AGENT_NATIVE_PHASES[
        min(int(payload.get("step_no") or 0), len(AGENT_NATIVE_PHASES) - 1)
    ]
    return routed


async def run_cli_gen(
    *,
    repo_ref: str,
    config: CliGenAnalyzeConfig | None = None,
    executor: Callable[..., Any] | None = None,
    probe_repo: Callable[[str], dict[str, Any]] | None = None,
    write_artifact: Callable[[str, str], str] | None = None,
    output_dir: str | Path | None = None,
    model: str = "",
    on_step: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run the 7-phase flow, via sequential_composer when it is available.

    Args:
        repo_ref: repo path or URL to make agent-native.
        config: pre-built phase config (overrides ``executor``/``probe_repo``/
            ``write_artifact`` when given).
        executor: injected per-phase executor (phase, context) -> dict.
        probe_repo: injected repo prober for the analyze phase.
        write_artifact: injected (path, content) -> str writer.
        output_dir: where phase artifacts are persisted.
        model: override for the default model.
        on_step: progress callback.

    Returns:
        ``{"status", "error", "phases", "results", "experimental", "model",
        "repo_ref", "engine"}``.  ``status`` is ``"completed"`` /
        ``"partial"`` / ``"failed"`` / ``"disabled"`` — never an exception.
    """
    from server.nano_w9.flags import flag_enabled

    if not flag_enabled("VEYA_CLI_GEN"):
        return {
            "status": "disabled",
            "error": {"code": "FEATURE_DISABLED", "flag": "VEYA_CLI_GEN"},
            "phases": [],
            "results": [],
            "experimental": True,
            "model": "",
            "repo_ref": repo_ref,
            "engine": None,
        }

    phase_config = config or CliGenAnalyzeConfig(
        executor=executor,
        probe_repo=probe_repo,
        write_artifact=write_artifact,
        model=model or default_model(),
    )
    if model:
        phase_config.model = model
    out_dir = Path(output_dir) if output_dir else None

    manifest = build_cli_gen_manifest(config=phase_config, repo_ref=repo_ref)
    engine_name = "sequential_composer" if manifest is not None else "sequential_fallback"

    results: list[dict[str, Any]] = []
    carried: dict[str, Any] = {}
    cost_usd = 0.0

    for index, phase in enumerate(AGENT_NATIVE_PHASES):
        payload = {
            "repo_ref": repo_ref,
            "phase": phase,
            "carried": dict(carried),
            "step_no": index,
            "artifacts": carried,
        }
        routed = _carry_forward(payload)
        try:
            result = await PHASE_FUNCTIONS[phase](
                phase_config,
                CliGenAnalyzeInput(
                    repo_ref=repo_ref,
                    phase=phase,
                    carried=routed.get("carried", {}),
                ),
                out_dir,
                on_step=on_step,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # §5.4: the flow reports, it does not raise
            result = {
                "status": "failed",
                "error": {"code": type(exc).__name__, "reason": str(exc)},
                "phase": phase,
                "artifacts": {},
            }
        results.append(result)
        if isinstance(result.get("artifacts"), dict):
            carried.update(result["artifacts"])
        with contextlib.suppress(TypeError, ValueError):
            cost_usd += float(result.get("cost_usd") or 0.0)
        if on_step is not None:
            with contextlib.suppress(Exception):
                on_step({"step_no": index, "phase": phase, "status": result.get("status")})

    failed = [r for r in results if r.get("status") != "completed"]
    status = "completed" if not failed else ("partial" if len(failed) < len(results) else "failed")
    return {
        "status": status,
        "error": failed[0].get("error") if failed else None,
        "phases": list(AGENT_NATIVE_PHASES),
        "results": results,
        "artifacts": carried,
        "cost_usd": cost_usd,
        "experimental": True,
        "model": phase_config.model,
        "repo_ref": repo_ref,
        "engine": engine_name,
    }
