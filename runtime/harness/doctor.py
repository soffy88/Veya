"""Read-only Harness Doctor for coding workspaces."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Literal

from runtime.coding.command_runner import command_requires_approval, parse_command
from runtime.coding.workspace_detect import detect_workspace

from .guides import guide_commands, load_guides
from .models import HarnessCheck, HarnessDoctorReport, SensorResult
from .sensors import sensor_acceptance, sensors_for_workspace


def _status_value(value: str | bool | None) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    return value.upper() in {"PASS", "PASSED", "OK", "HEALTHY", "READY"}


def _cached_gold_status(root: Path) -> bool | None:
    candidates = (
        root / "evals" / "personal_agent_gold" / "results" / "latest.json",
        root / "docs" / "release-health" / "release-candidate-latest.json",
    )
    for path in candidates:
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if raw.get("status") == "PASS" or raw.get("gold_gate") == "PASS":
            return True
        if raw.get("status") == "FAIL" or raw.get("gold_gate") == "FAIL":
            return False
    return None


def _cached_health_status(root: Path) -> bool | None:
    path = root / "docs" / "release-health" / "release-candidate-latest.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    public = raw.get("public_health")
    if isinstance(public, dict):
        code = public.get("http_status") or public.get("status_code")
        if code == 200:
            return True
        if isinstance(code, int):
            return False
        if public.get("status") in {"PASS", "healthy", "ok"}:
            return True
    return None


def _git_clean(root: Path) -> tuple[bool | None, str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"git status unavailable: {type(exc).__name__}"
    if result.returncode != 0:
        return None, (result.stderr or result.stdout).strip()[:500]
    return not bool(
        result.stdout.strip()
    ), "clean" if not result.stdout.strip() else "uncommitted changes present"


def run_harness_doctor(
    path: str | Path = ".",
    *,
    unattended: bool = False,
    sensor_results: Iterable[SensorResult | Mapping[str, Any]] | None = None,
    permissions_explicit: bool | None = None,
    gold_gate_status: str | bool | None = None,
    health_status: str | bool | None = None,
    direct_io_clean: bool | None = None,
    health_probe: Callable[[], bool | None] | None = None,
) -> HarnessDoctorReport:
    """Run non-mutating harness checks.

    ``sensor_results``, ``gold_gate_status`` and ``health_status`` are
    injectable so CI can remain offline and deterministic.  Missing evidence
    degrades the report; only unsafe commands or invalid workspace state block.
    """
    checks: list[HarnessCheck] = []
    degraded: list[str] = []
    blockers: list[str] = []

    def add(name: str, status: str, detail: str = "") -> None:
        checks.append(HarnessCheck(name=name, status=status, detail=detail))  # type: ignore[arg-type]
        if status == "degraded":
            degraded.append(f"{name}: {detail}" if detail else name)
        elif status == "blocked":
            blockers.append(f"{name}: {detail}" if detail else name)

    try:
        workspace = detect_workspace(path)
        root = Path(workspace.root_path)
    except Exception as exc:
        return HarnessDoctorReport(
            status="HARNESS_BLOCKED",
            workspace_path=str(Path(path).expanduser().resolve()),
            checks=[HarnessCheck("workspace", "blocked", f"{type(exc).__name__}: {exc}")],
            blockers=[f"workspace: {type(exc).__name__}: {exc}"],
        )

    try:
        guides = load_guides(workspace)
        if guides:
            add("project guides", "pass", f"{len(guides)} guide file(s)")
        else:
            add("project guides", "degraded", "no supported guide file found")
    except Exception as exc:
        guides = []
        add("project guides", "blocked", f"cannot load guides: {type(exc).__name__}: {exc}")

    merged_commands = guide_commands(guides)
    for key in ("build", "test", "lint"):
        values = getattr(merged_commands, key)
        add(
            f"guide {key} command",
            "pass" if values else "degraded",
            values[0] if values else "missing from project guides",
        )
    if unattended and not guides:
        add(
            "unattended guide context",
            "degraded",
            "unattended coding remains degraded without guides",
        )

    try:
        sensors = sensors_for_workspace(workspace, guides)
    except Exception as exc:
        sensors = []
        add("sensor registry", "blocked", f"cannot infer sensors: {type(exc).__name__}: {exc}")
    if not sensors:
        add("sensor registry", "degraded", "no computational sensors inferred")
    else:
        add("sensor registry", "pass", f"{len(sensors)} sensor(s)")
    for sensor in sensors:
        if sensor.command is None:
            add(
                f"sensor {sensor.id}",
                "degraded",
                "required sensor has no command" if sensor.required else "no command",
            )
            continue
        try:
            argv = parse_command(sensor.command)
            executable = Path(argv[0]).name
            safe = not command_requires_approval(argv)
            available = shutil.which(executable) is not None
        except ValueError as exc:
            add(f"sensor {sensor.id} safety", "blocked", str(exc))
            continue
        if not safe:
            add(
                f"sensor {sensor.id} safety",
                "blocked",
                "command requires approval and cannot be an unattended sensor",
            )
        elif sensor.required and not available:
            add(f"sensor {sensor.id} executable", "degraded", f"not found: {executable}")
        else:
            add(f"sensor {sensor.id} executable", "pass", executable)

    if sensor_results is not None:
        results = list(sensor_results)
        sensor_gate = sensor_acceptance(sensors, results)
        if sensor_gate["required_failures"]:
            add("required sensor evidence", "blocked", ", ".join(sensor_gate["required_failures"]))
        elif sensor_gate["insufficient_evidence"]:
            add(
                "required sensor evidence",
                "degraded",
                ", ".join(sensor_gate["insufficient_evidence"]),
            )
        else:
            add("required sensor evidence", "pass", "all required sensors passed")
    else:
        add("required sensor evidence", "degraded", "not run; doctor only verified availability")

    guide_text = "\n".join(rule.text.lower() for guide in guides for rule in guide.rules)
    permission_file = root / ".veya" / "harness" / "permissions.json"
    explicit_permissions = (
        permissions_explicit
        if permissions_explicit is not None
        else permission_file.is_file()
        or any(word in guide_text for word in ("permission", "权限", "approval", "批准"))
    )
    add(
        "permissions",
        "pass" if explicit_permissions else "degraded",
        "explicit workspace permission policy"
        if explicit_permissions
        else "no explicit permission policy found",
    )

    artifact_root = root / ".veya" / "runs"
    add("artifact path", "pass" if artifact_root.is_dir() else "degraded", str(artifact_root))

    clean, clean_detail = _git_clean(root)
    if clean is None:
        add("direct-IO baseline", "degraded", clean_detail)
        add("workspace uncommitted changes", "degraded", clean_detail)
    else:
        if direct_io_clean is not None:
            clean = direct_io_clean
        add("direct-IO baseline", "pass" if clean else "degraded", clean_detail)
        add("workspace uncommitted changes", "pass" if clean else "degraded", clean_detail)

    gold = (
        _status_value(gold_gate_status)
        if gold_gate_status is not None
        else _cached_gold_status(root)
    )
    add(
        "Personal Gold gate",
        "pass" if gold is True else "degraded",
        "PASS" if gold is True else "PASS evidence unavailable",
    )
    health = (
        _status_value(health_status)
        if health_status is not None
        else (health_probe() if health_probe is not None else _cached_health_status(root))
    )
    add(
        "public health",
        "pass" if health is True else "degraded",
        "PASS" if health is True else "health evidence unavailable",
    )

    status: Literal["HARNESS_READY", "HARNESS_DEGRADED", "HARNESS_BLOCKED"] = "HARNESS_READY"
    if blockers:
        status = "HARNESS_BLOCKED"
    elif degraded:
        status = "HARNESS_DEGRADED"
    return HarnessDoctorReport(
        status=status,
        workspace_path=str(root),
        checks=checks,
        degraded_reasons=degraded,
        blockers=blockers,
    )


__all__ = ["run_harness_doctor"]
