"""Veya 代码 Agent 可靠性适配层 (方案 A+C).

把 Veya 现有 generate 与 sandbox test 填进 veya_loop 的可靠性闭环:

    run_veya_code_agent(spec, tests, workspace, veya_generate, ...)
        → CodeLoopResult (merged_candidate | clarify | aborted)

veya_generate 契约 (规格 §5):
    veya_generate(spec=..., workspace=..., failure_context=..., tests=...) -> files dict
    修复轮 prompt 必须包含 failure_context 的 kind/summary/evidence[stderr_tail]/failed_nodeids。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from veya_loop.omodul.code_reliability_loop import (
    CodeLoopResult,
    TestResult,
)

from services.code_sandbox_client import CodeSandboxClient
from veya_loop import CodeTask, FailureSignature, PatchArtifact, run_code_reliability_loop

GenerateFn = Callable[[CodeTask, FailureSignature | None, PatchArtifact | None], PatchArtifact]


def adapt_veya_generate(veya_generate: Callable[..., dict[str, str]]) -> GenerateFn:
    """把 veya_generate(spec, workspace, failure_context=None, tests=None) 适配成 Loop 契约。"""

    def _gen(
        task: CodeTask, sig: FailureSignature | None, parent: PatchArtifact | None
    ) -> PatchArtifact:
        failure_context = None
        if sig is not None:
            failure_context = {
                "kind": sig.kind.value,
                "summary": sig.summary,
                "fingerprint": sig.fingerprint,
                "evidence": sig.evidence,
            }
        workspace = dict(task.workspace)
        if parent is not None:
            workspace.update(parent.files)
        files = veya_generate(
            spec=task.spec,
            workspace=workspace,
            failure_context=failure_context,
            tests=list(task.tests),
        )
        return PatchArtifact(
            patch_id=uuid.uuid4().hex[:8],
            files=files,
            parent_patch_id=parent.patch_id if parent else None,
            note=sig.kind.value if sig else "initial",
        )

    return _gen


def make_test_fn(client: CodeSandboxClient | None = None):
    """把 CodeSandboxClient.run 适配成 Loop 的 test_fn 契约。"""
    client = client or CodeSandboxClient()

    def _test(task: CodeTask, patch: PatchArtifact) -> TestResult:
        files = dict(task.workspace)
        files.update(patch.files)
        return client.run(files)

    return _test


def run_veya_code_agent(
    *,
    spec: str,
    tests: list[str],
    workspace: dict[str, str],
    veya_generate: Callable[..., dict[str, str]],
    spec_quality: float = 1.0,
    max_repairs: int = 3,
    sandbox: CodeSandboxClient | None = None,
    audit_path: str | None = None,
) -> CodeLoopResult:
    """跑可靠性闭环 (规格 §4.1 / §6.3)。"""
    task = CodeTask(
        task_id=uuid.uuid4().hex[:12],
        spec=spec,
        tests=tests,
        workspace=workspace,
        spec_quality=spec_quality,
        max_repairs=max_repairs,
    )
    return run_code_reliability_loop(
        task,
        generate_fn=adapt_veya_generate(veya_generate),
        test_fn=make_test_fn(sandbox),
        audit_path=audit_path,
    )
