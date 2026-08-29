"""Read-only Harness Doctor for coding workspaces."""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from runtime.coding.command_runner import parse_command
from runtime.coding.models import VerificationReport
from runtime.coding.workspace_detect import detect_workspace
from runtime.execution.artifacts import ArtifactStore

from .contract import build_coding_harness_contract, write_coding_harness_contract
from .guides import guide_commands, load_guides
from .models import HarnessCheck, HarnessDoctorReport, SensorResult
from .sensors import run_sensor, sensor_acceptance, sensor_command_is_safe, sensors_for_workspace

_PROTECTED_USER_FILES = frozenset(
    {
        "tests/test_inferera_free_pool.py",
        "veya/obase/_llm_config.py",
        "veya/obase/llm.py",
    }
)


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
    status_lines = [line for line in result.stdout.splitlines() if line.strip()]
    changed_paths = {
        line[3:].split(" -> ", 1)[-1].strip() for line in status_lines if len(line) >= 4
    }
    if changed_paths and changed_paths <= _PROTECTED_USER_FILES:
        return True, "only protected user files remain uncommitted; excluded from harness baseline"
    return not status_lines, "clean" if not status_lines else "uncommitted changes present"


def _command_available(root: Path, command: str) -> tuple[bool, str]:
    """Resolve a command without executing it, including repo-relative tools."""
    argv = parse_command(command)
    executable = Path(argv[0]).expanduser()
    if executable.parent != Path("."):
        candidate = executable if executable.is_absolute() else root / executable
        return candidate.is_file() and candidate.stat().st_mode & 0o111 != 0, str(candidate)
    resolved = shutil.which(argv[0])
    return resolved is not None, resolved or argv[0]


def _doctor_run_id() -> str:
    return "doctor-" + datetime.now(UTC).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(encoded, encoding="utf-8")
    temporary.replace(path)


def _sensor_rows(
    run_id: str,
    workspace_id: str,
    guide_sources: list[str],
    results: Iterable[SensorResult],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in results:
        row = result.to_dict()
        row.update(
            {
                "run_id": run_id,
                "workspace_id": workspace_id,
                "guide_sources": guide_sources,
                "sensor_id": result.sensor_id,
                "command": result.command,
                "status": result.status,
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "output_artifact": result.output_ref,
                "required": result.required,
                "deterministic": result.deterministic,
                "started_at": result.started_at,
                "completed_at": result.completed_at,
            }
        )
        rows.append(row)
    return rows


def _persist_sensor_evidence(
    root: Path,
    *,
    run_id: str,
    workspace_id: str,
    guide_sources: list[str],
    sensors: list[Any],
    results: list[SensorResult],
    acceptance: Mapping[str, Any],
    mode: str,
) -> tuple[Path, Path]:
    """Persist sensor and verification evidence through the existing artifact store."""
    store = ArtifactStore(root, run_id)
    store.ensure_layout()
    rows = _sensor_rows(run_id, workspace_id, guide_sources, results)
    sensor_report = {
        "run_id": run_id,
        "workspace_id": workspace_id,
        "guide_sources": guide_sources,
        "mode": mode,
        "sensors": [sensor.to_dict() for sensor in sensors],
        "results": rows,
        "deferred_sensor_ids": [
            sensor.id for sensor in sensors if sensor.id not in {item.sensor_id for item in results}
        ],
        **dict(acceptance),
    }
    sensor_path = store.path("outputs/sensor_report.json")
    _write_json(sensor_path, sensor_report)
    sensor_status = "verified" if acceptance["acceptance_passed"] else "partial"
    store.register(
        "outputs/sensor_report.json", kind="sensor_report", producer="harness", status=sensor_status
    )
    for result in results:
        if not result.output_ref:
            continue
        output_path = Path(result.output_ref).expanduser().resolve()
        try:
            relative = output_path.relative_to(store.run_root)
        except ValueError:
            continue
        if output_path.is_file():
            store.register(
                relative,
                kind="command_result",
                producer="harness",
                status="verified" if result.status == "passed" else "partial",
                evidence_ids=result.evidence_ids,
            )

    failed_checks = [
        *[f"sensor:{item}" for item in acceptance["required_failures"]],
        *[f"insufficient_evidence:{item}" for item in acceptance["insufficient_evidence"]],
    ]
    verification = VerificationReport(
        id="verification-" + uuid.uuid4().hex[:12],
        task_id=run_id,
        run_id=run_id,
        sensor_results=rows,
        acceptance_passed=bool(acceptance["acceptance_passed"]),
        failed_checks=failed_checks,
        tests_passed=all(
            result.status == "passed"
            for result in results
            if any(token in (result.command or "").lower() for token in ("pytest", "test"))
        )
        if any(
            any(token in (result.command or "").lower() for token in ("pytest", "test"))
            for result in results
        )
        else None,
        lint_passed=all(
            result.status == "passed"
            for result in results
            if any(token in (result.command or "").lower() for token in ("ruff", "lint"))
        )
        if any(
            any(token in (result.command or "").lower() for token in ("ruff", "lint"))
            for result in results
        )
        else None,
        typecheck_passed=all(
            result.status == "passed"
            for result in results
            if any(
                token in (result.command or "").lower() for token in ("mypy", "tsc", "typecheck")
            )
        )
        if any(
            any(token in (result.command or "").lower() for token in ("mypy", "tsc", "typecheck"))
            for result in results
        )
        else None,
        build_passed=all(
            result.status == "passed"
            for result in results
            if "build" in (result.command or "").lower()
        )
        if any("build" in (result.command or "").lower() for result in results)
        else None,
    )
    verification_path = store.path("outputs/verification_report.json")
    _write_json(verification_path, verification.to_dict())
    store.register(
        "outputs/verification_report.json",
        kind="verification_report",
        producer="harness",
        status=sensor_status,
    )
    store.write_manifest()
    return sensor_path, verification_path


def _read_sensor_evidence(root: Path, run_id: str) -> list[dict[str, Any]]:
    path = ArtifactStore(root, run_id).path("outputs/sensor_report.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    results = raw.get("results")
    if not isinstance(results, list):
        raise ValueError("sensor evidence results must be a list")
    return [item for item in results if isinstance(item, dict)]


def run_harness_doctor(
    path: str | Path = ".",
    *,
    unattended: bool = False,
    sensor_results: Iterable[SensorResult | Mapping[str, Any]] | None = None,
    run_sensors: bool = False,
    sensor_mode: Literal["quick", "full"] = "quick",
    doctor_run_id: str | None = None,
    sensor_profile: str = "local_trusted",
    permissions_explicit: bool | None = None,
    gold_gate_status: str | bool | None = None,
    health_status: str | bool | None = None,
    direct_io_clean: bool | None = None,
    health_probe: Callable[[], bool | None] | None = None,
) -> HarnessDoctorReport:
    """Run Harness checks, optionally executing safe deterministic sensors.

    ``sensor_results``, ``gold_gate_status`` and ``health_status`` are
    injectable so CI can remain offline and deterministic.  By default this
    function only diagnoses.  ``run_sensors`` is an explicit local execution
    request; it never runs LLM judges, network commands, installers, or code
    edits.
    """
    if sensor_mode not in {"quick", "full"}:
        raise ValueError(f"unsupported sensor mode: {sensor_mode}")
    checks: list[HarnessCheck] = []
    degraded: list[str] = []
    blockers: list[str] = []
    evidence_run_id: str | None = None
    sensor_report_path: str | None = None
    verification_report_path: str | None = None

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
            safe, safety_detail = sensor_command_is_safe(sensor)
            available, resolved = _command_available(root, sensor.command)
        except ValueError as exc:
            add(f"sensor {sensor.id} safety", "blocked", str(exc))
            continue
        if not safe:
            add(
                f"sensor {sensor.id} safety",
                "blocked",
                safety_detail,
            )
        elif not available:
            add(
                f"sensor {sensor.id} executable",
                "degraded",
                f"not found: {resolved or executable}",
            )
        else:
            add(f"sensor {sensor.id} executable", "pass", executable)

    selected_sensors = [
        sensor
        for sensor in sensors
        if sensor.deterministic
        and sensor.kind != "llm_judge"
        and sensor.command
        and (sensor_mode == "full" or sensor.cost_level in {"free", "low"})
    ]
    if run_sensors:
        evidence_run_id = doctor_run_id or _doctor_run_id()
        try:
            contract = build_coding_harness_contract(
                workspace,
                evidence_run_id,
                guides=guides,
                sensors=sensors,
            )
            contract_file = write_coding_harness_contract(root, evidence_run_id, contract)
            add("harness contract", "pass", str(contract_file))
        except Exception as exc:
            add(
                "harness contract",
                "blocked",
                f"cannot create contract: {type(exc).__name__}: {exc}",
            )

        run_results: list[SensorResult] = []
        for sensor in selected_sensors:
            try:
                run_results.append(
                    run_sensor(
                        sensor,
                        root,
                        profile=sensor_profile,
                        run_id=evidence_run_id,
                        require_worktree=False,
                    )
                )
            except Exception as exc:
                run_results.append(
                    SensorResult(
                        sensor_id=sensor.id,
                        status="error",
                        exit_code=None,
                        output_ref=None,
                        evidence_ids=["sensor-execution-error-" + uuid.uuid4().hex[:12]],
                        message=f"{type(exc).__name__}: {exc}",
                        command=sensor.command,
                        required=sensor.required,
                        deterministic=sensor.deterministic,
                        started_at=datetime.now(UTC).isoformat(),
                        completed_at=datetime.now(UTC).isoformat(),
                    )
                )
        # Persist first, then re-read the artifact.  Read-back, not the
        # subprocess return values, is the source for the readiness decision.
        try:
            artifact_root = root / ".veya" / "runs"
            artifact_root.mkdir(parents=True, exist_ok=True)
            acceptance = sensor_acceptance(selected_sensors, run_results)
            sensor_path, verification_path = _persist_sensor_evidence(
                root,
                run_id=evidence_run_id,
                workspace_id=workspace.id,
                guide_sources=[guide.source_path for guide in guides],
                sensors=sensors,
                results=run_results,
                acceptance=acceptance,
                mode=sensor_mode,
            )
            sensor_report_path = str(sensor_path)
            verification_report_path = str(verification_path)
            persisted_results = _read_sensor_evidence(root, evidence_run_id)
            persisted_acceptance = sensor_acceptance(selected_sensors, persisted_results)
        except Exception as exc:
            persisted_acceptance = {
                "acceptance_passed": False,
                "required_failures": [],
                "insufficient_evidence": [],
            }
            add(
                "sensor evidence persistence",
                "blocked",
                f"cannot persist/read evidence: {type(exc).__name__}: {exc}",
            )
        else:
            if not selected_sensors:
                add(
                    "required sensor evidence", "degraded", "no safe deterministic sensors selected"
                )
            elif persisted_acceptance["required_failures"]:
                add(
                    "required sensor evidence",
                    "blocked",
                    ", ".join(persisted_acceptance["required_failures"]),
                )
            elif persisted_acceptance["insufficient_evidence"]:
                add(
                    "required sensor evidence",
                    "degraded",
                    ", ".join(persisted_acceptance["insufficient_evidence"]),
                )
            else:
                add(
                    "required sensor evidence",
                    "pass",
                    f"{len(persisted_results)} result(s) reloaded from evidence",
                )
    elif sensor_results is not None:
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
    if run_sensors:
        artifact_root.mkdir(parents=True, exist_ok=True)
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
        evidence_run_id=evidence_run_id,
        sensor_report_path=sensor_report_path,
        verification_report_path=verification_report_path,
    )


__all__ = ["run_harness_doctor"]
