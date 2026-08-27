"""veya_loop.omodul.video_reliability_loop — 视频质检可靠性闭环 (单一来源转发)。"""

from .._assembly import omodul as _load_omodul

_omodul = _load_omodul()

FailureKind = _omodul.video_reliability_loop.FailureKind
FailureSignature = _omodul.video_reliability_loop.FailureSignature
VideoArtifact = _omodul.video_reliability_loop.VideoArtifact
VideoEvalFn = _omodul.video_reliability_loop.VideoEvalFn
VideoEvalResult = _omodul.video_reliability_loop.VideoEvalResult
VideoGenerateFn = _omodul.video_reliability_loop.VideoGenerateFn
VideoLoopResult = _omodul.video_reliability_loop.VideoLoopResult
VideoSpec = _omodul.video_reliability_loop.VideoSpec
VideoTask = _omodul.video_reliability_loop.VideoTask
run_video_reliability_loop = _omodul.video_reliability_loop.run_video_reliability_loop

__all__ = [
    "FailureKind",
    "FailureSignature",
    "VideoArtifact",
    "VideoEvalFn",
    "VideoEvalResult",
    "VideoGenerateFn",
    "VideoLoopResult",
    "VideoSpec",
    "VideoTask",
    "run_video_reliability_loop",
]
