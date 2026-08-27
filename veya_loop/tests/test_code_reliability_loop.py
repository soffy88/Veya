"""代码可靠性闭环单测 — 规格 §6.1。

覆盖 §6.4 产品行为:
  - 测试全过 → merged_candidate + patch
  - 规格质量低 → clarify, 不写危险补丁
  - 连续失败超 max_repairs → aborted + signature + trace
  - 沙箱超时/env 错误 → signature 标记, 不崩主进程
  - 修复轮带 failure_context (kind/summary/fingerprint/evidence)
  - 审计事件落 JSONL
"""

from __future__ import annotations

import json

from veya_loop.omodul.code_reliability_loop import (
    PatchArtifact,
    TestResult,
)

from veya_loop import CodeTask, FailureKind, run_code_reliability_loop


def _patch(files, note="initial"):
    return PatchArtifact(patch_id=note, files=files, note=note)


def test_merged_candidate_on_first_pass():
    """测试全过 → merged_candidate + patch。"""
    task = CodeTask(task_id="t1", spec="return 42", tests=["test_solve"])
    generated = []

    def gen(task_, sig, parent):
        generated.append(sig)
        return _patch({"main.py": "def solve():\n    return 42\n"}, "g1")

    def tst(task_, patch):
        return TestResult(passed=True, n_passed=1)

    r = run_code_reliability_loop(task, gen, tst)
    assert r.status == "merged_candidate" and r.success
    assert r.patch.files["main.py"] == "def solve():\n    return 42\n"
    assert r.repairs_used == 0
    assert [a["action"] for a in r.action_trace] == ["generate", "test", "merged_candidate"]


def test_repair_rounds_until_pass():
    """首轮失败 → 修复轮 (带 failure_context) → 全过。"""
    task = CodeTask(task_id="t2", spec="return 42", tests=["test_solve"], max_repairs=3)
    sigs_seen = []

    def gen(task_, sig, parent):
        sigs_seen.append(sig)
        if sig is None:  # 首轮故意错
            return _patch({"main.py": "def solve():\n    return 0\n"}, "g0")
        assert sig.kind == FailureKind.TEST_FAILURE
        assert sig.fingerprint and sig.evidence["stderr_tail"]
        assert sig.evidence["failed_nodeids"] == ["test_solve"]
        return _patch({"main.py": "def solve():\n    return 42\n"}, f"g{sig.fingerprint[:4]}")

    def tst(task_, patch):
        if "return 42" in patch.files["main.py"]:
            return TestResult(passed=True, n_passed=1)
        return TestResult(
            passed=False,
            n_failed=1,
            stderr="AssertionError: 0 != 42",
            failed_nodeids=["test_solve"],
        )

    r = run_code_reliability_loop(task, gen, tst)
    assert r.status == "merged_candidate" and r.success
    assert r.repairs_used == 1
    # 初始生成 sig=None, 修复轮 sig 带完整失败上下文
    assert len(sigs_seen) == 2 and sigs_seen[0] is None and sigs_seen[1] is not None
    actions = [a["action"] for a in r.action_trace]
    assert actions == ["generate", "test", "repair", "test", "merged_candidate"]


def test_aborted_after_max_repairs():
    """连续失败超 max_repairs → aborted + signature + trace。"""
    task = CodeTask(task_id="t3", spec="x", tests=["t"], max_repairs=2)
    calls = []

    def gen(task_, sig, parent):
        calls.append(sig)
        return _patch({"main.py": "def solve():\n    return 0\n"}, f"g{len(calls)}")

    def tst(task_, patch):
        return TestResult(passed=False, n_failed=1, stderr="boom", failed_nodeids=["t"])

    r = run_code_reliability_loop(task, gen, tst)
    assert r.status == "aborted" and not r.success
    assert r.repairs_used == 2  # 首轮 + 2 修复 = 3 次生成
    assert len(calls) == 3
    assert r.signature is not None and r.signature.kind == FailureKind.TEST_FAILURE
    assert r.signature.fingerprint
    last = r.action_trace[-1]
    assert last["action"] == "aborted" and "max_repairs" in last["reason"]


def test_clarify_on_low_spec_quality():
    """规格质量低 → clarify, 不调用生成 (不写危险补丁)。"""
    task = CodeTask(task_id="t4", spec="vague", tests=["t"], spec_quality=0.3)
    called = []

    def gen(task_, sig, parent):
        called.append(1)
        return _patch({})

    r = run_code_reliability_loop(task, gen, lambda t, p: TestResult(passed=True))
    assert r.status == "clarify" and not r.success
    assert r.clarify_message and "0.30" in r.clarify_message
    assert called == []  # 生成未被调用


def test_sandbox_timeout_signature():
    """沙箱超时 → signature.kind=timeout, 不崩主进程。"""
    task = CodeTask(task_id="t5", spec="x", tests=["t"], max_repairs=1)

    def gen(task_, sig, parent):
        return _patch({"main.py": "x"})

    def tst(task_, patch):
        return TestResult(
            passed=False, n_failed=1, stderr="timeout after 60s", metadata={"timeout": True}
        )

    r = run_code_reliability_loop(task, gen, tst)
    assert r.status == "aborted"
    assert r.signature.kind == FailureKind.TIMEOUT


def test_sandbox_raising_test_fn_does_not_crash():
    """test_fn 抛异常 → env_error 签名, Loop 继续到 aborted。"""
    task = CodeTask(task_id="t6", spec="x", tests=["t"], max_repairs=1)

    def gen(task_, sig, parent):
        return _patch({"main.py": "x"})

    def tst(task_, patch):
        raise RuntimeError("docker daemon unreachable")

    r = run_code_reliability_loop(task, gen, tst)
    assert r.status == "aborted"
    assert r.signature.kind == FailureKind.ENV_ERROR
    assert "sandbox raised" in r.signature.evidence["stderr_tail"]


def test_audit_jsonl_written(tmp_path):
    """审计事件落 JSONL (修复动作可审计)。"""
    task = CodeTask(task_id="t7", spec="x", tests=["t"], max_repairs=1)
    audit = str(tmp_path / "audit.jsonl")

    def gen(task_, sig, parent):
        return _patch({"main.py": "x"}, "g0")

    def tst(task_, patch):
        return TestResult(passed=False, n_failed=1, stderr="e", failed_nodeids=["t"])

    r = run_code_reliability_loop(task, gen, tst, audit_path=audit)
    assert r.status == "aborted"
    lines = [json.loads(ln) for ln in (tmp_path / "audit.jsonl").read_text().strip().splitlines()]
    assert lines
    assert all(e["trace_id"] == "code_loop_t7" for e in lines)
    # 审计含 execute 事件 (generate/test/repair/aborted)
    assert any("code_loop:test" in e["execution"]["primitive"] for e in lines)
    assert any("code_loop:aborted" in e["execution"]["primitive"] for e in lines)
