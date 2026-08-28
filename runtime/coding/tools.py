"""MasterAgent-facing local coding tools for PR-04.

The functions in this module are deliberately thin adapters over the product
layer.  They do not route user requests, create GoalRuns, or perform remote
GitHub operations.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .command_runner import CommandRunner
from .models import CommandResult, PatchArtifact, ToolResult, VerificationReport
from .sandbox_profiles import get_sandbox_profile
from .workspace_detect import detect_workspace
from .worktree import WorktreeError, WorktreeManager, repo_root_for_worktree, validate_task_id


def _result(
    status: str,
    *,
    data: dict[str, Any] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    command_results: list[CommandResult] | None = None,
    side_effect: bool = False,
    requires_approval: bool = False,
) -> dict[str, Any]:
    return ToolResult(
        status=status,  # type: ignore[arg-type]
        data=data or {},
        evidence=evidence or [],
        artifacts=artifacts or [],
        command_results=command_results or [],
        side_effect=side_effect,
        requires_approval=requires_approval,
    ).to_dict()


def _failed(message: str, *, requires_approval: bool = False) -> dict[str, Any]:
    return _result(
        "failed",
        evidence=[{"kind": "error", "message": message}],
        requires_approval=requires_approval,
    )


def _worktree_manager(worktree_path: str | Path) -> WorktreeManager:
    return WorktreeManager(repo_root_for_worktree(worktree_path))


def _task_id_from_path(worktree_path: str | Path) -> str:
    name = Path(worktree_path).expanduser().resolve().name
    if not name.startswith("task-"):
        raise WorktreeError("worktree directory must use the task-<id> naming convention")
    task_id = name.removeprefix("task-")
    return validate_task_id(task_id)


def _artifact_root(repo_root: Path, task_id: str) -> Path:
    validate_task_id(task_id)
    return (repo_root / ".veya" / "runs" / task_id / "outputs").resolve()


def _immutable_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_file() and path.read_text(encoding="utf-8") == content:
            return path
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
        path = path.with_name(f"{path.stem}-{digest}{path.suffix}")
        if path.exists() and path.read_text(encoding="utf-8") != content:
            raise FileExistsError(f"immutable artifact collision: {path}")
        if path.exists():
            return path
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
    return path


def coding_workspace_detect(
    path: str = ".",
    repo_url: str | None = None,
    owner_user_id: str = "local",
    hints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Detect a local coding workspace from known metadata without side effects."""
    try:
        workspace = detect_workspace(
            path,
            repo_url=repo_url or None,
            owner_user_id=owner_user_id,
            hints=hints,
        )
    except Exception as exc:
        return _failed(f"workspace detection failed: {type(exc).__name__}: {exc}")
    return _result(
        "ok",
        data={"workspace": workspace.to_dict()},
        evidence=[
            {
                "kind": "workspace_detection",
                "root_path": workspace.root_path,
                "provider": workspace.provider,
                "commands_inferred": True,
            }
        ],
    )


def coding_worktree_create(
    workspace_path: str,
    task_id: str,
    objective: str,
    base_ref: str | None = None,
    branch_name: str | None = None,
) -> dict[str, Any]:
    try:
        workspace = detect_workspace(workspace_path)
        record = WorktreeManager(workspace).create(
            task_id,
            objective,
            base_ref=base_ref or None,
            branch_name=branch_name or None,
        )
    except Exception as exc:
        return _failed(f"worktree create failed: {type(exc).__name__}: {exc}")
    return _result(
        "ok",
        data={"worktree": record.to_dict()},
        evidence=[
            {
                "kind": "worktree_created",
                "path": record.path,
                "branch_name": record.branch_name,
                "repo_root": record.repo_root,
            }
        ],
        side_effect=True,
    )


def coding_worktree_status(worktree_path: str) -> dict[str, Any]:
    try:
        record = _worktree_manager(worktree_path).status(path=worktree_path)
    except Exception as exc:
        return _failed(f"worktree status failed: {type(exc).__name__}: {exc}")
    return _result("ok", data={"worktree": record.to_dict()}, evidence=[record.to_dict()])


def coding_diff(worktree_path: str) -> dict[str, Any]:
    try:
        diff = _worktree_manager(worktree_path).diff(path=worktree_path)
    except Exception as exc:
        return _failed(f"coding diff failed: {type(exc).__name__}: {exc}")
    return _result(
        "ok",
        data=diff,
        evidence=[
            {
                "kind": "git_diff",
                "path": diff["path"],
                "branch_name": diff["branch_name"],
                "changed_files": diff["changed_files"],
            }
        ],
    )


def coding_apply_patch(worktree_path: str, patch: str) -> dict[str, Any]:
    """Apply a patch only inside a registered Veya worktree."""
    if not patch.strip():
        return _failed("patch must not be empty")
    if len(patch.encode("utf-8")) > 2_000_000:
        return _failed("patch exceeds the 2 MiB safety limit")
    try:
        manager = _worktree_manager(worktree_path)
        target = manager._assert_owned_path(worktree_path)
        manager._assert_registered(target)
        check = subprocess.run(
            ["git", "-C", str(target), "apply", "--check", "--binary", "-"],
            input=patch,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        if check.returncode != 0:
            return _failed(f"patch check failed: {(check.stderr or check.stdout).strip()[:2000]}")
        applied = subprocess.run(
            ["git", "-C", str(target), "apply", "--binary", "-"],
            input=patch,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        if applied.returncode != 0:
            return _failed(f"patch apply failed: {(applied.stderr or applied.stdout).strip()[:2000]}")
        record = manager.status(path=target)
    except Exception as exc:
        return _failed(f"patch apply failed: {type(exc).__name__}: {exc}")
    return _result(
        "ok",
        data={"worktree": record.to_dict()},
        evidence=[{"kind": "patch_applied", "path": record.path, "changed_files": record.changed_files}],
        side_effect=True,
    )


def coding_discard(worktree_path: str, confirm: bool = False, force: bool = False) -> dict[str, Any]:
    if not confirm:
        return _failed("discard requires explicit confirm=true", requires_approval=True)
    try:
        task_id = _task_id_from_path(worktree_path)
        manager = _worktree_manager(worktree_path)
        record = manager.discard(task_id, force=force)
    except Exception as exc:
        return _failed(f"worktree discard failed: {type(exc).__name__}: {exc}")
    return _result(
        "ok",
        data={"discarded": True, "worktree": record.to_dict()},
        evidence=[{"kind": "worktree_discarded", "path": record.path, "force": force}],
        side_effect=True,
    )


def coding_run_command(
    worktree_path: str,
    command: str,
    profile: str = "local_restricted",
    timeout_s: float = 900,
    approved: bool = False,
    network: str | None = None,
) -> dict[str, Any]:
    try:
        manager = _worktree_manager(worktree_path)
        target = manager._assert_owned_path(worktree_path)
        manager._assert_registered(target)
        task_id = _task_id_from_path(target)
        get_sandbox_profile(profile)
        runner = CommandRunner(
            target,
            profile=profile,
            artifact_root=_artifact_root(target, task_id),
        )
        command_result = runner.run(
            command,
            cwd=target,
            timeout_s=timeout_s,
            approved=approved,
            network=network,
        )
    except Exception as exc:
        return _failed(f"coding command failed: {type(exc).__name__}: {exc}")
    result_status = "ok" if command_result.status == "passed" else "failed"
    return _result(
        result_status,
        data={"task_id": task_id, "worktree_path": str(target), "profile": profile},
        evidence=[command_result.to_dict()],
        command_results=[command_result],
        side_effect=True,
        requires_approval=command_result.requires_approval,
    )


def _run_check(
    worktree_path: str,
    *,
    kind: str,
    command: str | None,
    profile: str,
    timeout_s: float,
    approved: bool,
) -> dict[str, Any]:
    try:
        manager = _worktree_manager(worktree_path)
        target = manager._assert_owned_path(worktree_path)
        manager._assert_registered(target)
        workspace = detect_workspace(manager.repo_root)
        candidates = getattr(workspace, f"{kind}_commands")
        selected = command or (candidates[0] if candidates else None)
        if not selected:
            return _failed(f"no inferred {kind} command; provide command explicitly")
        task_id = _task_id_from_path(target)
        runner = CommandRunner(
            target,
            profile=profile,
            artifact_root=_artifact_root(target, task_id),
        )
        command_result = runner.run(
            selected,
            cwd=target,
            timeout_s=timeout_s,
            approved=approved,
        )
    except Exception as exc:
        return _failed(f"{kind} check failed: {type(exc).__name__}: {exc}")
    return _result(
        "ok" if command_result.status == "passed" else "failed",
        data={"kind": kind, "command": selected, "worktree_path": str(target)},
        evidence=[command_result.to_dict()],
        command_results=[command_result],
        side_effect=True,
        requires_approval=command_result.requires_approval,
    )


def coding_run_tests(
    worktree_path: str,
    command: str | None = None,
    profile: str = "local_restricted",
    timeout_s: float = 900,
    approved: bool = False,
) -> dict[str, Any]:
    return _run_check(worktree_path, kind="test", command=command, profile=profile, timeout_s=timeout_s, approved=approved)


def coding_run_lint(
    worktree_path: str,
    command: str | None = None,
    profile: str = "local_restricted",
    timeout_s: float = 900,
    approved: bool = False,
) -> dict[str, Any]:
    return _run_check(worktree_path, kind="lint", command=command, profile=profile, timeout_s=timeout_s, approved=approved)


def coding_run_typecheck(
    worktree_path: str,
    command: str | None = None,
    profile: str = "local_restricted",
    timeout_s: float = 900,
    approved: bool = False,
) -> dict[str, Any]:
    return _run_check(
        worktree_path,
        kind="typecheck",
        command=command,
        profile=profile,
        timeout_s=timeout_s,
        approved=approved,
    )


def coding_build(
    worktree_path: str,
    command: str | None = None,
    profile: str = "local_restricted",
    timeout_s: float = 900,
    approved: bool = False,
) -> dict[str, Any]:
    return _run_check(worktree_path, kind="build", command=command, profile=profile, timeout_s=timeout_s, approved=approved)


def _write_artifact(store: Any, relative: str, content: str, *, kind: str, status: str = "draft") -> dict[str, Any]:
    target = _immutable_text(store.path(relative), content)
    ref = store.register(Path(target).relative_to(store.run_root), kind=kind, producer="coding", status=status)
    return ref.to_dict()


def coding_finalize_patch(
    worktree_path: str,
    verification: dict[str, Any] | None = None,
    run_id: str = "local-coding-run",
) -> dict[str, Any]:
    """Create immutable diff/report artifacts without committing or pushing."""
    try:
        manager = _worktree_manager(worktree_path)
        target = manager._assert_owned_path(worktree_path)
        manager._assert_registered(target)
        task_id = _task_id_from_path(target)
        diff = manager.diff(path=target)
        from runtime.execution.artifacts import ArtifactStore

        store = ArtifactStore(manager.repo_root, task_id)
        store.ensure_layout()
        patch_ref = _write_artifact(
            store,
            "outputs/patch.diff",
            str(diff["patch"]),
            kind="diff",
        )
        verification_data = verification or {}
        report = VerificationReport(
            id=f"verification-{uuid.uuid4().hex[:12]}",
            task_id=task_id,
            run_id=run_id,
            tests_passed=verification_data.get("tests_passed") if isinstance(verification_data.get("tests_passed"), bool) else None,
            lint_passed=verification_data.get("lint_passed") if isinstance(verification_data.get("lint_passed"), bool) else None,
            typecheck_passed=verification_data.get("typecheck_passed") if isinstance(verification_data.get("typecheck_passed"), bool) else None,
            build_passed=verification_data.get("build_passed") if isinstance(verification_data.get("build_passed"), bool) else None,
            acceptance_passed=bool(verification_data.get("acceptance_passed", False)),
            failed_checks=[str(item) for item in verification_data.get("failed_checks", []) if item],
        )
        report_json = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
        report_ref = _write_artifact(store, "outputs/verification_report.json", report_json, kind="test_report")
        summary_lines = [
            "# Coding patch finalization",
            "",
            f"- Task: `{task_id}`",
            f"- Worktree: `{target}`",
            f"- Branch: `{diff['branch_name']}`",
            f"- Changed files: {', '.join(diff['changed_files']) or '(none reported by Git)'}",
            "",
            "## Verification",
            f"- Tests: {report.tests_passed}",
            f"- Lint: {report.lint_passed}",
            f"- Typecheck: {report.typecheck_passed}",
            f"- Build: {report.build_passed}",
            f"- Acceptance: {report.acceptance_passed}",
            f"- Failed checks: {', '.join(report.failed_checks) or '(none recorded)'}",
            "",
            "## Diff stat",
            "```text",
            str(diff["stat"]).rstrip(),
            "```",
            "",
            "No commit or remote operation was performed.",
        ]
        summary_ref = _write_artifact(
            store,
            "outputs/summary.md",
            "\n".join(summary_lines) + "\n",
            kind="test_report",
        )
        manifest_path = store.write_manifest()
        patch = PatchArtifact(
            id=f"patch-{uuid.uuid4().hex[:12]}",
            task_id=task_id,
            run_id=run_id,
            kind="diff",
            path=patch_ref["path"],
            git_sha=_git_head(target),
            diff_summary=str(diff["stat"]),
            verified=report.acceptance_passed and not report.failed_checks,
            verification_ids=[report.id],
        )
    except Exception as exc:
        return _failed(f"patch finalization failed: {type(exc).__name__}: {exc}")
    return _result(
        "ok" if patch.verified else "partial",
        data={
            "task_id": task_id,
            "patch": patch.to_dict(),
            "verification_report": report.to_dict(),
            "manifest_path": str(manifest_path),
        },
        artifacts=[patch_ref, report_ref, summary_ref],
        evidence=[{"kind": "patch_finalized", "verified": patch.verified, "commit_performed": False}],
        side_effect=True,
    )


def _git_head(path: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def register_tools(registry: Any) -> int:
    """Register PR-04 tools into the existing MasterToolRegistry."""
    from server.tool_registry import SideEffect

    tools = [
        (
            "coding_workspace_detect",
            "只读识别本地 coding workspace、语言、包管理器和已知 test/lint/typecheck/build 命令；不初始化、不执行命令。",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目录路径，默认当前目录。"},
                    "repo_url": {"type": "string", "description": "可选的远端仓库 URL。"},
                    "owner_user_id": {"type": "string", "description": "工作区所有者标识。"},
                    "hints": {"type": "object", "description": "可选的显式命令/沙箱 profile 提示。"},
                },
            },
            coding_workspace_detect,
            SideEffect.PURE_READ,
        ),
        (
            "coding_worktree_create",
            "在 workspace/.veya/worktrees/task-<id> 创建隔离 Git 分支和 worktree；不会修改当前 worktree，也不会 push。",
            {
                "type": "object",
                "properties": {
                    "workspace_path": {"type": "string", "description": "Git workspace 目录。"},
                    "task_id": {"type": "string", "description": "单一安全 task 标识。"},
                    "objective": {"type": "string", "description": "任务目标，用于分支 slug。"},
                    "base_ref": {"type": "string", "description": "可选起始 ref，默认当前分支/HEAD。"},
                    "branch_name": {"type": "string", "description": "可选的安全分支名。"},
                },
                "required": ["workspace_path", "task_id", "objective"],
            },
            coding_worktree_create,
            SideEffect.LOCAL_WRITE,
        ),
        (
            "coding_worktree_status",
            "读取隔离 worktree 的分支、干净状态和 changed files。仅允许 .veya/worktrees 下的注册 worktree。",
            {"type": "object", "properties": {"worktree_path": {"type": "string"}}, "required": ["worktree_path"]},
            coding_worktree_status,
            SideEffect.PURE_READ,
        ),
        (
            "coding_diff",
            "读取隔离 worktree 相对 HEAD 的 unified diff、stat 和 changed files；不修改文件。",
            {"type": "object", "properties": {"worktree_path": {"type": "string"}}, "required": ["worktree_path"]},
            coding_diff,
            SideEffect.PURE_READ,
        ),
        (
            "coding_apply_patch",
            "在已注册隔离 worktree 内先 git apply --check 再应用 patch；拒绝 workspace 外路径。",
            {
                "type": "object",
                "properties": {
                    "worktree_path": {"type": "string"},
                    "patch": {"type": "string", "description": "unified diff patch，最大 2 MiB。"},
                },
                "required": ["worktree_path", "patch"],
            },
            coding_apply_patch,
            SideEffect.LOCAL_WRITE,
        ),
        (
            "coding_discard",
            "删除隔离 worktree；必须显式 confirm=true，脏 worktree 还必须 force=true。保留本地分支，不执行远程操作。",
            {
                "type": "object",
                "properties": {
                    "worktree_path": {"type": "string"},
                    "confirm": {"type": "boolean"},
                    "force": {"type": "boolean"},
                },
                "required": ["worktree_path", "confirm"],
            },
            coding_discard,
            SideEffect.LOCAL_WRITE,
        ),
        (
            "coding_run_command",
            "在隔离 worktree 中用指定 sandbox profile 执行一个 argv 命令；禁止 shell 运算符，输出脱敏并生成 command result artifact。",
            {
                "type": "object",
                "properties": {
                    "worktree_path": {"type": "string"},
                    "command": {"type": "string"},
                    "profile": {"type": "string", "enum": ["local_trusted", "local_restricted", "docker_python", "docker_node"]},
                    "timeout_s": {"type": "number"},
                    "approved": {"type": "boolean", "description": "仅显式批准破坏性/安装/远程命令。"},
                    "network": {"type": "string", "enum": ["none", "bridge", "host"]},
                },
                "required": ["worktree_path", "command"],
            },
            coding_run_command,
            SideEffect.PROCESS_EXEC,
        ),
    ]
    added = 0
    for name, description, parameters, function, side_effect in tools:
        if not registry.has(name):
            registry.register(
                name,
                description,
                parameters,
                function,
                max_result_chars=30000,
                side_effect=side_effect,
                effect_capability="manual_only" if side_effect is not SideEffect.PURE_READ else "none",
            )
            added += 1

    checks = [
        ("coding_run_tests", "运行推断或显式的测试命令并返回验证证据。", "test"),
        ("coding_run_lint", "运行推断或显式的 lint 命令并返回验证证据。", "lint"),
        ("coding_run_typecheck", "运行推断或显式的 typecheck 命令并返回验证证据。", "typecheck"),
        ("coding_build", "运行推断或显式的 build 命令并返回验证证据。", "build"),
    ]
    check_parameters = {
        "type": "object",
        "properties": {
            "worktree_path": {"type": "string"},
            "command": {"type": "string", "description": "可选；缺省使用 workspace detection 的第一条命令。"},
            "profile": {"type": "string", "enum": ["local_trusted", "local_restricted", "docker_python", "docker_node"]},
            "timeout_s": {"type": "number"},
            "approved": {"type": "boolean"},
        },
        "required": ["worktree_path"],
    }
    functions = {
        "coding_run_tests": coding_run_tests,
        "coding_run_lint": coding_run_lint,
        "coding_run_typecheck": coding_run_typecheck,
        "coding_build": coding_build,
    }
    for name, description, _ in checks:
        if not registry.has(name):
            registry.register(
                name,
                description,
                check_parameters,
                functions[name],
                max_result_chars=30000,
                side_effect=SideEffect.PROCESS_EXEC,
                effect_capability="manual_only",
            )
            added += 1
    if not registry.has("coding_finalize_patch"):
        registry.register(
            "coding_finalize_patch",
            "生成隔离 worktree 的 patch.diff、verification_report.json、summary.md 和 artifact manifest；只读 Git 状态并写 task-scoped artifacts，不 commit/push。",
            {
                "type": "object",
                "properties": {
                    "worktree_path": {"type": "string"},
                    "verification": {"type": "object", "description": "已实际运行检查的布尔结果和 failed_checks。"},
                    "run_id": {"type": "string"},
                },
                "required": ["worktree_path"],
            },
            coding_finalize_patch,
            max_result_chars=30000,
            side_effect=SideEffect.LOCAL_WRITE,
            effect_capability="manual_only",
        )
        added += 1
    return added


__all__ = [
    "coding_apply_patch",
    "coding_build",
    "coding_diff",
    "coding_discard",
    "coding_finalize_patch",
    "coding_run_command",
    "coding_run_lint",
    "coding_run_tests",
    "coding_run_typecheck",
    "coding_workspace_detect",
    "coding_worktree_create",
    "coding_worktree_status",
    "register_tools",
]
