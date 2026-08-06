"""视频质检可靠性闭环单测 — 对齐 VIDEO_QA_RELIABILITY_CC_SPEC。

覆盖产品行为:
  - 首轮合格 → merged_candidate (待人工发布, 不自动发布)
  - 规格矛盾 (min>max) → clarify
  - 连续失败超 max_repairs → aborted + signature + trace (禁止无预算无限重生成)
  - 沙箱挂 (SANDBOX_ERROR) → ENV 签名, 不崩主进程
  - 返工轮带 failure_context (kind/fingerprint/preferred_action)
  - 失败签名映射 (DURATION_* → SPEC_OR_DURATION / NO_AUDIO → AUDIO)
"""

from __future__ import annotations

from veya_loop.omodul.video_reliability_loop import (
    FailureKind,
    VideoArtifact,
    VideoEvalResult,
    VideoSpec,
    VideoTask,
    run_video_reliability_loop,
)


def _artifact(path="video.mp4", note="initial"):
    return VideoArtifact(video_id=note, video_path=path, note=note)


def _eval(passed: bool, issues=None, **kw):
    return VideoEvalResult(passed=passed, issues=issues or [], **kw)


def test_merged_candidate_on_first_pass():
    """首轮合格 → merged_candidate + artifact (待人工发布)。"""
    task = VideoTask(task_id="v1", prompt="蓝色方块",
                     spec=VideoSpec(min_duration_s=5.0, max_duration_s=60.0))
    generated = []

    def gen(task_, sig, parent):
        generated.append(sig)
        return _artifact("ok.mp4", "g1")

    def evl(task_, artifact):
        return _eval(True, duration_s=8.0, width=1280, height=720, has_audio=True)

    r = run_video_reliability_loop(task, gen, evl)
    assert r.status == "merged_candidate"
    assert r.success is True
    assert r.artifact.video_id == "g1"
    assert r.repairs_used == 0
    assert generated == [None]  # 只生成一次, 无签名


def test_clarify_on_spec_contradiction():
    """规格矛盾 (min>max) → clarify, 不生成。"""
    task = VideoTask(task_id="v2", prompt="x",
                     spec=VideoSpec(min_duration_s=60.0, max_duration_s=5.0))
    generated = []

    def gen(task_, sig, parent):
        generated.append(sig)
        return _artifact()

    r = run_video_reliability_loop(task, gen, lambda t, a: _eval(True))
    assert r.status == "clarify"
    assert r.success is False
    assert "矛盾" in (r.clarify_message or "")
    assert generated == []


def test_abort_after_max_repairs():
    """连续不合格 → 修完 max_repairs 后 ABORT (禁止无预算无限重生成)。"""
    task = VideoTask(task_id="v3", prompt="x",
                     spec=VideoSpec(min_duration_s=5.0), max_repairs=2)
    calls = []

    def gen(task_, sig, parent):
        calls.append(sig)
        return _artifact(f"gen{len(calls)}", f"g{len(calls)}")

    def evl(task_, artifact):
        return _eval(False, issues=[{"code": "DURATION_TOO_SHORT",
                                     "message": "2s < 5s", "severity": "high"}])

    r = run_video_reliability_loop(task, gen, evl)
    assert r.status == "aborted"
    assert r.success is False
    assert r.repairs_used == 2          # max_repairs 耗尽
    assert r.signature is not None
    assert r.signature.kind == FailureKind.SPEC_OR_DURATION
    assert r.signature.preferred_action == "ADJUST_PROMPT"
    assert len(r.action_trace) >= 5     # generate + 2×(evaluate+repair) + aborted
    # 生成次数 = 1 初始 + 2 修复 ≤ max_repairs+1
    assert len(calls) == 3


def test_signature_mapping_audio():
    """NO_AUDIO → FailureKind.AUDIO → REGENERATE。"""
    task = VideoTask(task_id="v4", prompt="x",
                     spec=VideoSpec(require_audio=True), max_repairs=1)
    actions = []

    def gen(task_, sig, parent):
        actions.append(sig.preferred_action if sig else None)
        return _artifact()

    def evl(task_, artifact):
        return _eval(False, issues=[{"code": "NO_AUDIO", "message": "无音轨",
                                     "severity": "high"}])

    r = run_video_reliability_loop(task, gen, evl)
    assert r.status == "aborted"
    assert r.signature.kind == FailureKind.AUDIO
    assert r.signature.preferred_action == "REGENERATE"
    assert actions[1] == "REGENERATE"


def test_sandbox_crash_is_env_signature():
    """沙箱 evaluate_fn 抛异常 → ENV 签名, 不崩主进程。"""
    task = VideoTask(task_id="v5", prompt="x",
                     spec=VideoSpec(min_duration_s=5.0), max_repairs=1)

    def gen(task_, sig, parent):
        return _artifact()

    def evl(task_, artifact):
        raise RuntimeError("docker 不可用")

    r = run_video_reliability_loop(task, gen, evl)
    assert r.status == "aborted"
    assert r.signature is not None
    assert r.signature.kind == FailureKind.ENV
    assert "sandbox raised" in r.signature.summary


def test_repair_round_carries_failure_context():
    """修复轮 generate_fn 收到 failure_context (kind/summary/fingerprint)。"""
    task = VideoTask(task_id="v6", prompt="x",
                     spec=VideoSpec(min_width=720), max_repairs=1)
    signatures = []

    def gen(task_, sig, parent):
        signatures.append(sig)
        return _artifact()

    def evl(task_, artifact):
        return _eval(False, issues=[{"code": "RESOLUTION_LOW",
                                     "message": "480 < 720", "severity": "high"}])

    run_video_reliability_loop(task, gen, evl)
    assert len(signatures) == 2
    sig = signatures[1]
    assert sig.kind == FailureKind.FORMAT
    assert sig.fingerprint
    assert sig.evidence["issues"][0]["code"] == "RESOLUTION_LOW"


def test_passed_after_one_repair():
    """一次返工后合格 → merged_candidate + repairs_used=1。"""
    task = VideoTask(task_id="v7", prompt="x",
                     spec=VideoSpec(min_duration_s=5.0), max_repairs=3)
    counter = {"n": 0}

    def gen(task_, sig, parent):
        counter["n"] += 1
        return _artifact(f"g{counter['n']}")

    def evl(task_, artifact):
        if counter["n"] == 1:
            return _eval(False, issues=[{"code": "DURATION_TOO_SHORT",
                                         "message": "3s < 5s", "severity": "high"}])
        return _eval(True, duration_s=6.0, width=1280, height=720, has_audio=True)

    r = run_video_reliability_loop(task, gen, evl)
    assert r.status == "merged_candidate"
    assert r.success is True
    assert r.repairs_used == 1
