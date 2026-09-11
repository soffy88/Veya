"""Fresh evidence runner for the VEYA_AUTONOMOUS_AGENT_P0 benchmark.

The runner submits fixed inputs through the real ProductShell HTTP route and
only scores facts read back from TaskStore/EventStore, trajectory JSONL,
GoalRun JSONL/taskgraph, and task-scoped verification artifacts.  Fixture
labels are used for denominators and expected comparisons only; they never
stand in for an observed capability, tool, result, or verification.

Examples:
    venv/bin/python evals/p0_autonomous_agent/runner.py --in-process
    venv/bin/python evals/p0_autonomous_agent/runner.py --base-url http://127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

OBSERVABILITY_TOPICS = {
    "capability.decision",
    "tool.call",
    "tool.result",
    "approval.suspended",
    "approval.resumed",
    "replan.started",
    "replan.completed",
}
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
FORBIDDEN_CODING_TOOLS = {"write_file", "run_in_sandbox", "hicode_run"}
NON_CODING_TOOLS = {
    "research": {
        "fetch_url",
        "browser_run",
        "mcp_stratum",
        "search_genesis_ledger",
        "memory_search",
    },
    "browser": {"browser_run", "fetch_url"},
    "knowledge": {
        "read_file_ast",
        "read_hashline",
        "list_files",
        "grep",
        "ast_grep_search",
        "assemble_code_context",
        "mcp_codebase",
        "memory_search",
        "memory_get",
        "skill_search",
        "skill_show",
    },
}
BEHAVIOR_ONLY_CATEGORIES = frozenset({"approval", "failure_replan"})


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        with contextlib.suppress(json.JSONDecodeError):
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    return rows


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    value = event.get("payload")
    return value if isinstance(value, dict) else event


def _topic(event: dict[str, Any]) -> str:
    return str(event.get("topic") or event.get("type") or event.get("event") or "")


def _event_key(event: dict[str, Any]) -> str:
    return str(event.get("event_id") or json.dumps(event, sort_keys=True, default=str))


def _truthy_result(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (dict, list, tuple)):
        return bool(value)
    text = str(value).strip().lower()
    return (
        bool(text)
        and text not in {"none", "null", ""}
        and "not configured" not in text
        and "shim" not in text
    )


def _success_status(value: Any) -> bool:
    return str(value or "").strip().lower() in {
        "completed",
        "complete",
        "success",
        "succeeded",
        "passed",
        "pass",
        "ok",
    }


def _extract_goal_run_id(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        payload = _payload(event)
        value = payload.get("goal_run_id")
        if value:
            return str(value)
        result = payload.get("result")
        if isinstance(result, str):
            with contextlib.suppress(json.JSONDecodeError):
                decoded = json.loads(result)
                if isinstance(decoded, dict) and decoded.get("goal_run_id"):
                    return str(decoded["goal_run_id"])
    return None


class EvidenceReader:
    def __init__(
        self,
        *,
        event_store_path: Path,
        trajectory_dir: Path,
        workspace: Path,
    ) -> None:
        self.event_store_path = event_store_path
        self.trajectory_dir = trajectory_dir
        self.workspace = workspace

    async def read(self, task: dict[str, Any], task_events: list[dict[str, Any]]) -> dict[str, Any]:
        task_id = str(task["id"])
        session_id = str(task.get("session_id") or "")
        trace_id = str(task.get("trace_id") or "")
        all_canonical = _jsonl(self.event_store_path)
        events_by_key: dict[str, dict[str, Any]] = {}
        for event in [*all_canonical, *task_events]:
            payload = _payload(event)
            event_task = event.get("task_id") or payload.get("task_id")
            event_session = event.get("session_id")
            event_trace = event.get("trace_id") or payload.get("trace_id")
            if str(event_task or "") == task_id or (
                str(event_session or "") == session_id and str(event_trace or "") == trace_id
            ):
                events_by_key[_event_key(event)] = event
        events = sorted(
            events_by_key.values(),
            key=lambda item: (float(item.get("ts") or 0), _event_key(item)),
        )
        trajectory = _jsonl(self.trajectory_dir / f"{task_id}.jsonl")
        goal_run_id = _extract_goal_run_id(events)
        goal_events: list[dict[str, Any]] = []
        goalgraph: dict[str, Any] | None = None
        if goal_run_id:
            goal_dir = self.workspace / ".veya-project" / "goal-runs" / goal_run_id
            goal_events = _jsonl(goal_dir / "events.jsonl")
            graph_path = goal_dir / "taskgraph.json"
            if graph_path.is_file():
                with contextlib.suppress(json.JSONDecodeError):
                    loaded = json.loads(graph_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        goalgraph = loaded
        artifacts: dict[str, Any] = {}
        artifact_dir = self.workspace / ".veya" / "runs" / task_id / "outputs"
        for name in (
            "verification_report.json",
            "sensor_report.json",
            "final_result.json",
            "delegate_result.json",
        ):
            path = artifact_dir / name
            if path.is_file():
                with contextlib.suppress(json.JSONDecodeError):
                    value = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(value, dict):
                        artifacts[name] = value
        p0_events = [event for event in events if _topic(event) in OBSERVABILITY_TOPICS]
        return {
            "task": task,
            "events": events,
            "p0_events": p0_events,
            "trajectory": trajectory,
            "goal_run_id": goal_run_id,
            "goal_events": goal_events,
            "goalgraph": goalgraph,
            "artifacts": artifacts,
        }


def _common_field_failures(evidence: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for event in evidence["p0_events"]:
        payload = _payload(event)
        for key in ("task_id", "trace_id", "goal_run_id", "capability", "tool", "action", "status"):
            if key not in payload:
                missing.append(f"{_topic(event)}:{key}")
    return missing


def _capability(evidence: dict[str, Any]) -> str | None:
    decisions = [event for event in evidence["p0_events"] if _topic(event) == "capability.decision"]
    values = [str(_payload(event).get("capability") or "") for event in decisions]
    values = [value for value in values if value]
    return values[-1] if values else None


def _execution_mode(evidence: dict[str, Any]) -> str:
    decisions = [event for event in evidence["p0_events"] if _topic(event) == "capability.decision"]
    for event in reversed(decisions):
        value = _payload(event).get("execution_mode")
        if value:
            return str(value)
    return "unknown"


def _tool_calls(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    return [event for event in evidence["p0_events"] if _topic(event) == "tool.call"]


def _tool_name(event: dict[str, Any]) -> str:
    payload = _payload(event)
    return str(payload.get("tool") or payload.get("tool_name") or "")


def _verified_result_evidence(evidence: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for event in evidence["p0_events"]:
        if _topic(event) != "tool.result":
            continue
        payload = _payload(event)
        if _success_status(payload.get("status")) and _truthy_result(payload.get("result")):
            found.append(f"event:{event.get('event_id', 'tool.result')}")
    for name, value in evidence["artifacts"].items():
        accepted = value.get("acceptance_passed")
        status = value.get("status")
        if accepted is True or _success_status(status):
            found.append(f"artifact:{name}")
    for event in evidence["events"]:
        if _topic(event) == "artifact.verified" and _success_status(
            _payload(event).get("status", "passed")
        ):
            found.append(f"event:{event.get('event_id', 'artifact.verified')}")
    for index, trajectory in enumerate(evidence["trajectory"]):
        acceptance = trajectory.get("acceptance_results") or []
        if (
            str(trajectory.get("outcome") or "").lower() == "completed"
            and acceptance
            and all(
                _success_status(item.get("status")) for item in acceptance if isinstance(item, dict)
            )
        ):
            found.append(f"trajectory:{index}")
    graph = evidence.get("goalgraph") or {}
    for item in graph.get("tasks") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "") == "completed" and (
            item.get("verify_summary") or item.get("evidence") or item.get("assertions")
        ):
            found.append("goalrun:taskgraph")
    return found


def _legacy_bypass(evidence: dict[str, Any]) -> dict[str, Any] | None:
    capability = _capability(evidence)
    mode = _execution_mode(evidence)
    bypass_tools: set[str] = set()
    matching_events: list[dict[str, Any]] = []
    for event in evidence["events"]:
        if _topic(event) == "legacy.tool_bypassed":
            payload = _payload(event)
            raw = payload.get("bypassed") or payload.get("legacy_tools_attempted") or []
            bypass_tools.update(str(value) for value in raw)
            matching_events.append(event)
    if capability == "coding" and mode == "goal":
        for event in _tool_calls(evidence):
            name = _tool_name(event)
            if name in FORBIDDEN_CODING_TOOLS:
                bypass_tools.add(name)
                matching_events.append(event)
    if not bypass_tools:
        return None
    sequence = [_topic(event) for event in evidence["events"] if _topic(event)]
    if matching_events and any(
        _topic(event) == "legacy.tool_bypassed" for event in matching_events
    ):
        root_cause = (
            "Persisted legacy.tool_bypassed evidence names the bypassed tool(s); "
            "the legacy execution path emitted the bypass fact instead of staying on the canonical harness."
        )
    else:
        root_cause = (
            "Observed a forbidden top-level tool.call after capability.decision="
            f"{capability!r}, execution_mode={mode!r}; the coding+goal boundary was bypassed."
        )
    return {
        "task_id": str(evidence["task"]["id"]),
        "capability": capability or "unknown",
        "execution_mode": mode,
        "bypass_tool": sorted(bypass_tools),
        "event_sequence": sequence,
        "root_cause": root_cause,
    }


def _historical_legacy_bypasses(event_store_path: Path) -> list[dict[str, Any]]:
    """Explain persisted legacy bypass facts without mixing them into fresh scores."""
    events = _jsonl(event_store_path)
    by_task: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        payload = _payload(event)
        task_id = str(event.get("task_id") or payload.get("task_id") or "")
        if task_id:
            by_task.setdefault(task_id, []).append(event)
    reports: list[dict[str, Any]] = []
    for task_id, task_events in by_task.items():
        legacy = [event for event in task_events if _topic(event) == "legacy.tool_bypassed"]
        for event in legacy:
            payload = _payload(event)
            decision = next(
                (item for item in task_events if _topic(item) == "capability.decision"),
                {},
            )
            decision_payload = _payload(decision)
            attempted = payload.get("legacy_tools_attempted") or []
            bypassed = payload.get("bypassed") or attempted
            reports.append(
                {
                    "task_id": task_id,
                    "capability": str(decision_payload.get("capability") or "unknown"),
                    "execution_mode": str(
                        decision_payload.get("execution_mode")
                        or payload.get("execution_mode")
                        or "unknown"
                    ),
                    "bypass_tool": [str(value) for value in bypassed],
                    "event_sequence": [_topic(item) for item in task_events if _topic(item)],
                    "root_cause": (
                        "The persisted sequence records a coding capability followed by a legacy "
                        "tool_name=write call/result and explicitly marks the attempted legacy "
                        "tools as bypassed; execution_mode was not persisted, so it remains unknown."
                    ),
                    "legacy_tools_attempted": [str(value) for value in attempted],
                }
            )
    return reports


def _score_case(spec: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    capability = _capability(evidence)
    expected = str(spec.get("expected_capability") or "")
    tools = {_tool_name(event) for event in _tool_calls(evidence)}
    result_evidence = _verified_result_evidence(evidence)
    bypass = _legacy_bypass(evidence)
    category = str(spec["category"])
    semantic_capability_ok = capability == expected and not _common_field_failures(evidence)
    behavior_only = category in BEHAVIOR_ONLY_CATEGORIES
    capability_ok = (
        semantic_capability_ok
        if not behavior_only
        else bool(capability) and not _common_field_failures(evidence)
    )
    coding_ok = False
    if category == "coding":
        coding_ok = (
            capability_ok
            and _execution_mode(evidence) == "goal"
            and "coding_task_run" in tools
            and bypass is None
        )
    non_coding_ok = (
        category in NON_CODING_TOOLS and capability_ok and bool(tools & NON_CODING_TOOLS[category])
    )
    approval_events = [
        event
        for event in evidence["p0_events"]
        if _topic(event) in {"approval.suspended", "approval.resumed"}
    ]
    suspended = next(
        (event for event in approval_events if _topic(event) == "approval.suspended"), None
    )
    resumed = next(
        (event for event in approval_events if _topic(event) == "approval.resumed"), None
    )
    approval_ok = False
    if suspended and resumed:
        suspended_payload = _payload(suspended)
        resumed_payload = _payload(resumed)
        same_action = (
            suspended_payload.get("tool") == resumed_payload.get("tool")
            and suspended_payload.get("action") == resumed_payload.get("action")
            and suspended_payload.get("tool") is not None
        )
        resumed_index = evidence["p0_events"].index(resumed)
        resumed_tool = str(resumed_payload.get("tool") or "")
        later_call = any(
            _tool_name(event) == resumed_tool
            for event in evidence["p0_events"][resumed_index + 1 :]
            if _topic(event) == "tool.call"
        )
        approval_ok = (
            same_action
            and later_call
            and _success_status(
                next(
                    (
                        _payload(event).get("status")
                        for event in evidence["p0_events"][resumed_index + 1 :]
                        if _topic(event) == "tool.result" and _tool_name(event) == resumed_tool
                    ),
                    None,
                )
            )
        )
    replan_topics = [_topic(event) for event in evidence["p0_events"]]
    replan_ok = (
        "replan.started" in replan_topics
        and "replan.completed" in replan_topics
        and bool(result_evidence)
        and str(evidence["task"].get("status")) == "completed"
    )
    started = any(_topic(event) == "task.started" for event in evidence["events"])
    completed = str(evidence["task"].get("status")) == "completed" and any(
        _topic(event) == "task.completed" for event in evidence["events"]
    )
    return {
        "task_id": str(evidence["task"]["id"]),
        "status": str(evidence["task"].get("status") or "unknown"),
        "capability": capability,
        "category": category,
        "execution_mode": _execution_mode(evidence),
        "started": started,
        "completed": completed,
        "capability_ok": capability_ok,
        "semantic_capability_ok": semantic_capability_ok,
        "behavior_only": behavior_only,
        "coding_ok": coding_ok,
        "non_coding_ok": non_coding_ok,
        "approval_ok": approval_ok,
        "replan_ok": replan_ok,
        "behavior_ok": approval_ok if category == "approval" else replan_ok if category == "failure_replan" else None,
        "verified_success": bool(result_evidence),
        "verification_evidence": result_evidence,
        "legacy_bypass": bypass,
        "event_sequence": [_topic(event) for event in evidence["events"] if _topic(event)],
        "missing_common_fields": _common_field_failures(evidence),
        "observed_tools": sorted(tool for tool in tools if tool),
    }


async def _wait_task(
    client: httpx.AsyncClient,
    task_id: str,
    *,
    spec: dict[str, Any],
    timeout_s: float,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    from server import auth as auth_mod

    deadline = time.monotonic() + timeout_s
    approved: set[str] = set()
    errors: list[str] = []
    latest_task: dict[str, Any] = {"id": task_id, "status": "unknown"}
    latest_events: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        task_response = await client.get(f"/api/v1/tasks/{task_id}")
        if task_response.status_code == 200:
            latest_task = dict(task_response.json().get("task") or latest_task)
        else:
            errors.append(f"task GET HTTP {task_response.status_code}: {task_response.text[:200]}")
        event_response = await client.get(f"/api/v1/tasks/{task_id}/events")
        if event_response.status_code == 200:
            latest_events = list(event_response.json().get("events") or [])
        else:
            errors.append(
                f"events GET HTTP {event_response.status_code}: {event_response.text[:200]}"
            )
        for event in latest_events:
            if _topic(event) != "approval.suspended":
                continue
            request_id = str(_payload(event).get("request_id") or "")
            if not request_id or request_id in approved:
                continue
            # Bind the same identity the task was created with, so the
            # in-process store resolves the correct pending action.  Without
            # this, every poll sees an anonymous user and the pending
            # approval lookup misses the task's action.
            auth_mod.set_user(auth_mod.current_user())
            approval_response = await client.post(
                "/api/v1/agent/approval",
                json={"request_id": request_id, "approved": True},
            )
            approved.add(request_id)
            if approval_response.status_code != 200 or not approval_response.json().get("ok"):
                errors.append(
                    f"approval POST {request_id} HTTP {approval_response.status_code}: "
                    f"{approval_response.text[:200]}"
                )
        if str(latest_task.get("status")) in TERMINAL_STATUSES:
            return latest_task, latest_events, errors
        await asyncio.sleep(0.5)
    with contextlib.suppress(Exception):
        await client.post(f"/api/v1/tasks/{task_id}/cancel")
    errors.append(f"timeout after {timeout_s:g}s")
    return latest_task, latest_events, errors


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    fixture_path = Path(args.inputs).resolve()
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    specs = list(fixture.get("tasks") or [])
    if len(specs) != 8:
        raise ValueError(f"expected exactly 8 fixed inputs, got {len(specs)}")
    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError(f"workspace does not exist: {workspace}")

    if args.in_process:
        # Read and write the existing authorities.  These are the same paths
        # used by the running product, not a benchmark-specific trace store.
        os.environ["VEYA_EVENT_STORE_PATH"] = str(Path(args.event_store).expanduser().resolve())
        os.environ["VEYA_TASK_STORE_PATH"] = str(Path(args.task_store).expanduser().resolve())
        os.environ["VEYA_PROJECT_ROOT"] = str(workspace)
        # The repository .env is production-shaped.  This subprocess is an
        # isolated local evidence run, so keep the real app lifecycle while
        # selecting its existing local execution profile; do not alter the
        # checked-in or user production configuration.
        os.environ["VEYA_EXECUTION_PRODUCTION"] = "0"
        os.environ["VEYA_DURABLE_EXECUTION"] = "0"
        # The repository-level user config (~/.veya/config.json) pins an
        # external provider/model that may be rate-limited (OpenRouter 429) or
        # out of balance (GMI 402).  This isolated local evidence run uses the
        # required gpt-5.6-luna @ 127.0.0.1:10100 provider and does not touch user credentials.
        os.environ["VEYA_LLM_PROVIDER"] = "openai"
        os.environ["VEYA_LLM_MODEL"] = "gpt-5.6-luna"
        os.environ["VEYA_LLM_ENDPOINT"] = "http://127.0.0.1:10100/v1"
        import server.trajectory as trajectory_module

        trajectory_dir = Path(args.trajectory_dir).expanduser().resolve()
        trajectory_module._default_path = lambda task_id: trajectory_dir / f"{task_id}.jsonl"
        from server.app import app

        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        client_context: Any = httpx.AsyncClient(transport=transport, base_url="http://p0.local")
        event_store_path = Path(args.event_store).expanduser().resolve()
    else:
        trajectory_dir = Path.home() / ".veya" / "trajectories"
        event_store_path = Path(args.event_store).expanduser().resolve()
        client_context = httpx.AsyncClient(base_url=args.base_url.rstrip("/"), timeout=30.0)

    results: list[dict[str, Any]] = []
    async with contextlib.AsyncExitStack() as stack:
        if args.in_process:
            await stack.enter_async_context(app.router.lifespan_context(app))
        client = await stack.enter_async_context(client_context)
        reader = EvidenceReader(
            event_store_path=event_store_path,
            trajectory_dir=trajectory_dir,
            workspace=workspace,
        )
        for spec in specs:
            objective = str(spec["objective"]).replace("{{WORKSPACE}}", str(workspace))
            response = await client.post(
                "/api/v1/bot/tasks",
                json={
                    "objective": objective,
                    "title": f"P0 {spec['id']}",
                    "workspace_id": str(workspace),
                },
            )
            if response.status_code not in {200, 201, 202}:
                results.append(
                    {
                        "id": spec["id"],
                        "status": "submission_failed",
                        "error": f"HTTP {response.status_code}: {response.text[:500]}",
                        "started": False,
                        "completed": False,
                        "capability_ok": False,
                        "verified_success": False,
                    }
                )
                continue
            created = response.json()
            task_id = str(created.get("task_id") or "")
            if not task_id:
                results.append(
                    {
                        "id": spec["id"],
                        "status": "submission_failed",
                        "error": "ProductShell response omitted task_id",
                        "started": False,
                        "completed": False,
                        "capability_ok": False,
                        "verified_success": False,
                    }
                )
                continue
            task, task_events, wait_errors = await _wait_task(
                client, task_id, spec=spec, timeout_s=args.timeout
            )
            evidence = await reader.read(task, task_events)
            score = _score_case(spec, evidence)
            score["fixture_id"] = spec["id"]
            score["wait_errors"] = wait_errors
            results.append(score)
            print(f"TASK_{len(results)}=" + json.dumps(score, ensure_ascii=False, sort_keys=True))
    coding = [item for item in results if item.get("category") == "coding"]
    noncoding = [item for item in results if item.get("category") in {"research", "browser", "knowledge"}]
    semantic = [item for item in results if not item.get("behavior_only")]
    behavior = [item for item in results if item.get("behavior_only")]
    approval = next((item for item in results if item.get("fixture_id") == "approval-1"), {})
    replan = next((item for item in results if item.get("fixture_id") == "replan-1"), {})
    bypasses = [item["legacy_bypass"] for item in results if item.get("legacy_bypass")]
    historical_bypasses = _historical_legacy_bypasses(event_store_path)
    summary = {
        "task_started": f"{sum(bool(item.get('started')) for item in results)}/8",
        "task_completed": f"{sum(bool(item.get('completed')) for item in results)}/8",
        "capability_selection": f"{sum(bool(item.get('semantic_capability_ok')) for item in semantic)}/{len(semantic)}",
        "semantic_capability_selection": f"{sum(bool(item.get('semantic_capability_ok')) for item in semantic)}/{len(semantic)}",
        "coding_routing": f"{sum(bool(item.get('coding_ok')) for item in coding)}/3",
        "non_coding_routing": f"{sum(bool(item.get('non_coding_ok')) for item in noncoding)}/3",
        "legacy_bypass": len(bypasses),
        "legacy_bypass_root_cause": bypasses or historical_bypasses,
        "historical_legacy_bypass": historical_bypasses,
        "approval_resume": "PASS" if approval.get("approval_ok") else "FAIL",
        "replan_recovery": "PASS" if replan.get("replan_ok") else "FAIL",
        "behavior_scenarios": f"{sum(bool(item.get('behavior_ok')) for item in behavior)}/{len(behavior)}",
        "verified_success": f"{sum(bool(item.get('verified_success')) for item in results)}/8",
        "results": results,
    }
    print("SUMMARY=" + json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inputs", default=str(Path(__file__).with_name("inputs.json")))
    parser.add_argument("--workspace", default=os.getcwd())
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--in-process", action="store_true")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--event-store", default=str(Path.home() / ".veya" / "events.jsonl"))
    parser.add_argument("--task-store", default=str(Path.home() / ".veya" / "tasks.json"))
    parser.add_argument("--trajectory-dir", default=str(Path.home() / ".veya" / "trajectories"))
    args = parser.parse_args()
    try:
        summary = asyncio.run(_run(args))
    except Exception as exc:
        print(f"RUNNER_ERROR={type(exc).__name__}: {exc}")
        return 2
    all_pass = (
        summary["task_started"] == "8/8"
        and summary["task_completed"] == "8/8"
        and summary["semantic_capability_selection"] == "6/6"
        and summary["behavior_scenarios"] == "2/2"
        and summary["coding_routing"] == "3/3"
        and summary["non_coding_routing"] == "3/3"
        and summary["legacy_bypass"] == 0
        and summary["approval_resume"] == "PASS"
        and summary["replan_recovery"] == "PASS"
        and summary["verified_success"] == "8/8"
    )
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
