"""video_agent_reliability — 视频生产 Agent 的可靠性闭环适配。

把 veya-video-sandbox 客户端 + hevi 生成函数接进 run_video_reliability_loop:
    generate_fn  (hevi/可灵/即梦/本地管线, 可先 stub)
    evaluate_fn  (VideoSandboxClient / LocalVideoEvaluator)
    → passed: merged_candidate (待人工发布) | clarify | aborted
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from veya_loop.omodul.video_reliability_loop import (
    FailureSignature,
    VideoArtifact,
    VideoEvalResult,
    VideoGenerateFn,
    VideoSpec,
    VideoTask,
    run_video_reliability_loop,
)
from video_sandbox_client import LocalVideoEvaluator, make_eval_fn


def run_veya_video_agent(
    *,
    prompt: str,
    platform_spec: dict[str, Any],
    generate_with_hevi: Callable[..., Path],
    max_repairs: int = 3,
    task_id: str = "veya_video_1",
    workspace: dict[str, str] | None = None,
    evaluate_fn: Callable[[Any, Any], VideoEvalResult] | None = None,
) -> Any:
    """视频生产 Agent 入口: hevi 出片 → 沙箱质检 → 有限次返工。

    generate_with_hevi(prompt, task, signature, parent) -> Path (本地视频路径)
    先 stub 后接真实 hevi 出片路径 (见 adapt_hevi)。
    """
    task = VideoTask(
        task_id=task_id,
        prompt=prompt,
        spec=VideoSpec(**{k: v for k, v in platform_spec.items() if k in
                          ("min_duration_s", "max_duration_s", "min_width",
                           "min_height", "aspect_ratios", "require_audio",
                           "max_size_mb", "platform")}),
        workspace=workspace or {},
        max_repairs=max_repairs,
    )
    if evaluate_fn is None:
        # 优先 Docker 沙箱; 无 Docker 回退本地 evaluate.py。
        try:
            import shutil

            if shutil.which("docker"):
                from video_sandbox_client import VideoSandboxClient

                eval_fn = make_eval_fn(VideoSandboxClient())
            else:
                eval_fn = make_eval_fn(LocalVideoEvaluator())
        except Exception:
            eval_fn = make_eval_fn(LocalVideoEvaluator())

    return run_video_reliability_loop(
        task,
        generate_fn=adapt_hevi(generate_with_hevi),
        evaluate_fn=eval_fn,
    )


def adapt_hevi(generate_with_hevi: Callable[..., Path]) -> VideoGenerateFn:
    """把 hevi 生成函数适配为闭环 generate_fn 契约。

    hevi 函数签名: generate_with_hevi(prompt, failure_context) -> Path
    failure_context 来自失败签名 (时长不够/要 9:16/无音轨等)。
    """

    def _generate(
        task: VideoTask,
        signature: FailureSignature | None,
        parent: VideoArtifact | None,
    ) -> VideoArtifact:
        failure_context: dict[str, Any] = {}
        if signature is not None:
            failure_context = {
                "kind": signature.kind.value,
                "summary": signature.summary,
                "preferred_action": signature.preferred_action,
                "fingerprint": signature.fingerprint,
            }
        # 修正提示: 把规格约束显式带给生成侧 (时长/比例/画质)。
        adjusted_prompt = _adjusted_prompt(task, failure_context)
        path = generate_with_hevi(adjusted_prompt, failure_context)
        provider = getattr(generate_with_hevi, "_hevi_provider", "hevi")
        return VideoArtifact(
            video_id=_next_video_id(task.task_id, failure_context),
            video_path=str(path),
            parent_id=parent.video_id if parent else None,
            provider=provider,
            note=failure_context.get("summary", ""),
            failure_context=failure_context,
        )

    return _generate


def _next_video_id(task_id: str, failure_context: dict[str, Any]) -> str:
    """确定性 video_id: 无失败=首轮, 有失败=签名指纹后 6 位。"""
    fp = failure_context.get("fingerprint") or ""
    return f"{task_id}_v{fp[:6]}" if fp else f"{task_id}_v0"


def _adjusted_prompt(task: VideoTask, failure_context: dict[str, Any]) -> str:
    """把规格 + 失败签名合成为修正提示。"""
    spec = task.spec
    constraints = (
        f"时长 {spec.min_duration_s}-{spec.max_duration_s}s; "
        f"分辨率 ≥{spec.min_width}x{spec.min_height}; "
        f"比例 {'/'.join(spec.aspect_ratios)}"
    )
    hint = ""
    kind = failure_context.get("kind")
    if kind == "spec_or_duration":
        hint = " (注意: 上次时长不符合规格, 请按目标时长生成)"
    elif kind == "format":
        hint = " (注意: 上次分辨率/比例不合格, 请严格按画幅规格)"
    elif kind == "audio":
        hint = " (注意: 上次缺少音轨, 必须包含音频)"
    return f"{task.prompt} {constraints}{hint}"


__all__ = ["adapt_hevi", "run_veya_video_agent"]
