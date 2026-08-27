"""veya_loop.omodul.code_reliability_loop — 代码可靠性闭环 (单一来源转发)。"""

from .._assembly import omodul as _load_omodul

_omodul = _load_omodul()

CodeLoopResult = _omodul.code_reliability_loop.CodeLoopResult
CodeTask = _omodul.code_reliability_loop.CodeTask
FailureKind = _omodul.code_reliability_loop.FailureKind
FailureSignature = _omodul.code_reliability_loop.FailureSignature
PatchArtifact = _omodul.code_reliability_loop.PatchArtifact
TestResult = _omodul.code_reliability_loop.TestResult
run_code_reliability_loop = _omodul.code_reliability_loop.run_code_reliability_loop

__all__ = [
    "CodeLoopResult",
    "CodeTask",
    "FailureKind",
    "FailureSignature",
    "PatchArtifact",
    "TestResult",
    "run_code_reliability_loop",
]
