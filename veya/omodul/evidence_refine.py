"""veya/omodul/evidence_refine — omodul_evidence_refine（代码生成防线）。

模型生成复杂代码时：先静态检查 → 沙箱执行验证 → 产出证据（错误信息）
供 agent_loop 把证据喂回模型自我修复。

注入:
    sandbox — VfsSandbox 句柄（默认 container 全局句柄；执行验证经
              oprim.shell_run_script，只发生在沙盒内）
    barrier — EventBarrier 句柄（emit evidence 事件）

流程:
    verify(code):
        ast_parse.syntax_check          → 语法证据（不执行）
        oprim.shell_run_script 沙箱执行  → 运行时证据（stdout/stderr）
        build_fix_hint(error, code)     → 给模型的修复提示（纯函数）

事件流: evidence_refine.verify / evidence_refine.result
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from veya.oprim.event import emit_event
from veya.oprim.shell import shell_run_script
from veya.oskill.pure.ast_parse import structure_summary, syntax_check


@dataclass
class RefineResult:
    """验证结果：ok=False 时 error/evidence 给模型做自我修复输入。"""

    ok: bool
    error: str = ""
    evidence: str = ""
    output: str = ""
    iterations: int = 1
    structure: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "error": self.error,
            "evidence": self.evidence,
            "output": self.output,
            "iterations": self.iterations,
            "structure": self.structure,
        }


class EvidenceRefine:
    """代码证据链：静态检查 + 沙箱执行 + 修复提示。"""

    def __init__(self, *, sandbox: Any = None, barrier: Any = None) -> None:
        self._sandbox = sandbox
        self._barrier = barrier

    async def verify(self, code: str, *, language: str = "python", max_iterations: int = 3) -> RefineResult:
        """验证代码。language='python' 时先 AST 静态检查再沙箱执行。"""
        if not isinstance(code, str) or not code.strip():
            return RefineResult(ok=False, error="代码为空", evidence="空代码块")

        emit_event("evidence_refine.verify", {"language": language}, barrier=self._barrier)

        # 1. 静态检查（不执行）
        if language == "python":
            ok, err = syntax_check(code)
            if not ok:
                result = RefineResult(
                    ok=False, error=f"语法检查失败: {err}",
                    evidence=f"SYNTAX ERROR\n{err}",
                    structure={"has_syntax_error": True},
                )
                emit_event("evidence_refine.result", {"ok": False, "stage": "syntax"}, barrier=self._barrier)
                return result
            structure = structure_summary(code)
        else:
            structure = {}

        # 2. 沙箱执行验证（物理触手，只发生在 VfsSandbox 内）
        evidence_lines: list[str] = []
        output = ""
        for attempt in range(1, max_iterations + 1):
            res = await shell_run_script(code, sandbox=self._sandbox)
            output = res.stdout
            if res.ok:
                emit_event(
                    "evidence_refine.result",
                    {"ok": True, "stage": "exec", "attempts": attempt},
                    barrier=self._barrier,
                )
                return RefineResult(
                    ok=True, output=output, iterations=attempt, structure=structure
                )
            # 失败 → 收集证据（stderr 优先）
            detail = res.stderr.strip() or f"exit={res.exit_code}"
            evidence_lines.append(f"ATTEMPT {attempt}:\n{detail}")
            # 语法类错误重复执行无意义，直接返回
            if "SyntaxError" in detail or "IndentationError" in detail:
                break

        evidence = "\n\n".join(evidence_lines)
        result = RefineResult(
            ok=False,
            error=f"沙箱执行失败 ({language})",
            evidence=evidence,
            output=output,
            iterations=min(len(evidence_lines), max_iterations),
            structure=structure,
        )
        emit_event("evidence_refine.result", {"ok": False, "stage": "exec"}, barrier=self._barrier)
        return result

    @staticmethod
    def build_fix_hint(error: str, code: str = "", *, max_code_chars: int = 800) -> str:
        """构造给模型的修复提示（纯函数）：错误证据 + 代码片段。

        agent_loop 把该提示并入下一轮 LLM 消息即完成「错误反馈 → 自我修复」闭环。
        """
        hint = (
            "你上一次生成的代码未通过验证。请根据以下证据修复代码, "
            "只输出修复后的完整代码:\n\n"
        )
        if code:
            snippet = code[:max_code_chars]
            hint += f"--- 待修复代码 ---\n```\n{snippet}\n```\n\n"
        hint += f"--- 验证证据 ---\n{error}\n"
        return hint


__all__ = ["EvidenceRefine", "RefineResult"]
