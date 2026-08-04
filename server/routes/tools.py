"""
智能工具执行 API - P1 核心能力
提供安全的工具执行、工具建议等功能
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from hicode.tools import create_tool_executor

router = APIRouter(prefix="/tools", tags=["tools"])

# 全局工具执行器
tool_executor = create_tool_executor()


class ToolExecuteRequest(BaseModel):
    tool: str
    params: dict[str, Any]
    use_sandbox: bool | None = True


class SandboxExecuteRequest(BaseModel):
    command: str
    script: str | None = None
    memory_limit: int | None = 100 * 1024 * 1024  # 100MB
    time_limit: float | None = 30.0
    network_blocked: bool | None = True


@router.post("/execute")
async def execute_tool(request: ToolExecuteRequest) -> dict[str, Any]:
    """安全执行工具"""
    try:
        result = await tool_executor.execute_tool(request.tool, **request.params)

        # 如果需要沙箱且工具执行失败
        if (
            request.use_sandbox
            and result.status.value == "failed"
            and "unsafe" in result.error.lower()
        ):
            from hicode.sandbox import SandboxConfig, create_safe_executor

            config = SandboxConfig(
                memory_limit=100 * 1024 * 1024, time_limit=30.0, network_blocked=True
            )
            executor = create_safe_executor(config)
            await executor.start()
            try:
                if request.tool == "terminal" or request.tool == "git":
                    sandbox_result = await executor.execute(request.params.get("command", ""))
                else:
                    return {"status": "failed", "error": "Sandbox not supported for this tool"}

                return {
                    "status": "success" if sandbox_result["exit_code"] == 0 else "failed",
                    "output": sandbox_result.get("stdout", ""),
                    "error": sandbox_result.get("stderr", ""),
                    "sandboxed": True,
                    "exit_code": sandbox_result["exit_code"],
                }
            finally:
                await executor.stop()

        return {
            "status": result.status.value,
            "output": result.output,
            "error": result.error,
            "duration": result.duration,
            "suggestions": result.suggestions,
            "sandboxed": False,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Tool execution failed: {e!s}")


@router.post("/execute-parallel")
async def execute_tools_parallel(requests: list[ToolExecuteRequest]) -> dict[str, Any]:
    """并行执行多个工具"""
    try:
        tools = [(r.tool, r.params) for r in requests]
        results = await tool_executor.execute_all(tools)

        return {
            "status": "success",
            "count": len(results),
            "results": [
                {
                    "tool": r.command if hasattr(r, "command") else r.tool,
                    "status": r.status.value,
                    "output": r.output,
                    "error": r.error,
                    "duration": r.duration,
                    "suggestions": r.suggestions,
                }
                for r in results
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Parallel tool execution failed: {e!s}")


@router.get("/suggestions")
async def get_tool_suggestions(context: str = "") -> dict[str, Any]:
    """获取工具建议"""
    try:
        suggestions = tool_executor.get_tool_suggestions(context)
        return {"status": "success", "context": context, "suggestions": suggestions}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get suggestions: {e!s}")


@router.get("/{tool_name}/history")
async def get_tool_history(tool_name: str, limit: int = 10) -> dict[str, Any]:
    """获取工具执行历史"""
    try:
        tool = tool_executor.tools.get(tool_name)
        if not tool:
            raise HTTPException(status_code=404, detail=f"Tool not found: {tool_name}")

        history = tool.get_history()[-limit:]
        return {
            "status": "success",
            "tool": tool_name,
            "count": len(history),
            "history": [
                {
                    "command": h.command,
                    "status": h.status.value,
                    "output": h.output[:200] if h.output else "",
                    "error": h.error[:200] if h.error else "",
                    "duration": h.duration,
                }
                for h in history
            ],
            "stats": tool.get_stats(),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get history: {e!s}")


@router.post("/sandbox/execute")
async def execute_in_sandbox(request: SandboxExecuteRequest) -> dict[str, Any]:
    """在沙箱中执行命令"""
    try:
        from hicode.sandbox import SandboxConfig, create_safe_executor

        config = SandboxConfig(
            memory_limit=request.memory_limit,
            time_limit=request.time_limit,
            network_blocked=request.network_blocked,
            audit_enabled=True,
        )

        executor = create_safe_executor(config)
        await executor.start()
        try:
            if request.script:
                result = await executor.run_script(request.script)
            else:
                result = await executor.execute(request.command)

            return {
                "status": "success" if result["exit_code"] == 0 else "failed",
                "output": result.get("stdout", ""),
                "error": result.get("stderr", ""),
                "exit_code": result["exit_code"],
                "duration": result.get("duration", 0),
                "sandboxed": True,
            }
        finally:
            await executor.stop()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sandbox execution failed: {e!s}")
